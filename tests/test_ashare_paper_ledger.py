"""模拟账本（``ashare/paper_ledger.py`` + ``paper_strategy.py``）单测。

覆盖两类不变量：
1. **会计**：现金流、100% 全损担保、平仓盈亏、到期 OTM/ITM、净值一致性；
2. **规则**：选档过滤链、出场优先级、张数额度门。
"""

from __future__ import annotations

from datetime import date

import pytest

from nanobot_quant.ashare.paper_ledger import (
    EtfHolding,
    PaperLedger,
    fee_of_premium,
)
from nanobot_quant.ashare.paper_strategy import (
    PaperParams,
    can_open,
    days_to_expiry,
    is_expired,
    net_yield_pct,
    pick_put,
    should_close,
)

MULT = 10_000


def _ledger(cash: float = 500_000.0) -> PaperLedger:
    return PaperLedger(initial_cash=cash)


# ── 会计：开仓 ────────────────────────────────────────────


def test_sell_open_cashflow_and_collateral():
    led = _ledger()
    r = led.sell_open(code="1001.SHO", underlying="510050.SH", strike=2.80,
                      expiry="20261223", qty=1, bar_close=0.0900,
                      ts="2026-11-25T01:30:00Z")
    assert r["ok"] is True
    # 成交价吃 bid：0.09 − 1 tick
    assert led.shorts["1001.SHO"].entry_px == pytest.approx(0.0899)
    assert led.cash == pytest.approx(500_000 + 899.0 - 1.2)
    assert led.collateral_locked() == pytest.approx(2.80 * MULT)
    assert led.available_cash() == pytest.approx(500_897.8 - 28_000)


def test_sell_open_rejects_when_collateral_short():
    led = _ledger(cash=27_000.0)          # 担保需 28000，差 1000 → 必须拒绝
    r = led.sell_open(code="1001.SHO", underlying="510050.SH", strike=2.80,
                      expiry="20261223", qty=1, bar_close=0.09, ts="t")
    assert r["ok"] is False and "担保不足" in r["reason"]
    assert not led.shorts


def test_sell_open_rejects_duplicate_contract_and_bad_inputs():
    led = _ledger()
    assert led.sell_open(code="A", underlying="510050.SH", strike=2.8,
                         expiry="20261223", qty=1, bar_close=0.09, ts="t")["ok"]
    r = led.sell_open(code="A", underlying="510050.SH", strike=2.8,
                      expiry="20261223", qty=1, bar_close=0.09, ts="t2")
    assert r["ok"] is False and "重复开仓" in r["reason"]
    assert led.sell_open(code="B", underlying="510050.SH", strike=2.8,
                         expiry="20261223", qty=0, bar_close=0.09, ts="t")["ok"] is False
    assert led.sell_open(code="C", underlying="510050.SH", strike=2.8,
                         expiry="20261223", qty=1, bar_close=0.00005, ts="t")["ok"] is False


def test_buy_close_realized_matches_equity_delta():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261223", qty=1, bar_close=0.09, ts="t1")
    r = led.buy_close(code="A", bar_close=0.04, ts="t2", reason="take_profit")
    assert r["ok"] is True
    # (0.0899 − 0.0401) × 10000 − 1.2 − 1.7
    assert r["fill"]["realized"] == pytest.approx(495.1)
    assert led.equity() == pytest.approx(500_495.1)
    assert led.shorts == {}
    assert led.check_consistency()["ok"] is True


def test_buy_close_without_position_fails_closed():
    led = _ledger()
    r = led.buy_close(code="NOPE", bar_close=0.05, ts="t")
    assert r["ok"] is False and "无该合约持仓" in r["reason"]
    assert led.cash == pytest.approx(500_000.0)


# ── 会计：到期 ────────────────────────────────────────────


def test_settle_otm_collects_full_premium():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261125", qty=1, bar_close=0.09, ts="t1")
    r = led.settle_expiry(code="A", settle_spot=2.85, ts="t2")
    assert r["ok"] is True and r["fill"]["action"] == "settle_otm"
    assert r["fill"]["realized"] == pytest.approx(899.0 - 1.2)
    assert led.etf == {}
    assert led.equity() == pytest.approx(500_897.8)
    assert led.check_consistency()["ok"] is True


