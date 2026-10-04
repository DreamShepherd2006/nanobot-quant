"""补买（自动接货）单元测试 —— §33.43 Step 1（2026-09-28）。

覆盖：补买参数归一化 / 纯函数 ``evaluate_cover`` 的模式与超时兜底 / 执行层
``auto_cover_pending`` 的状态机（幂等、dry-run 只记意图、挂单、超时撤单+市价、
三个哨兵 fail-closed）。全程 mock SDK 与台账路径，不触网、不动真实持久卷。

幂等是这里的**第一风险点**（重复补买 = 双倍持仓），故专测：
已 pending/done/failed 的行绝不新建订单。
"""

import json
import time

import pytest

from nanobot_quant import okx_options_strategy as stg
from nanobot_quant import okx_options_trade as ot

HOUR = 3600.0


# ── fixtures ──────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离台账/参数路径 + mock 下单与取价；返回记录器。"""
    rec = {"cover_calls": [], "limit_calls": [], "cancel_calls": [],
           "balance": {"lot_sz": 0.001, "min_sz": 0.001, "quote_avail": 1000.0,
                       "tick_sz": 0.01},
           "ask": 120.0, "ord_state": {"status": "open", "avg_px": 0.0, "acc_fill_sz": 0.0}}
    monkeypatch.setattr(ot, "ledger_path", lambda: tmp_path / "ledger.json")
    monkeypatch.setattr(ot, "params_path", lambda: tmp_path / "okx_options_params.json")
    monkeypatch.setattr(ot, "_entry_account", lambda account: {"label": "A", "creds": {}})
    monkeypatch.setattr(ot, "spot_limits", lambda acct, inst, **kw: dict(rec["balance"]) or rec["balance"])
    monkeypatch.setattr(ot, "ticker_quote", lambda inst: {"ask": rec["ask"],
                                                          "last": rec["ask"]})
    monkeypatch.setattr(ot, "spot_cover", _fake_cover(rec))
    monkeypatch.setattr(ot, "spot_cover_limit", _fake_cover_limit(rec))
    monkeypatch.setattr(ot, "poll_order", lambda creds, inst, oid: rec["ord_state"])
    monkeypatch.setattr(ot, "cancel_order", lambda acct, *, inst_id, ord_id: rec["cancel_calls"].append(ord_id))

    def _write(rows):
        (tmp_path / "ledger.json").write_text(json.dumps(rows, ensure_ascii=False), "utf-8")

    def _read():
        p = tmp_path / "ledger.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else []

    rec["write_ledger"] = _write
    rec["read_ledger"] = _read
    return rec


def _fake_cover(rec):
    def _fn(account, *, spot_inst, base_qty=None, quote_amt=None, ref_id=""):
        rec["cover_calls"].append({"account": account, "spot_inst": spot_inst,
                                   "qty": base_qty, "ref_id": ref_id})
        return {"kind": "spot_cover", "inst_id": spot_inst, "sz": base_qty,
                "status": "filled", "filled_px": rec["ask"], "ord_id": "SP1"}
    return _fn


def _fake_cover_limit(rec):
    def _fn(account, *, spot_inst, px, base_qty, ref_id=""):
        rec["limit_calls"].append({"account": account, "spot_inst": spot_inst,
                                   "px": px, "qty": base_qty, "ref_id": ref_id})
        return {"kind": "spot_cover", "inst_id": spot_inst, "sz": base_qty,
                "status": "pending", "px": px, "ord_id": "LIM1"}
    return _fn


def _row(**kw):
    """一条「被行权接货」台账行（settled_itm）。"""
    row = {"id": "r1", "kind": "open_put", "status": "settled_itm",
           "inst_id": "SOL-USD_UM-260927-100-P", "sz": 1, "strike": 100.0,
           "settle_px": 98.0, "ts": "2026-09-27 16:00:00"}
    row.update(kw)
    return row


def _set_params(rec, **cover):
    p = ot.params_path()
    p.write_text(json.dumps({"cover": cover}, ensure_ascii=False), "utf-8")


# ── 参数层 ────────────────────────────────────────────────

