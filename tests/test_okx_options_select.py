"""C24 卖 put 候选选择（okx_options_select）单测。

覆盖：硬过滤（到期窗口 / 无买盘 / min_distance / delta 带）、净权利金与净收益率口径、
排序（净收益率 + delta 次键）、top_n 截断、参数钳制与保存校验、窗口放宽说明。
"""

import pytest

from nanobot_quant import okx_options_select as osel

FEE = 0.0003          # OPTION_FEE_RATE_TAKER


def _put(inst, bid, delta, ask=None, iv=70.0):
    return {"inst_id": inst, "bid": bid, "ask": ask if ask is not None else (bid or 0) * 1.2,
            "iv": iv, "delta": delta}


def _chain(spot=100.0, lot=0.1, groups=None):
    return {"family": "SOL-USD_UM", "spot": spot, "spot_inst": "SOL-USDT", "lot_coin": lot,
            "hv": {"hv30": 55.0}, "groups": groups or [], "chosen": [], "expiries": [],
            "price_note": "test"}


def _group(days, exp_ms, rows):
    return {"exp_ms": exp_ms, "date": "2026-09-20", "days": days, "rows": rows,
            "contracts": len(rows)}


def test_hard_filters_expiry_bid_distance_delta():
    chain = _chain(groups=[
        _group(2, 1, [{"strike": 95.0, "P": _put("260917-95-P", 0.5, -0.2)}]),       # 到期窗口外
        _group(5, 2, [
            {"strike": 97.0, "P": _put("260920-97-P", 0.6, -0.25)},                  # 距离不足（3% < 5%）
        ]),
        _group(5, 2, [
            {"strike": 96.0, "P": _put("260920-96-P", 0.0, -0.2)},                  # 无买盘
            {"strike": 94.0, "P": _put("260920-94-P", None, -0.2)},                 # bid 缺失
            {"strike": 93.0, "P": _put("260920-93-P", 0.4, -0.04)},                 # delta 低于下限
            {"strike": 92.0, "P": _put("260920-92-P", 0.4, -0.9)},                  # delta 偏高
            {"strike": 95.0, "P": _put("260920-95-P", 0.5, -0.25)},                 # ✅ 命中
        ]),
    ])
    r = osel.select_puts("SOL-USD_UM", chain=chain)
    assert [c["inst_id"] for c in r["candidates"]] == ["260920-95-P"]
    assert r["filtered"]["expiry"] == 1
    assert r["filtered"]["distance"] == 1
    assert r["filtered"]["no_bid"] == 2
    assert r["filtered"]["delta"] == 2


def test_net_premium_and_yield_use_bid_minus_fee():
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 95.0, "P": _put("260920-95-P", 0.5, -0.25)},
    ])])
    c = osel.select_puts("SOL-USD_UM", chain=chain)["candidates"][0]
    notional = 95.0 * 0.1
    assert c["notional_usd"] == pytest.approx(notional, rel=1e-9)
    assert c["premium_usd"] == pytest.approx(0.5 * 0.1, rel=1e-9)
    assert c["fee_usd"] == pytest.approx(notional * FEE, rel=1e-9)
    assert c["net_premium_usd"] == pytest.approx(0.05 - notional * FEE, rel=1e-9)
    assert c["net_yield_pct"] == pytest.approx((0.05 - notional * FEE) / notional * 100, rel=1e-3)


def test_net_non_positive_filtered_out():
    """扣手续费后无利可图的档必须排除（薄权利金防御）。"""
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 90.0, "P": _put("a", 0.001, -0.20)},   # 权利金 0.0001 < 费 0.0027
        {"strike": 95.0, "P": _put("b", 0.50, -0.25)},    # ✅
    ])])
    r = osel.select_puts("SOL-USD_UM", chain=chain)
    assert [c["inst_id"] for c in r["candidates"]] == ["b"]
    assert r["filtered"]["net"] == 1


def test_min_net_yield_filter():
    """min_net_yield_pct > 0 时低于该净收益率的档不出候选。"""
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 95.0, "P": _put("a", 0.50, -0.25)},    # 净收益率 ≈0.50%
        {"strike": 90.0, "P": _put("b", 0.05, -0.25)},    # ≈0.29%
    ])])
    r = osel.select_puts("SOL-USD_UM", chain=chain, selector={"min_net_yield_pct": 0.4})
    assert [c["inst_id"] for c in r["candidates"]] == ["a"]
    assert r["filtered"]["yield"] == 1


