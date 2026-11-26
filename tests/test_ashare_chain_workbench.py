"""期权链工作台（``ashare.chain_workbench``）单测。

覆盖：选档排序与 ``pick_put`` 逐位一致（单一来源）、往返后/打平回落口径、
两臂额度状态、按到期档过滤 + 「自动线选了别的档」提示、IV 分位查表。
纯函数 + monkeypatch，不打网络、不读真实磁盘。
"""

from __future__ import annotations

from datetime import date

import pytest

from nanobot_quant.ashare import chain_workbench as cwb
from nanobot_quant.ashare import iv_panel as ivp
from nanobot_quant.ashare import paper_strategy as ps

TODAY = date(2026, 11, 26)
EXP_20D = "20261216"      # 距 TODAY 20 天
EXP_40D = "20270105"      # 距 TODAY 40 天（超出默认 max_dte=35）


def _row(code, strike, expiry, bid, *, right="P", iv=0.25, delta=-0.2, dte=20,
         underlying="510050.SH"):
    return {"code": code, "right": right, "strike": strike, "expiry": expiry,
            "bid": bid, "ask": bid + 0.01, "mid": bid + 0.005, "iv": iv,
            "delta": delta, "dte": dte, "otm_pct": 5.5, "underlying": underlying}


def _p(**kw):
    base = dict(min_dte=6, max_dte=35, min_distance_pct=5.0, band_pct=12.0,
                max_per_underlying=3, max_total=6)
    base.update(kw)
    return ps.PaperParams(**base)


# ── 选档：rank_puts 是唯一实现，pick_put 取第一项 ─────────────

def test_rank_puts_sorted_desc_and_matches_pick_put():
    chain = [
        _row("A", 2.70, EXP_20D, 0.0300),
        _row("B", 2.60, EXP_20D, 0.0200),
        _row("C", 2.55, EXP_20D, 0.0900),   # 更虚值但权利金厚 → 收益率更高
    ]
    ranked = ps.rank_puts(chain, spot=2.85, today=TODAY, params=_p())
    assert [r["code"] for r in ranked][0] == "C"
    ys = [r["net_yield_pct"] for r in ranked]
    assert ys == sorted(ys, reverse=True)
    assert ps.pick_put(chain, spot=2.85, today=TODAY, params=_p())["code"] == "C"


def test_rank_puts_tie_keeps_input_order_like_old_pick():
    chain = [_row("X1", 2.70, EXP_20D, 0.0300), _row("X2", 2.70, EXP_20D, 0.0300)]
    ranked = ps.rank_puts(chain, spot=2.85, today=TODAY, params=_p())
    assert [r["code"] for r in ranked] == ["X1", "X2"]
    assert ps.pick_put(chain, spot=2.85, today=TODAY, params=_p())["code"] == "X1"


def test_rank_puts_filters_and_empty():
    assert ps.rank_puts([], spot=2.85, today=TODAY) == []
    assert ps.rank_puts([_row("A", 2.70, EXP_20D, 0.03)], spot=0, today=TODAY) == []
    # 太靠近平价（虚值不足 5%）→ 排除
    assert ps.rank_puts([_row("A", 2.84, EXP_20D, 0.03)], spot=2.85, today=TODAY) == []
    # 扣费后净权利金 <= 0 → 排除
    assert ps.rank_puts([_row("A", 2.70, EXP_20D, 0.00005)], spot=2.85, today=TODAY) == []
    # 到期窗口外 → 排除
    assert ps.rank_puts([_row("A", 2.70, EXP_40D, 0.03)], spot=2.85, today=TODAY) == []


# ── 候选表：口径与状态 ─────────────────────────────────────

def test_candidate_round_trip_and_breakeven_fields():
    rows = [_row("SOL-put", 2.70, EXP_20D, 0.0300)]
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D,
                               underlying="510050.SH")
    assert len(out["rows"]) == 1
    r = out["rows"][0]
    # 净权利金 = 300 − 1.2；担保额 = 27000；收益率分母是担保额
    assert r["net_premium"] == pytest.approx(298.8)
    assert r["collateral"] == pytest.approx(27000)
    assert r["net_yield_pct"] == pytest.approx(298.8 / 27000 * 100, abs=1e-3)
    assert r["annual_pct"] == pytest.approx(r["net_yield_pct"] * 365 / 20, rel=1e-6)
    # 往返后 = 再扣平仓费 1.7 + 1 tick
    assert r["round_trip_pct"] == pytest.approx((298.8 - 2.7) / 27000 * 100, abs=1e-6)
    # 打平回落 = 往返成本 ÷ 开仓权利金
    assert r["breakeven_drop_pct"] == pytest.approx(2.7 / 300 * 100, abs=1e-6)
    assert r["pick"] is True
    assert out["pick"] == "SOL-put"
    assert out["pick_other_expiry"] == ""