def test_settle_itm_assigns_etf_with_cost_basis():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261125", qty=1, bar_close=0.09, ts="t1")
    r = led.settle_expiry(code="A", settle_spot=2.70, ts="t2")
    assert r["ok"] is True and r["fill"]["action"] == "assign"
    h = led.etf["510050.SH"]
    assert h.qty == 1 * MULT
    # 成本 = (行权价 × 面值 + 行权费) ÷ 份数
    assert h.cost_basis == pytest.approx((28_000 + 5.6) / 10_000)
    # 行权费已进成本，realized 只算权利金腿
    assert r["fill"]["realized"] == pytest.approx(899.0 - 1.2)
    assert led.check_consistency()["ok"] is True


def test_settle_itm_twice_accumulates_cost_basis():
    led = _ledger()
    for code in ("A", "B"):
        led.sell_open(code=code, underlying="510050.SH", strike=2.80,
                      expiry="20261125", qty=1, bar_close=0.09, ts="t1")
    led.settle_expiry(code="A", settle_spot=2.70, ts="t2")
    led.settle_expiry(code="B", settle_spot=2.60, ts="t2")
    h = led.etf["510050.SH"]
    assert h.qty == 2 * MULT
    assert h.cost_basis == pytest.approx((28_000 + 5.6) / 10_000)   # 两次行权价相同、费用相同
    assert led.check_consistency()["ok"] is True


# ── 会计：估值 ────────────────────────────────────────────


def test_mark_short_drives_unrealized_and_equity():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261223", qty=1, bar_close=0.09, ts="t1")
    base = led.equity()
    led.mark_short("A", 0.045)
    assert led.unrealized() == pytest.approx((0.0899 - 0.045) * MULT)
    assert led.equity() - base == pytest.approx(led.unrealized())
    assert led.check_consistency()["ok"] is True


def test_mark_etf_after_assignment():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261125", qty=1, bar_close=0.09, ts="t1")
    led.settle_expiry(code="A", settle_spot=2.70, ts="t2")
    led.mark_etf("510050.SH", 2.75)
    h = led.etf["510050.SH"]
    assert h.unrealized() == pytest.approx((2.75 - h.cost_basis) * MULT)
    assert led.check_consistency()["ok"] is True


def test_snapshot_fields_present():
    led = _ledger()
    led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                  expiry="20261223", qty=1, bar_close=0.09, ts="t1")
    snap = led.snapshot()
    for k in ("cash", "available_cash", "collateral_locked", "equity",
              "realized", "unrealized", "return_pct", "shorts", "etf", "fills"):
        assert k in snap
    assert snap["fills"] == 1 and len(snap["shorts"]) == 1


def test_fee_helper_per_contract():
    assert fee_of_premium(0.09, 3) == pytest.approx(3.6)


# ── 规则：选档 ────────────────────────────────────────────


def _chain(**kw):
    base = {"code": "C1", "underlying": "510050.SH", "strike": 2.70,
            "expiry": "20261223", "bid": 0.03, "ask": 0.031}
    base.update(kw)
    return base


TODAY = date(2026, 11, 25)


def test_days_to_expiry_and_is_expired():
    assert days_to_expiry("20261223", TODAY) == 28
    assert is_expired({"expiry": "20261125"}, TODAY) is True
    assert is_expired({"expiry": "20261126"}, TODAY) is False