def test_delta_missing_is_kept_with_marker():
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 95.0, "P": _put("260920-95-P", 0.5, None)},
    ])])
    r = osel.select_puts("SOL-USD_UM", chain=chain)
    assert len(r["candidates"]) == 1
    assert r["candidates"][0]["delta"] is None
    assert r["candidates"][0]["delta_gap"] == 9.9      # 排序时置后


def test_sort_by_net_yield_then_delta_gap():
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 95.0, "P": _put("a", 0.50, -0.30)},   # yield 同 b、delta 离目标远（gap 0.05）
        {"strike": 95.0, "P": _put("b", 0.50, -0.25)},   # 同 yield、delta 贴近目标 → 应在前
        {"strike": 90.0, "P": _put("c", 0.30, -0.25)},   # yield 低
    ])])
    r = osel.select_puts("SOL-USD_UM", chain=chain)
    assert [c["inst_id"] for c in r["candidates"]] == ["b", "a", "c"]


def test_top_n_truncates():
    rows = [{"strike": 95.0 - i, "P": _put(f"x{i}", 0.5 - i * 0.01, -0.25)} for i in range(4)]
    chain = _chain(groups=[_group(5, 2, rows)])
    r = osel.select_puts("SOL-USD_UM", chain=chain, selector={"top_n": 2})
    assert len(r["candidates"]) == 2 == r["selector"]["top_n"]
    assert r["scanned"] == 4


def test_base_px_overrides_spot_for_distance():
    chain = _chain(spot=100.0, groups=[_group(5, 2, [
        {"strike": 97.0, "P": _put("260920-97-P", 0.6, -0.25)},
    ])])
    assert osel.select_puts("SOL-USD_UM", chain=chain)["candidates"] == []          # 97 > 95
    r = osel.select_puts("SOL-USD_UM", base_px=105.0, chain=chain)                  # 105×0.95 = 99.75
    assert len(r["candidates"]) == 1 and r["base_px"] == 105.0


def test_distance_filter_can_be_disabled():
    chain = _chain(groups=[_group(5, 2, [
        {"strike": 99.0, "P": _put("260920-99-P", 0.6, -0.45)},
    ])])
    r = osel.select_puts("SOL-USD_UM", chain=chain,
                         selector={"min_distance_pct": 0, "delta_min": 0, "delta_max": 0})
    assert len(r["candidates"]) == 1


def test_selector_params_clamps_bad_values():
    s = osel.selector_params({"min_distance_pct": "abc", "top_n": 999,
                              "expiry_min_days": 10, "expiry_max_days": 5,
                              "delta_min": 0.4, "delta_max": 0.1, "sort_by": "nope",
                              "min_net_yield_pct": 99})
    assert s["min_distance_pct"] == osel.DEFAULT_SELECTOR["min_distance_pct"]
    assert s["top_n"] == 20
    assert s["expiry_max_days"] == 10          # 上限被拉到不低于下限
    assert s["delta_max"] == 0.4               # 同上
    assert s["min_net_yield_pct"] == 10.0      # 越界钳制
    assert s["sort_by"] == osel.DEFAULT_SELECTOR["sort_by"]
    assert osel.DEFAULT_SELECTOR["delta_min"] == 0.05          # 默认下限已放宽
    assert osel.selector_params({})["delta_min"] == 0.05       # 未传 → 回落默认


def test_validate_selector_rejects_out_of_range():
    cleaned, err = osel.validate_selector({"min_distance_pct": 80})
    assert cleaned is None and "min_distance_pct" in err
    cleaned, err = osel.validate_selector({"top_n": 1.5})
    assert cleaned is None and "top_n" in err
    cleaned, err = osel.validate_selector({"sort_by": "bogus"})
    assert cleaned is None and "sort_by" in err
    cleaned, err = osel.validate_selector({"expiry_min_days": 9, "expiry_max_days": 3})
    assert cleaned is None and "到期" in err
    cleaned, err = osel.validate_selector({"delta_min": 0.5, "delta_max": 0.2})
    assert cleaned is None and "delta" in err


