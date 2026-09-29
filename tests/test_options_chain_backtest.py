"""资金链段回测单测（§33.43 Step 6 / C41b）—— 注入假数据，零网络。

覆盖：
* covered 覆盖张数的**单一来源**（实盘/回测共用 ``covered_sellable_sz``）
* 补买三模式（limit 触碰 / 超时兜底 / signal 等信号）+ 「建立当根 bar 不成交」
* 成本锚 C 回写（= (现金赔付 + 实际补买支出 + 费) ÷ (面值×张数)）
* 卖 call 支线：covered 容量门 / 成本锚门 / 保本选档 / 方向隔离止盈 / 现金结算
* KPI 与净值把接货现货当**资产**计入（不是白送的）
"""

from __future__ import annotations

import pytest

import test_options_driver as T
from nanobot_quant.backtest.options_driver import SimPosition, _exp_ms_of
from nanobot_quant.okx_options_trade import covered_sellable_sz

_LOT = 0.1                      # SOL-USD_UM 每张面值
_PUT = "SOL-USD_UM-260911-200-P"          # strike 200 >> 现价 → 深度 ITM
_CALL = "SOL-USD_UM-260911-80-C"          # strike 80 < 现价 → 卖 call 深度 ITM


def _selector() -> dict:
    return {"min_distance_pct": 5, "delta_min": 0.05, "delta_max": 0.35,
            "expiry_min_days": 0, "expiry_max_days": 3,
            "min_net_yield_pct": 0, "top_n": 5, "sort_by": "net_yield"}


def _driver(**kw):
    """构造注入了假数据的 driver；``opt_params`` 与默认值合并（不能重复传）。"""
    op = kw.pop("opt_params", None) or {}
    base = {"entry_setup": 9, "entry_countdown": 13, "selector": _selector()}
    base.update(op)
    d = T._driver(**kw)
    d.opt_params = base
    d._opt_cache = None            # T._driver() 已缓存过一份默认参数
    return d


def _put_pos(inst: str = _PUT, sz: int = 1, strike: float = 200.0):
    return SimPosition(inst, "SOL-USD_UM", strike, _exp_ms_of(inst, T._IDX[0]),
                       sz, 1.0, T._IDX[0], "buy9", _LOT)


def _call_pos(inst: str = _CALL, sz: int = 1, strike: float = 80.0,
              entry_px: float = 1.0):
    return SimPosition(inst, "SOL-USD_UM", strike, _exp_ms_of(inst, T._IDX[0]),
                       sz, entry_px, T._IDX[0], "covered", _LOT, opt_type="C")


def _call_chain(strike: float = 120.0, bid: float = 2.0, days: float = 2.0) -> dict:
    """给卖 call 用的一条链（右侧键 = C）。"""
    inst = f"SOL-USD_UM-260918-{int(strike)}-C"
    return {"ts": None, "spot": 96.9, "lot_coin": _LOT,
            "groups": [{"days": days,
                        "rows": [{"strike": strike,
                                  "C": {"inst_id": inst, "bid": bid, "mark_px": bid,
                                        "iv": 0.55, "delta": 0.25,
                                        "entry_reason": "covered"}}]}],
            "stats": {"total": 1, "kept": 1, "expired": 0,
                      "no_spot": 0, "no_iv": 0}}


# ── covered 覆盖张数：单一来源 ────────────────────────────────

def test_covered_sellable_sz_single_source():
    assert covered_sellable_sz(0.1, _LOT) == 1
    # 补买扣 0.1% 手续费后到货 0.0999 —— 按整数面值判会把自己刚补的货判成裸卖
    assert covered_sellable_sz(0.0999, _LOT) == 1
    assert covered_sellable_sz(0.05, _LOT) == 0
    assert covered_sellable_sz(0.2, _LOT) == 2
    assert covered_sellable_sz(0.0, _LOT) == 0
    assert covered_sellable_sz(float("nan"), _LOT) == 0
    assert covered_sellable_sz(0.5, 0.0) == 0