def test_pick_put_filters_chain():
    params = PaperParams()          # min_dte 6 / max_dte 35 / 距离 5%
    chain = [
        _chain(code="too_near", strike=2.80, expiry="20261223", bid=0.05),   # 距现价 1.7% → 拦
        _chain(code="too_soon", strike=2.70, expiry="20261127", bid=0.02),   # DTE 2 → 拦
        _chain(code="too_far", strike=2.70, expiry="20270324", bid=0.08),    # DTE 119 → 拦
        _chain(code="no_bid", strike=2.70, expiry="20261223", bid=0.0),      # 无买盘 → 拦
        _chain(code="ok_low", strike=2.70, expiry="20261223", bid=0.020),
        _chain(code="ok_high", strike=2.60, expiry="20261223", bid=0.015),
    ]
    pick = pick_put(chain, spot=2.85, today=TODAY, params=params)
    assert pick is not None
    # 净收益率 = (bid×10000 − 1.2) ÷ (strike×10000)
    y_low = net_yield_pct(2.70, 0.020)
    y_high = net_yield_pct(2.60, 0.015)
    assert pick["code"] == ("ok_low" if y_low >= y_high else "ok_high")
    assert pick["dte"] == 28
    assert pick["net_yield_pct"] == pytest.approx(round(max(y_low, y_high), 4))


def test_pick_put_returns_none_when_nothing_qualifies():
    assert pick_put([], spot=2.85, today=TODAY) is None
    assert pick_put([_chain(strike=2.84, bid=0.05)], spot=2.85, today=TODAY) is None


def test_pick_put_excludes_negative_net_premium():
    # bid 太薄、扣 1.2 元手续费后为负 → 必须排除（fail-closed）
    thin = _chain(strike=2.60, expiry="20261223", bid=0.00005)
    assert pick_put([thin], spot=2.85, today=TODAY) is None


# ── 规则：出场 ────────────────────────────────────────────


def test_should_close_take_profit_first():
    p = PaperParams(take_profit_pct=50.0, force_close_dte=5)
    short = {"entry_px": 0.10, "expiry": "20261223"}
    ok, why = should_close(short, mark_px=0.05, today=TODAY, params=p)
    assert ok is True and why.startswith("take_profit")
    ok2, _ = should_close(short, mark_px=0.06, today=TODAY, params=p)
    assert ok2 is False


def test_should_close_dte_force():
    p = PaperParams(take_profit_pct=50.0, force_close_dte=5)
    short = {"entry_px": 0.10, "expiry": "20261130"}      # DTE 5
    ok, why = should_close(short, mark_px=0.10, today=TODAY, params=p)
    assert ok is True and why.startswith("dte_force")
    short2 = {"entry_px": 0.10, "expiry": "20261201"}     # DTE 6
    assert should_close(short2, mark_px=0.10, today=TODAY, params=p)[0] is False


# ── 规则：参数自检 ───────────────────────────────────────


def test_params_reject_min_dte_not_above_force_close():
    with pytest.raises(ValueError, match="min_dte"):
        PaperParams(min_dte=5, force_close_dte=5)
    PaperParams(min_dte=6, force_close_dte=5)      # 合法：开仓窗口在强平线之上


# ── 规则：额度门 ─────────────────────────────────────────


def test_can_open_limits():
    p = PaperParams(max_per_underlying=2, max_total=3)
    shorts = [{"underlying": "510050.SH"}, {"underlying": "510050.SH"}]
    assert can_open(shorts, "510050.SH", p)[0] is False
    assert can_open(shorts, "588000.SH", p)[0] is True
    full = shorts + [{"underlying": "588000.SH"}]
    ok, why = can_open(full, "588000.SH", p)
    assert ok is False and "总额度" in why


# ── 集成：卖 → 到期 ITM → 接货 → 净值链 ────────────────


def test_full_cycle_sell_assign_net_value():
    led = _ledger()
    assert led.sell_open(code="A", underlying="510050.SH", strike=2.80,
                         expiry="20261125", qty=1, bar_close=0.09, ts="t1")["ok"]
    led.settle_expiry(code="A", settle_spot=2.70, ts="t2")
    # 接货当天按结算价计 ETF 市值
    eq = led.equity()
    assert eq == pytest.approx(500_000 + 899.0 - 1.2 - 28_000 - 5.6 + 27_000)
    # 与「行情继续跌到 2.60」对比：吃亏 1000 元/张
    led.mark_etf("510050.SH", 2.60)
    assert eq - led.equity() == pytest.approx(1_000.0)
    assert led.check_consistency()["ok"] is True
