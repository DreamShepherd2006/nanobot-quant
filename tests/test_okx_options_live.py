"""okx_options_live 单元测试（C23 S3 到期巡检 daemon，不触网）。

mock ot.settle_expired_puts / 凭证存储路径，验证：
单轮巡检 run_once（事件落盘 + LIVE_STATE）、异常不外抛、
配置存取与规范化、线程启停/重启（sync）、事件文件尾部读取。

另覆盖策略轮次（E 期方案 B：策略跑在 lumibot executor 里）：dry_run 只记录不下单、
关掉 dry_run 才真下单/买回、持仓查询失败不抛异常。
"""

import threading
import time

import pytest

from nanobot_quant import okx_options_live as ol
from nanobot_quant import okx_options_trade as ot


@pytest.fixture
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "params_path", lambda: tmp_path / "okx_options_params.json")
    monkeypatch.setattr(ol, "_storage_dir", lambda: tmp_path)
    # 重置模块级全局状态（事件文件/线程隔离由 monkeypatch+stop 负责，_state 需显式复位）
    ol.stop()
    ol._state.update(running=False, last_run=None, last_settled=[],
                     last_error="", total_settled=0, last_strategy=None,
                     total_entries=0, total_exits=0)
    # 机制层已抽到 LiveRunnerBase（2026-09-16）：线程/停止位/计数都在 runner 实例上
    _r = ol._runner()
    _r._thread = None
    _r._stop_event.clear()
    _r._state["totals"] = {}
    # 方案 B：策略/executor 是 runner 实例状态 —— 不复位会把上一个测试的
    # stop_requested / _stopping 等标志带过来（表现为下一测试首轮就不执行）。
    _r._executor = None
    _r._stopping = False
    from nanobot_quant import okx_options_live_state as _lst
    _lst.reset()
    # 注入轻量策略：do_round() 走「真实策略逻辑」，但不构造 broker / 不碰网络。
    # 入场扫描默认关（families=[]）—— 需要测入场的用例自行覆盖该参数。
    from nanobot_quant.strategies.okx_options_put_strategy import OkxOptionsPutStrategy
    _s = OkxOptionsPutStrategy()
    _s.parameters = {**dict(OkxOptionsPutStrategy.parameters),
                     "live_mode": True, "families": []}
    _r._strategy = _s
    # 持仓查询也隔离（否则真去读 Gate/OKX 凭证）
    monkeypatch.setattr(ot, "open_option_positions", lambda account="": [], raising=False)
    # 策略轮次默认打桩（不触网）：持仓空 + 无 K 线；策略专项测试用 _strat 覆盖
    monkeypatch.setattr(ot, "open_option_positions", lambda account="": [])
    monkeypatch.setattr(OkxOptionsPutStrategy, "_td_signal",
                        lambda self, family, base, p: None)
    yield tmp_path
    ol.stop()  # 保证测试结束无 daemon 线程泄漏
    ol._state.update(running=False, last_run=None, last_settled=[],
                     last_error="", total_settled=0, last_strategy=None,
                     total_entries=0, total_exits=0)


# ── 配置 ─────────────────────────────────────────────────

def test_live_config_default(_iso):
    cfg = ol.live_config()
    assert cfg["enabled"] is False
    assert cfg["interval_s"] == ol.DEFAULT_INTERVAL_S
    # Step 2b：策略段默认值随配置返回（dry_run 默认 True = 只观测不下单）
    assert cfg["strategy"]["dry_run"] is True


def test_save_live_config_persists_and_clamps(_iso):
    ol.save_live_config(enabled=True, interval_s=30)
    cfg = ol.live_config()
    assert cfg["enabled"] is True and cfg["interval_s"] == 30
    # 越界值 clamp
    ol.save_live_config(interval_s=999999)
    assert ol.live_config()["interval_s"] == ol.MAX_INTERVAL_S
    ol.save_live_config(interval_s=1)
    assert ol.live_config()["interval_s"] == ol.MIN_INTERVAL_S
    # 非法类型忽略
    ol.save_live_config(interval_s="abc")
    assert ol.live_config()["interval_s"] == ol.MIN_INTERVAL_S
    # 只改 interval 不动 enabled
    ol.save_live_config(interval_s=45)
    cfg = ol.live_config()
    assert cfg["enabled"] is True and cfg["interval_s"] == 45
    # collateral 字段保留
    assert ot.load_option_params().get("collateral_ratio_pct") == ot.DEFAULT_COLLATERAL_RATIO_PCT


