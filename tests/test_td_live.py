"""td_live 管理器测试（P2 B3）— 生命周期语义（不依赖真实 lumibot）。

用 fake executor 替换 _build_executor（StrategyExecutor 构造需要真实
lumibot，测试容器没有），验证 sync_from_params 的启停/重启逻辑。
"""

from __future__ import annotations

import threading
import time

import pytest

from nanobot_quant import td_live


class _FakeExecutor:
    def __init__(self):
        self.stopped = False
        self.running = False
        # 2026-08-21 延迟停止：stop() 不再调 executor.stop()，改设
        # stop_event + 清理 scheduler（run() 模拟主循环感知 stop_event
        # 后 break）。
        self.stop_event = threading.Event()
        self.scheduler = None

    def run(self):
        self.running = True
        while not self.stopped and not self.stop_event.is_set():
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
        while not self.stopped and not self.stop_event.is_set():
            time.sleep(0.01)
        time.sleep(self.quit_delay)  # 收尾耗时
        self.running = False


def _params(**over):
    p = dict(
        td_enabled=False, td_symbols=["SOL"], td_sleeptime="1D",
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
    assert st["symbols"] == ["SOL"]
    assert st["sleeptime"] == "1D"
    time.sleep(0.05)  # 给线程启动
    assert fake.running is True


def test_enabled_idempotent(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    st = runner.sync_from_params(_params(td_enabled=True))
    assert st["running"] is True
    # 未重复构造 executor（单例）——延迟停止未触发（stop_event 未置位）
    assert fake.stop_event.is_set() is False


def test_param_change_restarts(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    st = runner.sync_from_params(_params(td_enabled=True, td_symbols=["CRCLX"]))
    assert st["running"] is True
    assert st["symbols"] == ["CRCLX"]
    time.sleep(0.1)
    assert fake.stop_event.is_set()  # 旧循环已收到停止信号


def test_disable_stops_loop(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    runner.sync_from_params(_params(td_enabled=True))
    time.sleep(0.05)
    st = runner.sync_from_params(_params(td_enabled=False))
    assert st["running"] is False
    time.sleep(0.1)
    assert fake.stop_event.is_set()  # 延迟停止已发停止信号
    time.sleep(0.05)
    assert fake.running is False


def test_status_shape(monkeypatch):
    fake = _FakeExecutor()
    runner = _fresh_runner(monkeypatch, fake)
    st = runner.status()
    assert "running" in st and "thread_alive" in st
    assert "symbols" in st and "sleeptime" in st and "quantity_mode" in st
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
    st = runner.sync_from_params(_params(td_enabled=True, td_symbols=["CRCLX"]))
    assert st["running"] is True
    assert st["symbols"] == ["CRCLX"]
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
    st = runner.sync_from_params(_params(td_enabled=True, td_symbols=["CRCLX"]))
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
    st = runner.start(_params(td_enabled=True, td_symbols=["CRCLX"]))
    assert st["running"] is True
    time.sleep(0.1)
    assert fake2.running is True

class TestBuildExecutorChannel:
    """P2: execution_channel 分叉 — dex=OnchainOS，cex=CexBroker + CexDataSource。

    通过 sys.modules 注入 fake 模块拦截 _build_executor 函数内延迟 import
    （测试容器无真实 lumibot），只验证 broker/data_source 选型，不验证
    StrategyExecutor 真实构造。
    """

    def _fake_modules(self, monkeypatch):
        import sys
        import types

        fakes = {}

        class FakeCexBroker:
            def __init__(self, **kw):
                self.data_source = kw["data_source"]
                fakes["cex_broker_kw"] = kw

        class FakeCexDataSource:
            def __init__(self, **kw):
                fakes["cex_ds_kw"] = kw

        class FakeOnchainBroker:
            def __init__(self, **kw):
                self.data_source = kw["data_source"]
                fakes["dex_broker_kw"] = kw

        class FakeOnchainDataSource:
            def __init__(self, **kw):
                fakes["dex_ds_kw"] = kw

        class FakeTdStrategy:
            parameters = {}

            def __init__(self, **kw):
                self.broker = kw.get("broker")
                self.data_source = kw.get("data_source")

        class FakeExecutor:
            daemon = False

            def __init__(self, strategy):
                self.strategy = strategy

        def _mod(name, attrs):
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            monkeypatch.setitem(sys.modules, name, m)
            return m

        _mod("nanobot_quant.brokers.cex_broker", {"CexBroker": FakeCexBroker})
        _mod("nanobot_quant.data.cex_data_source", {"CexDataSource": FakeCexDataSource})
        _mod(
            "nanobot_quant.brokers.onchainos_broker",
            {"OnchainOSBroker": FakeOnchainBroker},
        )
        _mod(
            "nanobot_quant.data.onchainos_data_source",
            {"OnchainOSDataSource": FakeOnchainDataSource},
        )
        _mod(
            "nanobot_quant.strategies.registry",
            {"load_selected": lambda: "td_sequential"},
        )
        _mod(
            "nanobot_quant.strategies.td_sequential_strategy",
            {"TdSequentialStrategy": FakeTdStrategy},
        )
        _mod("nanobot_quant.td_params", {"load_td_params": lambda n: {}})
        _mod("nanobot_quant.tokens_store", {"load_tokens_json": lambda: []})
        _mod(
            "lumibot.strategies.strategy_executor",
            {"StrategyExecutor": FakeExecutor},
        )
        return fakes, FakeCexBroker, FakeCexDataSource, FakeOnchainBroker, FakeOnchainDataSource

    def test_cex_channel(self, monkeypatch):
        fakes, CexB, CexDS, _OnB, _OnDS = self._fake_modules(monkeypatch)
        runner = td_live._TdLiveRunner()
        ex = runner._build_executor(
            _params(td_enabled=True, execution_channel="gate")
        )
        assert isinstance(ex.strategy.broker, CexB)
        assert isinstance(ex.strategy.broker.data_source, CexDS)
        assert fakes["cex_broker_kw"]["slippage"] == "0.01"
        assert "sol_buffer_pct" not in fakes["cex_broker_kw"]

    def test_legacy_cex_channel_normalized(self, monkeypatch):
        """旧值 cex 经 normalize_execution_channel 迁移后仍构造 CexBroker（方案 C）。"""
        fakes, CexB, CexDS, _OnB, _OnDS = self._fake_modules(monkeypatch)
        runner = td_live._TdLiveRunner()
        ex = runner._build_executor(
            _params(td_enabled=True, execution_channel="cex")
        )
        assert isinstance(ex.strategy.broker, CexB)
        assert isinstance(ex.strategy.broker.data_source, CexDS)

    def test_dex_channel_default(self, monkeypatch):
        fakes, _CexB, _CexDS, OnB, OnDS = self._fake_modules(monkeypatch)
        runner = td_live._TdLiveRunner()
        ex = runner._build_executor(
            _params(td_enabled=True, execution_channel="okx_dex")
        )
        assert isinstance(ex.strategy.broker, OnB)
        assert isinstance(ex.strategy.broker.data_source, OnDS)
        assert fakes["dex_broker_kw"]["slippage"] == "0.01"

    def test_unknown_channel_fails_closed(self, monkeypatch):
        """未知执行通道 fail-closed（KeyError），绝不静默回退到别所的行情。"""
        self._fake_modules(monkeypatch)
        runner = td_live._TdLiveRunner()
        with pytest.raises(KeyError):
            runner._build_executor(
                _params(td_enabled=True, execution_channel="coinbase")
            )
def test_prepare_scene_batches_fail_closed_sub_accounts_short():
    """S3b：场景 sub_accounts 长度 < batches → ValueError（fail-closed，
    不静默截断）；匹配时把 scene 传给 _prepare_batches（场景化台账）。"""
    tl = td_live._TdLiveRunner()
    with pytest.raises(ValueError, match="子账号池不足"):
        tl._prepare_scene_batches(
            {"batches": 5, "sub_accounts": ["gate_bot1", "gate_bot2"]},
            ["CRCLX"], "gate", "high",
        )
    # 缺省 sub_accounts（None）不触发 fail-closed（回退旧映射）
    tl2 = td_live._TdLiveRunner()
    tl2._prepare_batches = lambda *a, **k: None
    assert tl2._prepare_scene_batches(
        {"batches": 2}, ["CRCLX"], "gate", "high",
    ) == {}


def test_prepare_scene_batches_passes_scene(monkeypatch):
    """S3b：场景台账路径——_prepare_batches 收到 scene（batches.gate.high.*）。"""
    tl = td_live._TdLiveRunner()
    seen = {}

    def fake_prepare(batches_n, sym, channel, account_ids=None, scene=None):
        seen.update({"n": batches_n, "sym": sym, "channel": channel,
                     "scene": scene, "ids": account_ids})
        return object()

    tl._prepare_batches = fake_prepare
    bms = tl._prepare_scene_batches(
        {"batches": 2, "sub_accounts": ["gate_bot1", "gate_bot2"]},
        ["CRCLX"], "gate", "high",
    )
    assert seen == {"n": 2, "sym": "CRCLX", "channel": "gate",
                    "scene": "high", "ids": ["gate_bot1", "gate_bot2"]}
    assert len(bms) == 1