def test_validate_selector_defaults_and_ok():
    cleaned, err = osel.validate_selector({"min_distance_pct": 6, "top_n": 3,
                                           "expiry_min_days": 4, "expiry_max_days": 6,
                                           "delta_min": 0.2, "delta_max": 0.3,
                                           "min_net_yield_pct": 0.1, "sort_by": "apr"})
    assert err is None
    assert cleaned == {"min_distance_pct": 6.0, "top_n": 3, "expiry_min_days": 4.0,
                       "expiry_max_days": 6.0, "delta_min": 0.2, "delta_max": 0.3,
                       "min_net_yield_pct": 0.1, "sort_by": "apr"}
    # 缺省字段回落到默认值（min_net_yield_pct 默认 0 = 关闭）
    cleaned2, err2 = osel.validate_selector({"min_distance_pct": 5})
    assert err2 is None and cleaned2["min_net_yield_pct"] == 0.0


def test_window_relaxed_note_when_no_expiry_in_range(monkeypatch):
    """窗口内无在售到期 → 放宽为最近 3 个并在 note 说明。"""
    import nanobot_quant.okx_options_data as od

    monkeypatch.setattr(od, "list_expiries", lambda fam: [
        {"exp_ms": 1, "date": "2026-09-16", "days": 1},
        {"exp_ms": 2, "date": "2026-09-17", "days": 2},
    ])
    monkeypatch.setattr(od, "fetch_chain", lambda fam, expiries=None: _chain(
        groups=[_group(1, 1, [{"strike": 95.0, "P": _put("a", 0.5, -0.25)}])]))
    r = osel.select_puts("SOL-USD_UM")
    assert "放宽" in r["note"]
    assert r["candidates"] == []               # 放宽后仍受到期窗口过滤（1 天不在 3–7 天）


def test_locked_expiry_skips_window_filter():
    """锁定到期档（跟随链 tab）：只在该档里挑，哪怕它不在「天窗口」内。"""
    chain = _chain(groups=[
        _group(2, 111, [{"strike": 95.0, "P": _put("a", 0.5, -0.25)}]),     # 2 天，窗口外
        _group(20, 222, [{"strike": 95.0, "P": _put("b", 0.5, -0.25)}]),    # 20 天，窗口外
    ])
    r = osel.select_puts("SOL-USD_UM", chain=chain, exp_ms=111)
    assert r["expiry_mode"] == "locked"
    assert r["expiry_locked_ms"] == 111
    assert [c["inst_id"] for c in r["candidates"]] == ["a"]
    assert r["filtered"]["expiry"] == 1          # 另一个到期被排除


def test_locked_expiry_fetches_only_that_expiry(monkeypatch):
    """锁定档拉链时只请求该到期（跟随 tab），不回退天窗口。"""
    import nanobot_quant.okx_options_data as od

    seen = {}

    def _fc(fam, expiries=None):
        seen["expiries"] = list(expiries or [])
        return _chain(groups=[_group(4, 333, [{"strike": 95.0, "P": _put("z", 0.5, -0.25)}])])

    def _no_list(fam):
        raise AssertionError("锁定档不该查到期列表")

    monkeypatch.setattr(od, "fetch_chain", _fc)
    monkeypatch.setattr(od, "list_expiries", _no_list)
    r = osel.select_puts("SOL-USD_UM", exp_ms="333")
    assert seen["expiries"] == [333]
    assert r["expiry_mode"] == "locked"
    assert [c["inst_id"] for c in r["candidates"]] == ["z"]


def test_window_mode_default_unchanged():
    """不传 exp_ms（组合档）→ 保持原「天窗口」语义。"""
    chain = _chain(groups=[
        _group(2, 111, [{"strike": 95.0, "P": _put("a", 0.5, -0.25)}]),
        _group(5, 222, [{"strike": 94.0, "P": _put("b", 0.5, -0.25)}]),
    ])
    r = osel.select_puts("SOL-USD_UM", chain=chain)
    assert r["expiry_mode"] == "window"
    assert r["expiry_locked_ms"] is None
    assert [c["inst_id"] for c in r["candidates"]] == ["b"]


def test_bad_exp_ms_falls_back_to_window():
    """非法 exp_ms 值退化为组合档（底层防御；接口层已挡）。"""
    chain = _chain(groups=[_group(5, 222, [{"strike": 94.0, "P": _put("b", 0.5, -0.25)}])])
    r = osel.select_puts("SOL-USD_UM", chain=chain, exp_ms="abc")
    assert r["expiry_mode"] == "window"
    assert r["expiry_locked_ms"] is None