# ── 单轮巡检 run_once ────────────────────────────────────

def _settled_one(**kw):
    d = {"id": "evt1", "inst_id": "SOL-USD_UM-260906-101-P",
         "status": ot.STATUS_SETTLED_OTM, "settle_px": 105.0,
         "settle_pnl": 0.007, "note": ""}
    d.update(kw)
    return d


class _FakeScheduler:
    """假 scheduler（镜像 lumibot ``executor.scheduler`` 的两个用法）。"""

    def __init__(self):
        self.removed = 0
        self.shutdown_calls: list = []

    def remove_all_jobs(self):
        self.removed += 1

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


class _FakeExecutor:
    """测试用 executor 替身（方案 B 后 runner 把节拍交给 lumibot）。

    镜像真实 lumibot 的两条停止语义：``executor.stop_event``（主循环据此 break
    —— 2026-09-25 修正后 runner 走这条）+ ``strategy.parameters["stop_requested"]``
    （策略侧本轮不再执行）；``scheduler`` 用于验证停止时的清理调用。
    不依赖 threading 以外的依赖 —— 只需 time（本文件已 import）。
    """

    def __init__(self, strategy=None, rounds: int = 0):
        self.strategy = strategy
        self.daemon = False
        self.rounds = rounds
        self._count = 0
        self._stopped = False
        self.stop_event = threading.Event()
        self.scheduler = _FakeScheduler()

    def run(self):
        while not self._stopped and not self.stop_event.is_set():
            if self.strategy is not None and \
                    self.strategy.parameters.get("stop_requested"):
                return
            if self.rounds and self._count < self.rounds:
                self._count += 1
                self.strategy.on_trading_iteration()
            time.sleep(0.05)

    def stop(self):
        self._stopped = True


def _patch_executor(monkeypatch, rounds: int = 0):
    """把 `_build_executor` 换成建假 executor —— 不碰网络/凭证。

    同时清除残留的 ``stop_requested``：真实 `_build_executor` 也会重置它，
    否则重启后新线程会看到上一轮停止留下的 True 而立即退出（sync 变 False）。
    """
    def _build(self):
        if self._strategy is not None:
            self._strategy.parameters["stop_requested"] = False
        self._executor = _FakeExecutor(strategy=self._strategy, rounds=rounds)
        return self._executor
    monkeypatch.setattr(ol._OkxOptionsRunner, "_build_executor", _build)


def test_to_sleeptime_matches_lumibot_parser():
    """sleeptime 必须是「数字 + 单字母」—— lumibot 用 int(sleeptime[:-1]) 解析。

    实证（2026-09-16 HF Space）：传 "minute" 时 int("minut") 抛 ValueError，
    策略在 _setup_live_trading_scheduler 就崩了（on_bot_crash）。
    """
    assert ol._to_sleeptime(60) == "1m"
    assert ol._to_sleeptime(300) == "5m"
    assert ol._to_sleeptime(3600) == "1H"
    assert ol._to_sleeptime(86400) == "1D"
    assert ol._to_sleeptime(0) == "1m"      # 下限保护
    assert ol._to_sleeptime(None) == "1m"    # 脏值保护
    for s in (ol._to_sleeptime(60), ol._to_sleeptime(300),
              ol._to_sleeptime(3600), ol._to_sleeptime(86400)):
        int(s[:-1])                          # 必须能过 lumibot 的解析


def test_run_once_appends_event_and_state(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one()])
    res = ol.run_once()
    assert res["ok"] is True
    # 方案 B：轮次结果由策略写进状态模块（现场内），经 live_state() 读出
    st = ol.live_state()
    assert st["last_error"] == ""
    assert st["last_settled"][0]["status"] == ot.STATUS_SETTLED_OTM
    assert st["last_run"] and st["total_settled"] == 1
    evs = ol.load_events(10)
    assert len(evs) == 1
    assert evs[0]["type"] == "settle"
    assert evs[0]["inst_id"] == "SOL-USD_UM-260906-101-P"
    assert evs[0]["status"] == ot.STATUS_SETTLED_OTM
    assert evs[0]["settle_px"] == pytest.approx(105.0)