def test_cover_params_defaults(env):
    cp = ot.cover_params()
    assert cp == {"auto": False, "mode": "limit", "discount_pct": 1.0,
                  "timeout_hours": 24.0}, "默认：关 / limit / 1% / 24h"


def test_cover_params_clamps_and_falls_back(env):
    _set_params(env, auto=True, mode="weird", discount_pct=999, timeout_hours=-3)
    cp = ot.cover_params()
    assert cp["mode"] == "limit" and cp["discount_pct"] == 50.0
    assert cp["timeout_hours"] == 0.1 and cp["auto"] is True


def test_save_cover_params_roundtrip(env):
    cp = ot.save_cover_params(auto=True, mode="signal", discount_pct=2.0,
                              timeout_hours=6)
    assert cp == {"auto": True, "mode": "signal", "discount_pct": 2.0,
                  "timeout_hours": 6.0}
    assert ot.cover_params() == cp, "写盘后可读回（归一化一致）"


# ── 纯函数 evaluate_cover ─────────────────────────────────

@pytest.mark.parametrize("state", ["waiting", "pending"])
def test_evaluate_cover_idempotent_states(state):
    d = stg.evaluate_cover(_row(cover_status=state), mode="limit", now=time.time())
    assert d["action"] == "skip" and "幂等" in d["reason"]


@pytest.mark.parametrize("state", ["done", "failed"])
def test_evaluate_cover_terminal_states_skip_even_after_timeout(state):
    d = stg.evaluate_cover(_row(cover_status=state,
                                cover_started_at=time.time() - 25 * HOUR),
                           mode="limit", timeout_hours=24.0, now=time.time())
    assert d["action"] == "skip" and "幂等" in d["reason"]


def test_evaluate_cover_requires_strike():
    d = stg.evaluate_cover(_row(strike=0), mode="limit", now=time.time())
    assert d["action"] == "skip" and "strike" in d["reason"]


def test_evaluate_cover_immediate():
    d = stg.evaluate_cover(_row(), mode="immediate", now=time.time())
    assert d["action"] == "market"


def test_evaluate_cover_limit_target_price():
    d = stg.evaluate_cover(_row(settle_px=100.0), mode="limit", discount_pct=1.0,
                           now=time.time())
    assert d["action"] == "limit" and d["target_px"] == pytest.approx(99.0)


def test_evaluate_cover_limit_without_settle_price_falls_back_market():
    d = stg.evaluate_cover(_row(settle_px=None), mode="limit", now=time.time())
    assert d["action"] == "market" and "结算价" in d["reason"]


def test_evaluate_cover_signal_wait_then_hit():
    now = time.time()
    d = stg.evaluate_cover(_row(), mode="signal", signal={"setup_buy": 4, "cd_buy": 2},
                           now=now)
    assert d["action"] == "wait_signal" and "未达阈值" in d["reason"]
    hit = stg.evaluate_cover(_row(), mode="signal", signal={"setup_buy": 9, "cd_buy": 0},
                             now=now)
    assert hit["action"] == "market" and "setup_buy=9" in hit["reason"]
    cd = stg.evaluate_cover(_row(), mode="signal", signal={"setup_buy": 0, "cd_buy": 13},
                            now=now)
    assert cd["action"] == "market" and "cd_buy=13" in cd["reason"]
    none = stg.evaluate_cover(_row(), mode="signal", signal=None, now=now)
    assert none["action"] == "wait_signal", "取数失败保守等待（不莽撞下单）"


def test_evaluate_cover_timeout_beats_mode_and_requires_cancel():
    started = time.time() - 25 * HOUR
    d = stg.evaluate_cover(_row(cover_status="pending", cover_ord_id="LIM9",
                                cover_started_at=started),
                           mode="signal", signal={"setup_buy": 0, "cd_buy": 0},
                           timeout_hours=24.0, now=time.time())
    assert d["action"] == "market" and d["cancel_ord"] is True, \
        "超时兜底优先于进行中幂等（否则挂单永远无法兜底成交）"
    assert "超时兜底" in d["reason"]
    d2 = stg.evaluate_cover(_row(cover_started_at=started, cover_ord_id="LIM9"),
                            mode="signal", signal={"setup_buy": 0, "cd_buy": 0},
                            timeout_hours=24.0, now=time.time())
    assert d2["action"] == "market" and d2["cancel_ord"] is True