# ── 补买：limit 触碰 → 成本锚 C 回写 ──────────────────────────

def test_cover_limit_touch_fills_and_writes_cost_basis():
    d = _driver(cover_enabled=True, cover_mode="limit", cover_discount_pct=1.0)
    pos, fills = [_put_pos(sz=2)], []
    d.data.seek(T._IDX[40])
    cash = d._settle_expired(T._IDX[40], pos, fills, 1000.0)
    settle_row = fills[0]
    assert settle_row["side"] == "settle_itm"
    assert settle_row["cover_status"] == "waiting"
    assert len(d._covers) == 1
    task = d._covers[0]
    target = task.target_px
    payout = settle_row["payout_usd"]
    cash_after_settle = cash

    # 建立任务的那根 bar 不成交（结算发生在 bar 中间，拿整根 bar 的最低价占便宜）
    cash = d._tick_covers(T._IDX[40], T._SIG, fills, cash)
    assert cash == pytest.approx(cash_after_settle)
    assert len(fills) == 1 and len(d._covers) == 1
    assert d._spot_qty == 0.0

    # 后续 bar 价格继续下跌 → 最低价触到限价目标 → 成交
    ts = T._IDX[41]
    d.data.seek(ts)
    cash = d._tick_covers(ts, T._SIG, fills, cash)
    cover = fills[-1]
    assert cover["side"] == "cover" and cover["mode"] == "limit"
    assert cover["avg_px"] == pytest.approx(target, rel=1e-9)
    qty = _LOT * 2
    assert cover["qty"] == pytest.approx(qty * 0.999, rel=1e-9)
    assert d._spot_qty == pytest.approx(qty * 0.999, rel=1e-9)
    # 成本锚 C =（现金赔付 + 实际补买支出 + 费）÷（面值×张数）
    spend = qty * target
    expect_c = (payout + spend + spend * 0.001) / qty
    assert cover["cost_basis"] == pytest.approx(round(expect_c, 4), rel=1e-9)
    assert settle_row["cost_basis"] == pytest.approx(round(expect_c, 4), rel=1e-9)
    assert settle_row["cover_status"] == "done"
    assert d._covers == [] and len(d._cost_bases) == 1
    assert cash == pytest.approx(cash_after_settle - spend, rel=1e-9)


def test_cover_limit_timeout_falls_back_to_market():
    """目标价永远够不到 → 超时兜底转市价（撤销挂单语义由纯函数给出）。"""
    d = _driver(cover_enabled=True, cover_mode="limit", cover_discount_pct=50.0,
                cover_timeout_hours=0.1)
    pos, fills = [_put_pos()], []
    d.data.seek(T._IDX[40])
    cash = d._settle_expired(T._IDX[40], pos, fills, 1000.0)
    task = d._covers[0]
    assert task.target_px == pytest.approx(task.settle_px * 0.5, rel=1e-9)

    ts = T._IDX[41]                    # 1 bar = 1h > 超时 0.1h
    d.data.seek(ts)
    spot = d.data.price_of()
    cash = d._tick_covers(ts, T._SIG, fills, cash)
    cover = fills[-1]
    assert cover["side"] == "cover"
    assert cover["avg_px"] == pytest.approx(spot, rel=1e-9)   # 市价 = 当根收盘
    assert "超时兜底" in (task.settle_row.get("cover_reason") or "")
    assert d._covers == []


def test_cover_signal_waits_for_td_exhaustion(monkeypatch):
    d = _driver(cover_enabled=True, cover_mode="signal")
    pos, fills = [_put_pos()], []
    d.data.seek(T._IDX[40])
    cash = d._settle_expired(T._IDX[40], pos, fills, 1000.0)

    ts = T._IDX[41]
    d.data.seek(ts)
    cash = d._tick_covers(ts, {"setup_buy": 1, "cd_buy": 0}, fills, cash)
    assert len(d._covers) == 1 and d._spot_qty == 0.0     # 未达阈值 → 继续等

    cash = d._tick_covers(T._IDX[42], {"setup_buy": 9, "cd_buy": 0}, fills, cash)
    assert d._covers == []
    assert fills[-1]["side"] == "cover"