def test_candidates_only_puts_and_current_expiry():
    rows = [
        _row("P1", 2.70, EXP_20D, 0.0300),
        _row("C1", 3.00, EXP_20D, 0.0500, right="C"),   # call 不参与卖 put 候选
    ]
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D)
    assert [r["code"] for r in out["rows"]] == ["P1"]


def test_candidates_warn_when_pick_is_other_expiry():
    # 更远到期档的收益率更高 → 自动线会选它，但页面在近月 tab 上
    rows = [
        _row("NEAR", 2.70, EXP_20D, 0.0300, dte=20),
        _row("FAR", 2.55, "20261231", 0.0800, dte=35),
    ]
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D)
    assert out["pick"] == "FAR"
    assert out["pick_other_expiry"] == "20261231"
    assert [r["code"] for r in out["rows"]] == ["NEAR"]
    assert all(r["pick"] is False for r in out["rows"])


def test_arm_status_held_underlying_cap_and_total_cap():
    row = [_row("P1", 2.70, EXP_20D, 0.0300)]
    params = _p()
    # 已持有该合约
    out = cwb.build_candidates(row, 2.85, TODAY, params, expiry=EXP_20D,
                               underlying="510050.SH",
                               arm_shorts={"A": [{"code": "P1", "underlying": "510050.SH"}]})
    assert out["rows"][0]["arms"]["A"] == {"ok": False, "label": "已持有"}
    # 本标的已满 3 张
    three = [{"code": f"Z{i}", "underlying": "510050.SH"} for i in range(3)]
    out = cwb.build_candidates(row, 2.85, TODAY, params, expiry=EXP_20D,
                               underlying="510050.SH", arm_shorts={"A": three})
    assert out["rows"][0]["arms"]["A"]["ok"] is False
    assert "本标的满" in out["rows"][0]["arms"]["A"]["label"]
    # 总额满 6 张（其他标的）
    six = [{"code": f"Y{i}", "underlying": "510300.SH"} for i in range(6)]
    out = cwb.build_candidates(row, 2.85, TODAY, params, expiry=EXP_20D,
                               underlying="510050.SH", arm_shorts={"A": six})
    assert out["rows"][0]["arms"]["A"]["label"].startswith("总额满")
    # 空仓 → 可开
    out = cwb.build_candidates(row, 2.85, TODAY, params, expiry=EXP_20D,
                               underlying="510050.SH", arm_shorts={"A": []})
    assert out["rows"][0]["arms"]["A"] == {"ok": True, "label": "可开"}


def test_iv_percentile_wired_by_callback():
    rows = [_row("P1", 2.70, EXP_20D, 0.0300, iv=0.32)]
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D,
                               iv_pct_fn=lambda iv, dte: 87.5 if iv == 0.32 else None)
    assert out["rows"][0]["iv_pct"] == pytest.approx(87.5)
    # 回调返回 None（桶无样本）→ 保持 None，不写成 0
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D,
                               iv_pct_fn=lambda iv, dte: None)
    assert out["rows"][0]["iv_pct"] is None


def test_iv_percentile_uses_atm_reference_not_contract_iv():
    """分位基准必须是该到期档的 ATM put IV（历史桶里存的是 ATM）。

    拿深虚值合约自己的 IV 去比会因 skew 恒落在高位，页面会一直报「超贵」。
    """
    rows = [
        _row("ATM", 2.85, EXP_20D, 0.0500, iv=0.145, delta=-0.05),   # 参考档，不进候选
        _row("DEEP1", 2.70, EXP_20D, 0.0300, iv=0.165, delta=-0.13),
        _row("DEEP2", 2.60, EXP_20D, 0.0200, iv=0.185, delta=-0.07),
    ]
    seen = []
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D,
                               iv_pct_fn=lambda iv, dte: seen.append(iv) or 100.0)
    # 回调只收到一次 ATM 参考值，不是逐行合约 IV
    assert seen == [0.145]
    cand = {r["code"]: r for r in out["rows"]}
    assert set(cand) == {"DEEP1", "DEEP2"}
    assert {r["iv_pct"] for r in cand.values()} == {100.0}   # 同到期同值
    assert {round(r["atm_iv"], 3) for r in cand.values()} == {0.145}
    assert cand["DEEP1"]["iv"] == 0.165                      # 合约自身 IV 照旧展示