def test_evaluate_cover_no_timeout_before_deadline():
    d = stg.evaluate_cover(_row(cover_started_at=time.time() - 2 * HOUR),
                           mode="limit", timeout_hours=24.0, now=time.time())
    assert d["action"] == "limit", "未到兜底时限不应转市价"


# ── 执行层 auto_cover_pending ─────────────────────────────

def test_pending_covers_filters_terminal_states(env):
    env["write_ledger"]([_row(id="a"), _row(id="b", cover_status="waiting"),
                         _row(id="c", cover_status="done"),
                         _row(id="d", cover_status="failed"),
                         {"id": "e", "kind": "open_call", "status": "settled_itm",
                          "inst_id": "SOL-USD_UM-261002-120-C", "sz": 1, "strike": 120.0},
                         {"id": "f", "kind": "open_put", "status": "open",
                          "inst_id": "SOL-USD_UM-261002-110-P", "sz": 1, "strike": 110.0}])
    ids = [r["id"] for r in ot.pending_covers()]
    assert ids == ["a", "b"], "只挑 put 被行权且未终态的行"


def test_auto_cover_disabled_is_noop(env):
    _set_params(env, auto=False, mode="limit")
    env["write_ledger"]([_row()])
    assert ot.auto_cover_pending("A", dry_run=False, now=time.time()) == []
    assert env["cover_calls"] == [] and env["limit_calls"] == []
    assert env["read_ledger"]()[0].get("cover_status") is None, "关闭时不动台账"


def test_auto_cover_dry_run_records_intent_only(env):
    _set_params(env, auto=True, mode="limit", discount_pct=1.0)
    env["write_ledger"]([_row(settle_px=100.0)])
    res = ot.auto_cover_pending("A", dry_run=True, now=time.time())
    assert len(res) == 1
    r = res[0]
    assert r["action"] == "limit" and r["status"] == "" and r["target_px"] == pytest.approx(99.0)
    assert r["qty"] == pytest.approx(0.1), "数量机械 = 1 张 × 0.1 SOL/张"
    assert "[dry-run]" in r["reason"]
    assert env["limit_calls"] == [] and env["cover_calls"] == [], "dry-run 不下单"
    assert env["read_ledger"]()[0].get("cover_status") is None, "dry-run 不写状态"


def test_auto_cover_limit_places_order_and_marks_pending(env):
    _set_params(env, auto=True, mode="limit", discount_pct=1.0, timeout_hours=24)
    env["write_ledger"]([_row(settle_px=100.0)])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "limit" and res[0]["status"] == "pending"
    assert env["limit_calls"][0]["px"] == pytest.approx(99.0)
    assert env["limit_calls"][0]["qty"] == pytest.approx(0.1)
    row = env["read_ledger"]()[0]
    assert row["cover_status"] == "pending" and row["cover_ord_id"] == "LIM1"
    assert row["cover_target_px"] == pytest.approx(99.0)
    assert row["cover_started_at"], "超时计时起点必须落盘"


def test_auto_cover_is_idempotent_while_order_open(env):
    _set_params(env, auto=True, mode="limit")
    env["write_ledger"]([_row(cover_status="pending", cover_ord_id="LIM1",
                              cover_started_at=time.strftime(
                                  "%Y-%m-%d %H:%M:%S",
                                  time.gmtime(time.time() - 3600)))])
    env["ord_state"] = {"status": "open", "avg_px": 0.0, "acc_fill_sz": 0.0}
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "skip" and "等待" in res[0]["reason"]
    assert env["limit_calls"] == [] and env["cover_calls"] == [], "挂单中绝不重复下单"


def test_auto_cover_marks_done_when_order_filled(env):
    _set_params(env, auto=True, mode="limit")
    env["write_ledger"]([_row(cover_status="pending", cover_ord_id="LIM1",
                              cover_started_at=time.time())])
    env["ord_state"] = {"status": "filled", "avg_px": 98.5, "acc_fill_sz": 0.1}
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "filled" and res[0]["status"] == "done"
    row = env["read_ledger"]()[0]
    assert row["cover_status"] == "done" and row["cover_qty"] == pytest.approx(0.1)
    assert row["cover_px"] == pytest.approx(98.5)


