"""td_live 管理器测试（P2 B3）— 生命周期语义（不依赖真实 lumibot）。

用 fake executor 替换 _build_executor（StrategyExecutor 构造需要真实
lumibot，测试容器没有），验证 sync_from_params 的启停/重启逻辑。
"""

from __future__ import annotations

import time

from nanobot_quant import td_live


class _FakeExecutor:
    def __init__(self):
        self.stopped = False
        self.running = False

    def run(self):
        self.running = True
        while not self.stopped:
            time.sleep(0.01)
        self.running = False

    def stop(self):
        self.stopped = True


def _params(**over):
    p = dict(
        td_enabled=False, td_symbol="SOL", td_sleeptime="1D",
        quantity_mode="fixed", td_quantity=10,
        max_position_pct=0.20, max_drawdown_pct=0.15, stop_loss_pct=0.10,
        slippage=0.01, sol_buffer_pct=0.05,
    )
    p.update(over)
    return p


def _fresh_runner(monkeypatch, fake):
    runner = td_live._TdLiveRunner()
    monkeypatch.setattr(runner, "_build_executor", lambda params: fake)
    return runner


def test_disabled_does_not_start(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    st = runner.sync_from_params(_params(td_enabled=False))
    assert st["running"] is False
    assert st["thread_alive"] is False
    assert fake.running is False


def test_enabled_starts_loop(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    st = runner.sync_from_params(_params(td_enabled=True))
    assert st["running"] is True
    assert st["symbol"] == "SOL"
    assert st["sleeptime"] == "1D"
    time.sleep(0.05)  # 给线程启动
    assert fake.running is True


def test_enabled_idempotent(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    st = runner.sync_from_params(_params(td_enabled=True))
    assert st["running"] is True
    # 未重复构造 executor（单例）——fake 的 stop 未被调用
    assert fake.stopped is False


def test_param_change_restarts(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    st = runner.sync_from_params(_params(td_enabled=True, td_symbol="CRCLX"))
    assert st["running"] is True
    assert st["symbol"] == "CRCLX"
    assert fake.stopped is True  # 旧循环已 stop


def test_disable_stops_loop(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    st = runner.sync_from_params(_params(td_enabled=False))
    assert st["running"] is False
    assert fake.stopped is True
    time.sleep(0.05)
    assert fake.running is False


def test_status_shape(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    st = runner.status()
    assert "running" in st and "thread_alive" in st
    assert "symbol" in st and "sleeptime" in st and "quantity_mode" in st
    assert "last_error" in st


def test_module_level_sync_and_status(monkeypatch):
    """模块级 sync_from_params()/status() 单例入口（td_enabled=False 时不启动）。"""
    fake = _FakeExecutor()
    runner = td_live._TdLiveRunner()
    monkeypatch.setattr(runner, "_build_executor", lambda params: fake)
    monkeypatch.setattr(td_live, "_runner", runner)
    st = td_live.sync_from_params(_params(td_enabled=False))
    assert st["running"] is False
    assert td_live.status()["running"] is False