def test_atm_iv_ref_falls_back_to_nearest_strike_without_delta():
    puts = [
        {"code": "A", "expiry": EXP_20D, "strike": 2.60, "iv": 0.20, "delta": None},
        {"code": "B", "expiry": EXP_20D, "strike": 2.85, "iv": 0.145, "delta": None},
    ]
    assert cwb._atm_iv_ref(puts, EXP_20D, 2.85) == pytest.approx(0.145)
    # 有 Δ 的行优先于只靠 strike 距离的行
    puts2 = [
        {"code": "A", "expiry": EXP_20D, "strike": 2.60, "iv": 0.20, "delta": None},
        {"code": "B", "expiry": EXP_20D, "strike": 2.85, "iv": 0.145, "delta": -0.30},
    ]
    assert cwb._atm_iv_ref(puts2, EXP_20D, 2.85) == pytest.approx(0.145)


def test_candidates_otm_pct_uses_spot_basis():
    """虚值% 必须与 ``min_distance_pct`` 同源（现货），不能郿链行的 F 口径。"""
    rows = [_row("P1", 2.70, EXP_20D, 0.0300)]   # _row 自带 otm_pct=5.5（F 口径）
    out = cwb.build_candidates(rows, 2.85, TODAY, _p(), expiry=EXP_20D)
    r = out["rows"][0]
    assert r["otm_pct"] == pytest.approx((2.85 - 2.70) / 2.85 * 100, abs=1e-6)
    assert r["otm_pct"] != pytest.approx(5.5)
    assert out["params"]["otm_basis"] == "spot"
    assert out["params"]["iv_pct_basis"] == "atm_put"


def test_params_snapshot_exposes_effective_values():
    out = cwb.build_candidates([], 2.85, TODAY, _p(min_distance_pct=6.5))
    p = out["params"]
    assert p["min_dte"] == 6 and p["max_dte"] == 35
    assert p["min_distance_pct"] == 6.5
    assert p["fee_open"] == 1.2 and p["fee_close"] == 1.7
    assert p["mult"] == 10000


# ── IV 分位：一次读盘、批量查 ─────────────────────────────

def test_percentile_in_pure():
    buckets = [(0.0, 3.0, []), (8.0, 16.0, [0.10, 0.20, 0.30, 0.40])]
    assert ivp.percentile_in(buckets, 10, 0.25) == pytest.approx(50.0, abs=1e-6)
    assert ivp.percentile_in(buckets, 10, 0.05) == pytest.approx(0.0)
    assert ivp.percentile_in(buckets, 10, 0.99) == pytest.approx(100.0)
    # 桶无样本 / DTE 不落任何桶 / 输入缺失 → None（页面显示 —，不是 0）
    assert ivp.percentile_in(buckets, 1, 0.2) is None
    assert ivp.percentile_in(buckets, 50, 0.2) is None
    assert ivp.percentile_in(buckets, None, 0.2) is None
    assert ivp.percentile_in(buckets, 10, None) is None


def test_bucket_percentile_reads_cache(monkeypatch):
    payload = {"ok": True, "buckets": [
        {"lo": 8.0, "hi": 16.0, "_values": [0.10, 0.20, 0.30, 0.40]}]}
    monkeypatch.setattr(ivp, "_as_dirs", lambda src: ["d"])
    monkeypatch.setattr(ivp, "_cache_path", lambda dirs, undl: "cache.json")
    monkeypatch.setattr(ivp, "_read_cache", lambda p: payload)
    assert ivp.bucket_percentile("510050.SH", 10, 0.20) == pytest.approx(50.0, abs=1e-6)
    assert ivp.bucket_percentile("510050.SH", 1, 0.20) is None


def test_public_strips_internal_values_without_mutating():
    payload = {"ok": True, "buckets": [{"lo": 0.0, "hi": 3.0, "_values": [0.1],
                                        "days": 1}]}
    pub = ivp._public(payload)
    assert "_values" not in pub["buckets"][0]
    assert pub["buckets"][0]["days"] == 1
    # 原 payload 不受影响（缓存要留着给 bucket_percentile 用）
    assert payload["buckets"][0]["_values"] == [0.1]