def test_run_once_no_settle_no_event(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [])
    ol.run_once()
    st = ol.live_state()
    assert st["last_settled"] == [] and st["last_error"] == ""
    assert st["last_strategy"] is not None   # 同一轮也跑策略决策
    assert ol.load_events(10) == []
    assert st["total_settled"] == 0


def test_run_once_error_swallowed_to_state(_iso, monkeypatch, capsys):
    """到期判定内部异常 → 策略自愈（本轮跳过），不外抛也不上抛。

    语义变化（E 期接线步 2）：接线前异常冒到 runner 记 `last_error`；现在
    到期判定已挪进策略的 `_settle_expired()`，那里自己 try/except 后写
    stderr 诊断，因此 runner 侧 `last_error` 保持为空。
    """
    def boom():
        raise RuntimeError("OKX 500")
    monkeypatch.setattr(ot, "settle_expired_puts", boom)
    res = ol.run_once()  # 不外抛
    assert res["ok"] is True
    st = ol.live_state()
    assert st["last_error"] == ""
    assert st["last_settled"] == []
    assert st["total_settled"] == 0
    err = capsys.readouterr().err
    assert "到期判定异常" in err and "RuntimeError: OKX 500" in err


def test_load_events_tail_limit(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one(id=f"e{i}") for i in range(3)])
    ol.run_once()
    ol.run_once()
    # 2 轮 × 3 笔 = 6 条事件
    assert len(ol.load_events(50)) == 6
    # tail limit 只取最近 N 条（倒序）
    tail = ol.load_events(2)
    assert len(tail) == 2


def _settled_itm_one(**kw):
    d = {"id": "evt-itm", "inst_id": "SOL-USD_UM-260907-106-P",
         "status": ot.STATUS_SETTLED_ITM, "settle_px": 104.445181,
         "settle_pnl": -0.0644, "settle_payout": 0.1555, "note": ""}
    d.update(kw)
    return d


def test_load_events_enrich_covered(_iso, monkeypatch):
    """settled_itm 判定行 enrich covered：台账存在同现货对 filled spot_cover 即已补买。"""
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [_settled_itm_one()])
    monkeypatch.setattr(ot, "load_ledger", lambda: [
        {"kind": "spot_cover", "inst_id": "SOL-USD", "status": "filled"}])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and evs[0]["status"] == ot.STATUS_SETTLED_ITM
    assert evs[0]["covered"] is True


def test_load_events_itm_without_cover_not_covered(_iso, monkeypatch):
    """无对应现货补买时 settled_itm 行 covered=False（前端保留「补买」入口）。"""
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [_settled_itm_one()])
    monkeypatch.setattr(ot, "load_ledger", lambda: [])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and evs[0]["status"] == ot.STATUS_SETTLED_ITM
    assert evs[0].get("covered") is False


def test_load_events_otm_untouched(_iso, monkeypatch):
    """OTM 事件不加 covered 字段（无补买概念）。"""
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one(status=ot.STATUS_SETTLED_OTM)])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and "covered" not in evs[0]


# ── 线程生命周期 sync ────────────────────────────────────

def _wait_stopped(timeout: float = 5.0) -> bool:
    """等优雅停止收尾。

    ``stop()`` 立即返回（不阻塞页面），线程在数百毫秒内退出 —— 旧版测试直接
    断言 ``running is False``，在异步停语义下必然失败（2026-09-25 改异步后修正）。
    """
    deadline = time.time() + timeout
    r = ol._runner()
    while time.time() < deadline:
        if ol.live_state()["running"] is False and r._thread is None:
            return True
        time.sleep(0.05)
    return False


