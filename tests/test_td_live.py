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


class _SlowStopExecutor(_FakeExecutor):
    """stop 后收尾慢（模拟 lumibot on_abrupt_closing / scheduler 清理）。"""

    def __init__(self, quit_delay=0.5):
        super().__init__()
        self.quit_delay = quit_delay

    def run(self):
        self.running = True
        while not self.stopped:
            time.sleep(0.01)
        time.sleep(self.quit_delay)  # 收尾耗时
        self.running = False


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


def test_param_change_waits_for_thread_exit(monkeypatch):
    """stop 后旧线程收尾慢（>0.3s）→ 等待退出再启动，不被 is_alive 守卫挡回。

    回归：真实 lumibot 收尾（on_abrupt_closing + scheduler 清理）超过
    旧版 sleep(0.3)，start() 的 is_alive 守卫把新循环挡回，参数变更后
    停在 running=False（"Scheduler is None" 噪音）。
    """
    fake1 = _SlowStopExecutor(quit_delay=0.6)
    runner = _fresh_runner(monkeypatch, fake1)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    assert runner._thread is not None and runner._thread.is_alive()

    fake2 = _FakeExecutor()
    monkeypatch.setattr(runner, "_build_executor", lambda params: fake2)
    st = runner.sync_from_params(_params(td_enabled=True, td_symbol="CRCLX"))
    assert st["running"] is True
    assert st["symbol"] == "CRCLX"
    time.sleep(0.1)
    assert fake1.running is False  # 旧线程已退出
    assert fake2.running is True  # 新循环已启动


def test_restart_timeout_fails_closed(monkeypatch):
    """旧线程超时未退出 → 拒绝启动新循环（fail-closed，防双循环重复下单）。"""
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    monkeypatch.setattr(runner, "_wait_thread_exit", lambda *a, **k: False)
    st = runner.sync_from_params(_params(td_enabled=True, td_symbol="CRCLX"))
    assert st["running"] is False
    assert st["last_error"]


def test_start_waits_when_thread_tearing_down(monkeypatch):
    """直接调 start()（不经 sync）时，线程收尾中也等待退出再启动。"""
    fake1 = _SlowStopExecutor(quit_delay=0.4)
    runner = _fresh_runner(monkeypatch, fake1)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    runner.stop()
    assert runner._thread is not None and runner._thread.is_alive()

    fake2 = _FakeExecutor()
    monkeypatch.setattr(runner, "_build_executor", lambda params: fake2)
    st = runner.start(_params(td_enabled=True, td_symbol="CRCLX"))
    assert st["running"] is True
    time.sleep(0.1)
    assert fake2.running is True