def test_auto_cover_timeout_cancels_then_markets(env):
    _set_params(env, auto=True, mode="limit", timeout_hours=24)
    old = time.time() - 25 * HOUR
    env["write_ledger"]([_row(cover_status="pending", cover_ord_id="LIM1",
                              cover_started_at=old)])
    env["ord_state"] = {"status": "open", "avg_px": 0.0, "acc_fill_sz": 0.0}
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert env["cancel_calls"] == ["LIM1"], "超时兜底必须先撤挂单（防双倍持仓）"
    assert env["cover_calls"] and res[0]["action"] == "filled"
    row = env["read_ledger"]()[0]
    assert row["cover_status"] == "done" and row["cover_qty"] == pytest.approx(0.1)


def test_auto_cover_immediate_market_path(env):
    _set_params(env, auto=True, mode="immediate")
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "filled"
    assert env["cover_calls"][0]["spot_inst"] == "SOL-USDC"
    assert env["cover_calls"][0]["qty"] == pytest.approx(0.1)
    assert env["cover_calls"][0]["ref_id"] == "r1", \
        "补买下单须带台账行 ref_id（§33.43 Step 2 成本锚 C 归因）"


def test_auto_cover_cash_guard_fails_closed(env):
    _set_params(env, auto=True, mode="immediate")
    env["balance"] = {"lot_sz": 0.001, "min_sz": 0.001, "quote_avail": 1.0}
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "skip" and "现金不足" in res[0]["reason"]
    assert env["cover_calls"] == [] and env["limit_calls"] == []


def test_auto_cover_notional_sentinel(env):
    _set_params(env, auto=True, mode="immediate")
    env["ask"] = 5000.0        # 异常价：预计金额远超接货名义×1.5
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "skip" and "哨兵拦截" in res[0]["reason"]
    assert env["cover_calls"] == []


def test_auto_cover_qty_below_min_is_rejected(env):
    _set_params(env, auto=True, mode="immediate")
    env["balance"] = {"lot_sz": 1.0, "min_sz": 1.0, "quote_avail": 1000.0}
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "skip" and "数量不合法" in res[0]["reason"]
    assert env["cover_calls"] == []


def test_auto_cover_market_failure_marks_failed(env, monkeypatch):
    _set_params(env, auto=True, mode="immediate")
    env["write_ledger"]([_row()])

    def _boom(*a, **k):
        raise RuntimeError("OKX 51008 余额不足")

    monkeypatch.setattr(ot, "spot_cover", _boom)
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["status"] == "failed" and "市价补买失败" in res[0]["reason"]
    row = env["read_ledger"]()[0]
    assert row["cover_status"] == "failed", "失败显式落盘（不静默）"
    assert ot.pending_covers() == [], "failed 不自动重试（退回人工按钮）"


def test_auto_cover_signal_mode_waits_without_signal(env):
    _set_params(env, auto=True, mode="signal")
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time(),
                                signal_fn=lambda base: {"setup_buy": 1, "cd_buy": 0})
    assert res[0]["action"] == "wait_signal"
    row = env["read_ledger"]()[0]
    assert row["cover_status"] == "waiting" and row["cover_started_at"]
    assert env["cover_calls"] == [] and env["limit_calls"] == []


def test_auto_cover_signal_mode_buys_when_hit(env):
    _set_params(env, auto=True, mode="signal")
    env["write_ledger"]([_row()])
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time(),
                                signal_fn=lambda base: {"setup_buy": 9, "cd_buy": 0})
    assert res[0]["action"] == "filled" and res[0]["status"] == "done"
    assert env["cover_calls"], "信号达标 → 市价补买"