def _wait(pred, timeout: float = 5.0) -> bool:
    """轮询等条件成立（线程刚起/刚退都可能需一拍才可见）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


def test_sync_start_stop(_iso, monkeypatch):
    # 缩短心跳避免测试挂起——直接 patch interval 后启动
    # 用长驻假 executor：本用例只验启停语义，不碰网络（真 executor 会查 OKX 账户，
    # 整套跑受负载影响时会因异常而线程早退 —— 也是这个用例历史上 flaky 的根因）
    _patch_executor(monkeypatch)
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    ol.sync()
    # 线程刚起来可能需一拍（整套跑时负载高）——失败时把 last_error 打出来
    assert _wait(lambda: ol.live_state()["running"]), \
        f"循环未起来：last_error={ol.live_state().get('last_error')!r}"
    # 同一配置再 sync = 幂等不重启
    assert ol.sync()["running"] is True
    ol.save_live_config(enabled=False)
    assert ol.sync()["running"] is False
    assert _wait_stopped()


def test_sync_interval_change_restarts(_iso, monkeypatch):
    _patch_executor(monkeypatch)          # 长驻假 executor：不碰网络
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    assert ol.sync()["running"] is True
    ol.save_live_config(interval_s=15)
    st = ol.sync()
    assert st["running"] is True
    assert st["config"]["interval_s"] == 15
    ol.save_live_config(enabled=False)
    ol.sync()
    assert _wait_stopped()


def test_daemon_runs_periodic_settle(_iso, monkeypatch):
    """真实线程跑一轮：mock settle 返回一笔 → 事件文件落盘。

    方案 B 后节拍在 lumibot executor 里，这里用假 executor 跑 1 轮策略。
    """
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one()])
    _patch_executor(monkeypatch, rounds=1)
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    ol.sync()
    assert ol.live_state()["running"] is True
    deadline = time.time() + 5
    while ol.live_state()["total_settled"] < 1 and time.time() < deadline:
        time.sleep(0.1)
    assert ol.live_state()["total_settled"] >= 1
    assert len(ol.load_events(10)) >= 1
    ol.stop()
    assert _wait_stopped()


def test_stop_sets_executor_stop_event_and_cleans_scheduler(_iso, monkeypatch):
    """停止通道 = executor.stop_event + scheduler 清理（不再是 SystemExit）。"""
    _patch_executor(monkeypatch)
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    assert ol.sync()["running"] is True
    ex = ol._runner()._executor
    sched = ex.scheduler
    assert ol.stop()["ok"] is True
    assert _wait_stopped()
    assert ex.stop_event.is_set()             # lumibot 主循环据此 break
    assert sched.removed == 1                 # remove_all_jobs
    assert sched.shutdown_calls == [False]     # shutdown(wait=False)
    assert ex.scheduler is None
    assert ol._runner()._thread is None


def test_stop_marks_strategy_stop_requested(_iso, monkeypatch):
    """停止位 + 策略看到后只 return（不再抛 SystemExit）。"""
    _patch_executor(monkeypatch)
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    ol.sync()
    s = ol._runner()._strategy
    ol.stop()
    assert _wait_stopped()
    assert s.parameters["stop_requested"] is True
    assert s.on_trading_iteration() is None   # 不抛异常（异常会被 APScheduler 吞掉）


def test_start_fail_closed_when_old_thread_alive(_iso, monkeypatch):
    """旧循环未退完时拒绝起新线程（否则基类 is_alive 守卫静默吞掉新配置）。"""
    monkeypatch.setattr(ol, "STOP_JOIN_WAIT_S", 0.2)
    r = ol._runner()
    blocker = threading.Thread(target=lambda: time.sleep(1.5), daemon=True)
    blocker.start()
    r._thread = blocker
    res = r.start()
    assert res["ok"] is False and res["started"] is False
    assert "尚未退出" in res["reason"]
    assert r._executor is None                # 未构造新 executor
    blocker.join(3)


def test_finish_counts_actions_only(_iso):
    """计数只认动作记录 —— fail-closed 跳过不再被记成「卖call 1」。"""
    s = ol._runner()._strategy
    s.parameters["live_mode"] = False         # 不写事件文件
    s._finish([], {"entries": [{"status": "no_action"},
                              {"status": "cycle_wait"},
                              {"status": "dry_run(would_sell)"},
                              {"status": "failed"}],
                  "exits": [{"status": "dry_run(would_buy_back)"}],
                  "call_entries": [{"status": "no_action"}],
                  "call_exits": []}, "")
    tot = ol.live_state()
    assert tot["total_entries"] == 1          # 只有 dry_run(would_sell)
    assert tot["total_exits"] == 1
    assert tot["total_call_entries"] == 0     # fail-closed 跳过不计


def test_stop_idempotent(_iso):
    ol.stop()
    ol.stop()
    assert ol.live_state()["running"] is False


# ── Step 2b：策略轮次（dry_run / 真下单）──────────────────

SOL_POS = {"inst_id": "SOL-USD_UM-260918-94-P", "side": "short", "pos": 1.0,
           "avg_px": 0.25, "mark_px": 0.39}
CAND = {"inst_id": "SOL-USD_UM-260920-92-P", "strike": 92.0, "bid": 0.6,
        "net_yield_pct": 0.55, "days": 5, "notional_usd": 9.2}


@pytest.fixture
def _strat(monkeypatch, _iso):
    """策略轮次所需外部依赖全部打桩（不触网 / 不下单）。"""
    from nanobot_quant import okx_options_strategy as st

    calls = {"sell": [], "buy": []}
    # TD 取数已随策略落地（方案 B）：patch 策略方法即可
    from nanobot_quant.strategies.okx_options_put_strategy import (
        OkxOptionsPutStrategy as _OptStrat)
    _sig = lambda *a, **k: {"setup_buy": 9, "cd_buy": 0}
    monkeypatch.setattr(_OptStrat, "_td_signal", lambda self, family, base, p: _sig())
    # _iso 默认关掉 families（防误触网），本 fixture 的用例要真跑入场扫描
    _r_ = ol._runner()
    if _r_._strategy is not None:
        _r_._strategy.parameters["families"] = ["SOL-USD_UM"]
    monkeypatch.setattr(ol.ot, "open_option_positions", lambda account="": [dict(SOL_POS)])
    monkeypatch.setattr(ol.ot, "suggest_px_for_order",
                        lambda inst, side, sz=None: {"px": 0.3})
    monkeypatch.setattr(ol.ot, "open_put",
                        lambda acc, **kw: (calls["sell"].append(kw)
                                           or {"ord_id": "s1", "status": "pending"}))
    monkeypatch.setattr(ol.ot, "close_put",
                        lambda acc, **kw: (calls["buy"].append(kw)
                                           or {"ord_id": "b1", "status": "pending"}))
    monkeypatch.setattr(st, "select_puts",
                        lambda family, base_px=None, selector=None, chain=None: {
                            "family": family, "base_px": 100.0, "spot": 100.0,
                            "lot_coin": 0.1, "selector": selector,
                            "candidates": [dict(CAND)], "scanned": 1,
                            "filtered": {}, "note": ""})
    return calls


def _cfg(**strat):
    base = dict(ol.DEFAULT_STRATEGY)
    base.update(strat)
    return {"enabled": True, "interval_s": 60, "strategy": base}


class TestStrategyConfig:
    def test_live_config_defaults_include_strategy(self, _iso):
        cfg = ol.live_config()
        assert cfg["strategy"]["dry_run"] is True          # 默认只观测不下单
        assert cfg["strategy"]["families"] == ["SOL-USD_UM"]
        assert cfg["strategy"]["iv_min_percentile"] == 0

    def test_save_live_config_persists_strategy(self, _iso):
        ol.save_live_config(enabled=True, interval_s=120,
                            strategy={"dry_run": False, "td_period": "15m"})
        cfg = ol.live_config()
        assert cfg["enabled"] is True and cfg["interval_s"] == 120
        assert cfg["strategy"]["dry_run"] is False
        assert cfg["strategy"]["td_period"] == "15m"

    def test_unknown_strategy_keys_ignored(self, _iso):
        ol.save_live_config(strategy={"nonsense": 1, "dry_run": True})
        assert "nonsense" not in ol.live_config()["strategy"]

    def test_run_once_appends_entry_events(self, _strat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy={"dry_run": True,
                                                    "max_contracts_per_family": 2})
        ol.run_once()
        assert ol.live_state()["last_strategy"]["entries"]
        assert any(e.get("type") == "entry" for e in ol.load_events())
        assert ol.live_state()["total_entries"] >= 1


# ── C41：卖 call（covered）支线（策略轮次，§33.40）────────────────

CALL_CAND = {"inst_id": "SOL-USD_UM-260920-108-C", "strike": 108.0, "bid": 0.5,
             "net_yield_pct": 0.48, "days": 5, "notional_usd": 10.8,
             "collateral_usd": 10.0, "covered": True, "delta": 0.2}


def _cov(hint=104.0, sellable=2, spot=0.2):
    return {"base": "SOL", "spot_avail": spot, "sellable_sz": sellable,
            "spot_cov_pct": 200.0, "cost_hint": hint}


@pytest.fixture
def _callstrat(_strat, monkeypatch):
    """在 _strat 之上补 covered 上下文 + call 选档 + 下单打桩（不触网/不下单）。"""
    from nanobot_quant import okx_options_strategy as st
    from nanobot_quant.strategies.okx_options_put_strategy import (
        OkxOptionsPutStrategy as _S)

    orders = {"sell": [], "buy": []}
    monkeypatch.setattr(ol.ot, "covered_context", lambda account, family: _cov())
    monkeypatch.setattr(st, "select_calls",
                        lambda family, base_px=None, selector=None, chain=None, cost_basis=None: {
                            "family": family, "right": "C", "base_px": 100.0,
                            "spot": 100.0, "lot_coin": 0.1, "selector": selector,
                            "candidates": [dict(CALL_CAND)], "scanned": 1,
                            "filtered": {}, "note": ""})

    def _submit(self, dec, p, closing=False):
        orders["buy" if closing else "sell"].append(dec)
        return True, None

    monkeypatch.setattr(_S, "_submit_option", _submit)
    return orders


def _call_cfg(**strat):
    base = {"dry_run": True, "put_enabled": False, "call_enabled": True}
    base.update(strat)
    return base


class TestCallLine:
    def test_default_off(self, _strat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy={"dry_run": True, "put_enabled": False})
        ol.run_once()
        assert ol.live_state()["last_strategy"]["call_entries"] == []

    def test_dry_run_records_intent_without_order(self, _callstrat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy=_call_cfg())
        ol.run_once()
        rows = ol.live_state()["last_strategy"]["call_entries"]
        assert rows and rows[0]["opt_type"] == "C"
        assert str(rows[0]["status"]).startswith("dry_run")
        assert _callstrat["sell"] == []                        # dry-run 不下单
        assert ol.live_state()["total_call_entries"] >= 1
        assert any(e.get("type") == "entry" and e.get("opt_type") == "C"
                   for e in ol.load_events())

    def test_real_order_after_dry_run_off(self, _callstrat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy=_call_cfg(dry_run=False))
        ol.run_once()
        assert len(_callstrat["sell"]) == 1
        assert ol.live_state()["last_strategy"]["call_entries"][0]["status"] == "sold"

    def test_put_line_off_skips_put_scan(self, _callstrat, monkeypatch, _iso):
        """put_enabled=false ⇒ put 线不扫描（便于只验证 call 线）。"""
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy=_call_cfg(dry_run=False))
        ol.run_once()
        assert ol.live_state()["last_strategy"]["entries"] == []

    def test_no_cost_anchor_skips(self, _callstrat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "covered_context", lambda account, family: _cov(hint=None))
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy=_call_cfg(dry_run=False))
        ol.run_once()
        rows = ol.live_state()["last_strategy"]["call_entries"]
        assert rows and rows[0]["status"] == "no_action" and "无成本锚" in rows[0]["note"]
        assert _callstrat["sell"] == []

    def test_allow_no_cost_basis_sells(self, _callstrat, monkeypatch, _iso):
        monkeypatch.setattr(ol.ot, "covered_context", lambda account, family: _cov(hint=None))
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True,
                            strategy=_call_cfg(dry_run=False, allow_no_cost_basis=True))
        ol.run_once()
        assert len(_callstrat["sell"]) == 1

    def test_call_exit_uses_own_tp_line(self, _callstrat, monkeypatch, _iso):
        """call 止盈线独立（30%）：回落 32.5% 即买回（put 线 50% 不适用于 call）。"""
        monkeypatch.setattr(ol.ot, "open_option_positions",
                            lambda account="": [{"inst_id": "SOL-USD_UM-260918-108-C",
                                                  "side": "short", "pos": 1.0,
                                                  "avg_px": 0.40, "mark_px": 0.27}])
        monkeypatch.setattr(ol.ot, "settle_expired_puts", lambda: [])
        ol.save_live_config(enabled=True, strategy=_call_cfg(dry_run=False))
        ol.run_once()
        assert len(_callstrat["buy"]) == 1
        assert _callstrat["buy"][0].inst_id == "SOL-USD_UM-260918-108-C"

    def test_defaults_include_call_params(self, _iso):
        s = ol.live_config()["strategy"]
        assert s["call_enabled"] is False and s["put_enabled"] is True
        assert s["take_profit_pct_call"] == 30
        assert s["max_calls_per_family"] == 1 and s["max_calls_total"] == 2
        assert s["allow_no_cost_basis"] is False