def test_cover_disabled_marks_row_without_spot():
    d = _driver(cover_enabled=False)
    pos, fills = [_put_pos()], []
    d.data.seek(T._IDX[40])
    d._settle_expired(T._IDX[40], pos, fills, 1000.0)
    assert fills[0]["cover_status"] == "manual"
    assert d._covers == [] and d._spot_qty == 0.0


# ── 卖 call 支线 ────────────────────────────────────────────

def test_call_entry_fail_closed_without_spot_or_cost_basis():
    d = _driver()
    d.data.seek(T._IDX[40])
    d.data.chain_dict_at = lambda ts, **kw: _call_chain()
    fills: list[dict] = []
    # ① 无现货 → 无 covered 容量
    assert d._try_call_entry(T._IDX[40], [], fills, 100.0) == pytest.approx(100.0)
    assert fills == []
    # ② 有现货但无成本锚 C（且未允许无锚）→ fail-closed
    d._spot_qty = 0.1
    assert d._try_call_entry(T._IDX[40], [], fills, 100.0) == pytest.approx(100.0)
    assert fills == [] and any("无成本锚" in s for s in d.skips)


def test_call_entry_opens_covered_position_and_pays_premium():
    d = _driver()
    d.data.seek(T._IDX[40])
    d.data.chain_dict_at = lambda ts, **kw: _call_chain(strike=120.0, bid=2.0)
    d._spot_qty = 0.1
    d._cost_bases = [{"inst_id": _PUT, "sz": 1, "cost_basis": 100.0,
                      "ts": str(T._IDX[0])}]
    positions: list = []
    fills: list[dict] = []
    cash = d._try_call_entry(T._IDX[40], positions, fills, 100.0)
    assert len(positions) == 1
    p = positions[0]
    assert p.opt_type == "C" and p.collateral == 0.0
    assert p.inst_id == "SOL-USD_UM-260918-120-C"
    f = fills[-1]
    assert f["side"] == "sell_open" and f["opt_type"] == "C"
    premium = 2.0 * _LOT * 1
    fee = min(120.0 * _LOT * 0.0003, 0.07 * premium)
    assert cash == pytest.approx(100.0 + premium - fee, rel=1e-9)
    assert any("去重门" in n for n in d.notes)      # 回测里退化为恒放行的留痕


def test_call_entry_respects_covered_capacity_minus_open_calls():
    d = _driver()
    d.data.seek(T._IDX[40])
    d.data.chain_dict_at = lambda ts, **kw: _call_chain()
    d._spot_qty = 0.1                       # 只够 1 张
    d._cost_bases = [{"inst_id": _PUT, "sz": 1, "cost_basis": 100.0,
                      "ts": str(T._IDX[0])}]
    positions = [_call_pos()]               # 已在仓 1 张 call
    fills: list[dict] = []
    before = 100.0
    assert d._try_call_entry(T._IDX[40], positions, fills, before) == pytest.approx(before)
    assert fills == []
    assert any("covered 容量不足" in s for s in d.skips)


def test_call_exit_uses_its_own_tp_line(monkeypatch):
    """§33.39 方向隔离：put 的 50% 线不得平掉 call 仓，call 用自己的 30%。"""
    d = _driver(cover_enabled=False, call_enabled=True, tp_call_pct=30.0)
    d.data.seek(T._IDX[40])
    put, call = _put_pos(), _call_pos(entry_px=1.0)
    d.data.premium_of = lambda inst, ts: 0.6      # 两笔都回落 40%
    positions = [put, call]
    fills: list[dict] = []
    d._check_exits(T._IDX[40], positions, fills, 100.0)
    assert [f["opt_type"] for f in fills if f["side"] == "close"] == ["C"]
    assert put in positions and call not in positions