def test_auto_cover_skips_legacy_manually_covered(env):
    """本功能上线前人工补买的行（无 cover_status，但有 filled spot_cover）→ 不重复补买。"""
    _set_params(env, auto=True, mode="immediate")
    env["write_ledger"]([
        _row(),
        {"id": "s1", "kind": "spot_cover", "status": "filled",
         "inst_id": "SOL-USD", "sz": "0.1", "filled_px": 98.0},
    ])
    assert ot.pending_covers() == [], "旧口径已补买的行必须排除（防双倍持仓）"
    assert ot.auto_cover_pending("A", dry_run=False, now=time.time()) == []
    assert env["cover_calls"] == []


def test_auto_cover_limit_fill_syncs_spot_row(env):
    """限价单成交后要把 spot_cover 台账行同步为 filled（台账 tab「已补买」口径）。"""
    _set_params(env, auto=True, mode="limit")
    env["write_ledger"]([
        _row(cover_status="pending", cover_ord_id="LIM1", cover_started_at=time.time()),
        {"id": "s1", "kind": "spot_cover", "status": "pending", "ord_id": "LIM1",
         "inst_id": "SOL-USD", "sz": "0.1"},
    ])
    env["ord_state"] = {"status": "filled", "avg_px": 98.5, "acc_fill_sz": 0.1}
    ot.auto_cover_pending("A", dry_run=False, now=time.time())
    spot = [r for r in env["read_ledger"]() if r.get("id") == "s1"][0]
    assert spot["status"] == "filled" and spot["filled_px"] == pytest.approx(98.5)


def test_auto_cover_signal_error_does_not_kill_round(env):
    _set_params(env, auto=True, mode="signal")
    env["write_ledger"]([_row()])

    def _boom(base):
        raise RuntimeError("K 线取数失败")

    res = ot.auto_cover_pending("A", dry_run=False, now=time.time(), signal_fn=_boom)
    assert res[0]["action"] == "wait_signal", "信号取数失败 → 保守等待（不莽撞下单）"
    assert "信号取数失败" in res[0]["reason"] or "无信号" in res[0]["reason"]


# ── 限价目标价的 tick 对齐（2026-09-30）──────────────────────

def test_floor_to_tick_never_rounds_up():
    """合成价向下取整：恰好在 tick 上不被浮点琢掉一档；无 tick 信息则原样透传。"""
    assert ot.floor_to_tick(117.8694, 0.01) == pytest.approx(117.86)
    assert ot.floor_to_tick(117.86, 0.01) == pytest.approx(117.86)
    assert ot.floor_to_tick(99.0, 0.01) == pytest.approx(99.0)
    assert ot.floor_to_tick(117.8694, 0.0) == pytest.approx(117.8694)
    assert ot.floor_to_tick(0.999, 1.0) == 0.0


def test_auto_cover_limit_px_aligned_to_tick(env):
    """目标价 = 结算价×0.99 是合成价，必须按 tickSz 取整后再下发。

    OKX 现货要求 px 是 tickSz 的整数倍；否则以「价格精度」拒单 →
    cover_status=failed 退回人工（2026-09-30 定位的实盘风险）。
    """
    _set_params(env, auto=True, mode="limit", discount_pct=1.0, timeout_hours=24)
    env["write_ledger"]([_row(settle_px=119.06)])        # 119.06 × 0.99 = 117.8694
    env["balance"]["tick_sz"] = 0.01
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert env["limit_calls"][0]["px"] == pytest.approx(117.86), "下单价必须 tick 对齐"
    assert res[0]["target_px"] == pytest.approx(117.86), "展示口径 = 下单价"
    row = env["read_ledger"]()[0]
    assert row["cover_target_px"] == pytest.approx(117.86)
    assert "117.86" in row["cover_note"]


def test_auto_cover_limit_px_fail_closed_when_floor_zero(env):
    """向下取整后不足一档（=0）→ fail-closed：不挂单、台账不动。"""
    _set_params(env, auto=True, mode="limit", discount_pct=1.0, timeout_hours=24)
    env["write_ledger"]([_row(settle_px=0.5)])           # 0.5 × 0.99 = 0.495
    env["balance"]["tick_sz"] = 1.0                      # floor → 0
    res = ot.auto_cover_pending("A", dry_run=False, now=time.time())
    assert res[0]["action"] == "skip" and env["limit_calls"] == []
    assert "取整" in res[0]["reason"]
    assert not env["read_ledger"]()[0].get("cover_status")