def test_call_settlement_pays_cash_and_keeps_spot():
    d = _driver(cover_enabled=False)
    pos, fills = [_call_pos(strike=80.0, sz=2)], []
    d._spot_qty = 0.2                     # covered：现货在手
    d.data.seek(T._IDX[40])
    cash = d._settle_expired(T._IDX[40], pos, fills, 100.0)
    row = fills[0]
    assert row["opt_type"] == "C" and row["side"] == "settle_itm"
    assert row["settle_px"] > 80.0
    payout = (row["settle_px"] - 80.0) * _LOT * 2
    assert row["payout_usd"] == pytest.approx(round(payout, 6), rel=1e-9)
    assert cash == pytest.approx(100.0 - payout, rel=1e-9)
    assert d._spot_qty == pytest.approx(0.2)          # 现金结算不交币
    assert "现货保留" in row["reason"]


def test_call_otm_settlement_keeps_premium_and_spot():
    d = _driver(cover_enabled=False)
    pos, fills = [_call_pos(inst="SOL-USD_UM-260911-300-C", strike=300.0)], []
    d._spot_qty = 0.1
    d.data.seek(T._IDX[40])
    cash = d._settle_expired(T._IDX[40], pos, fills, 100.0)
    assert fills[0]["side"] == "settle_otm"
    assert cash == pytest.approx(100.0)
    assert d._spot_qty == pytest.approx(0.1)


# ── KPI / 净值：接货现货算资产 ─────────────────────────────────

def test_run_kpi_counts_spot_as_asset_and_reports_chain():
    d = _driver(cover_enabled=True, cover_mode="limit", call_enabled=False)
    out = d.run()
    chain = out["chain"]
    assert chain["cover_enabled"] is True and chain["cover_mode"] == "limit"
    assert chain["call_enabled"] is False
    assert chain["settle_window_min"] == 30
    k = out["kpi"]
    for key in ("premium_put_usd", "premium_call_usd", "cover_spend_usd",
                "spot_qty", "spot_value_usd", "cost_basis_max"):
        assert key in k, key
    assert out["spot"]["qty"] == pytest.approx(k["spot_qty"])
    # 净值 = 现金 − 期末持仓负债 + 现货市值（现货是资产）
    assert k["final_net_usd"] == pytest.approx(
        out["cash"] - k["open_mark_value_usd"] + k["spot_value_usd"], abs=0.01)
    assert out["tp_pct_call"] is None       # 关掉的支线不报止盈线


# ── 参数网格（参数裁决入口）───────────────────────────────────

def test_run_grid_reuses_one_data_source_and_renders_markdown():
    """网格：共享一份预取数据、逐组换参数；输出带口径提示的 markdown 表。"""
    from nanobot_quant.backtest.options_grid import render_grid_markdown, run_grid

    d = _driver()
    grid = run_grid("SOL-USD_UM", days=2, timestep="1H", td_bars=60,
                    initial_cash=100.0, data_source=d.data,
                    iv_gates=(0.0, 50.0), expiries=((0.0, 3.0),),
                    tps=(50.0,), chain_modes=("off", "full"),
                    min_net_yields=(0.0,), log=lambda *a, **k: None)
    assert len(grid["rows"]) == 4                     # 2 IV × 1 档 × 1 TP × 1 净 × 2 链
    labels = {r["label"] for r in grid["rows"]}
    assert "IV0/档0-3/TP50/净0/仅put" in labels
    assert "IV50/档0-3/TP50/净0/全链" in labels
    for r in grid["rows"]:
        assert r["roi_pct"] is not None and r["fills"] is not None
    md = render_grid_markdown(grid)
    assert "## 期权回测参数网格" in md
    assert "| 组合 | ROI% |" in md
    assert "仅put" in md and "全链" in md
    assert "方向性初判" in md          # 顺风样本的口径提示不能省
