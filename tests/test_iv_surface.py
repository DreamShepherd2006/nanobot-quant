"""iv_surface 单测 —— 全部离线（纯数学，无网络、无 pandas）。

锁定的核心行为：
* log-moneyness 插值（不是 strike 线性）、范围外常数延拓（不外推出负 IV）
* 同 strike 多点取最近、保鲜期过滤、只取「截至 ts」的点
* **往返一致性**：真实 IV → BS 定价 → 反解 → 还原同一个 IV
* fail-closed：缺 spot / 缺元数据 / 已到期 → 跳过并计数，不猜
"""

from __future__ import annotations

import math

import pytest

from nanobot_quant.bs_pricing import bs_delta, bs_price, years_to_expiry
from nanobot_quant.iv_surface import (
    IVPoint,
    IVSurface,
    Smile,
    fit_smile,
    iv_points_from_trades,
)

TS0 = 1_789_000_000_000          # 任意基准毫秒
DAY_MS = 86_400_000


def _meta(strike: float, exp_ms: int, right: str = "P") -> dict:
    return {"strike": strike, "exp_ms": exp_ms, "right": right}


def _pt(ts: int, strike: float, iv: float, exp_ms: int = TS0 + 5 * DAY_MS,
        inst: str = "", right: str = "P") -> IVPoint:
    return IVPoint(ts_ms=ts, inst_id=inst or f"X-{strike}-{right}", strike=strike,
                   exp_ms=exp_ms, right=right, iv=iv, price=1.0, spot=100.0)


# ─────────────────────────── Smile 插值 ───────────────────────────

def test_smile_single_point_is_constant():
    sm = Smile(strikes=[100.0], ivs=[0.65], forward=100.0)
    assert sm.iv(50.0) == pytest.approx(0.65)
    assert sm.iv(200.0) == pytest.approx(0.65)


def test_smile_empty_returns_none():
    assert Smile().iv(100.0) is None
    assert Smile().empty is True


def test_smile_endpoints_hit_exact_points():
    sm = Smile(strikes=[90.0, 100.0, 110.0], ivs=[0.75, 0.60, 0.55], forward=100.0)
    assert sm.iv(90.0) == pytest.approx(0.75)
    assert sm.iv(100.0) == pytest.approx(0.60)
    assert sm.iv(110.0) == pytest.approx(0.55)


def test_smile_interpolates_between_points():
    sm = Smile(strikes=[90.0, 100.0], ivs=[0.80, 0.60], forward=100.0)
    mid = sm.iv(95.0)
    assert 0.60 < mid < 0.80


def test_smile_interpolates_in_log_moneyness_not_strike():
    """K=95 在 (90,100) 之间 —— 按 strike 线性给 0.70，按 log-moneyness 不是。"""
    sm = Smile(strikes=[90.0, 100.0], ivs=[0.80, 0.60], forward=100.0)
    x, x0, x1 = math.log(95 / 100), math.log(90 / 100), math.log(100 / 100)
    expected = 0.80 + (x - x0) / (x1 - x0) * (0.60 - 0.80)
    assert sm.iv(95.0) == pytest.approx(expected, abs=1e-12)
    assert sm.iv(95.0) != pytest.approx(0.70, abs=1e-6)   # 不是 strike 线性


def test_smile_flat_extrapolation_never_negative():
    """范围外取最近点 —— 线性外推会在深虚端算出负 IV，而那是卖 put 最关心的区域。"""
    sm = Smile(strikes=[90.0, 100.0], ivs=[0.80, 0.60], forward=100.0)
    assert sm.iv(50.0) == pytest.approx(0.80)      # 左侧延拓取左端
    assert sm.iv(300.0) == pytest.approx(0.60)     # 右侧延拓取右端
    assert sm.iv(0.01) == pytest.approx(0.80)
    assert sm.iv(300.0) > 0


def test_smile_rejects_bad_strike():
    sm = Smile(strikes=[90.0, 100.0], ivs=[0.8, 0.6], forward=100.0)
    assert sm.iv(0) is None
    assert sm.iv(-5) is None


# ─────────────────────────── fit_smile ───────────────────────────

def test_fit_smile_keeps_latest_per_strike():
    pts = [_pt(100, 95.0, 0.70), _pt(200, 95.0, 0.66), _pt(150, 100.0, 0.60)]
    sm = fit_smile(pts, forward=100.0)
    assert sm.strikes == [95.0, 100.0]
    assert sm.iv(95.0) == pytest.approx(0.66)      # 取 ts 更新的那个
    assert sm.n_points == 2


def test_fit_smile_empty_returns_none():
    assert fit_smile([], forward=100.0) is None


def test_fit_smile_uses_mid_strike_when_forward_missing():
    sm = fit_smile([_pt(100, 90.0, 0.7), _pt(100, 110.0, 0.5)], forward=0.0)
    assert sm.forward == 110.0                      # 中位（索引 1）


# ─────────────────── iv_points_from_trades ───────────────────

def test_iv_points_recover_true_iv():
    """往返：真实 IV → BS 定价 → 反解 → 还原同一个 IV。"""
    spot, k, iv, right = 100.0, 95.0, 0.65, "P"
    exp = TS0 + 5 * DAY_MS
    t = years_to_expiry(TS0, exp)
    px = bs_price(spot, k, t, iv, 0.0, right)
    inst = "SOL-USD_UM-260918-95-P"
    pts = iv_points_from_trades(
        [{"instrument_name": inst, "created_time": str(TS0), "price": str(px)}],
        spot_at=lambda ts: spot, contracts={inst: _meta(k, exp, right)})
    assert len(pts) == 1
    assert pts[0].iv == pytest.approx(iv, abs=1e-4)
    assert pts[0].strike == k and pts[0].right == right


def test_iv_points_skip_when_spot_missing_and_count():
    inst = "SOL-USD_UM-260918-95-P"
    rejects: list[tuple[str, str]] = []
    pts = iv_points_from_trades(
        [{"instrument_name": inst, "created_time": str(TS0), "price": "1.0"}],
        spot_at=lambda ts: None,
        contracts={inst: _meta(95.0, TS0 + 5 * DAY_MS)},
        on_reject=lambda why, i: rejects.append((why, i)))
    assert pts == []
    assert rejects == [("spot", inst)]


def test_iv_points_skip_unknown_contract():
    rejects: list[tuple[str, str]] = []
    pts = iv_points_from_trades(
        [{"instrument_name": "UNKNOWN", "created_time": str(TS0), "price": "1.0"}],
        spot_at=lambda ts: 100.0, contracts={},
        on_reject=lambda why, i: rejects.append((why, i)))
    assert pts == [] and rejects == [("meta", "UNKNOWN")]


def test_iv_points_skip_expired():
    inst = "SOL-USD_UM-260915-95-P"
    exp = TS0 - DAY_MS                       # 已到期
    rejects: list[tuple[str, str]] = []
    pts = iv_points_from_trades(
        [{"instrument_name": inst, "created_time": str(TS0), "price": "1.0"}],
        spot_at=lambda ts: 100.0, contracts={inst: _meta(95.0, exp)},
        on_reject=lambda why, i: rejects.append((why, i)))
    assert pts == [] and rejects == [("expired", inst)]


def test_iv_points_sorted_by_time():
    inst = "X"
    exp = TS0 + 5 * DAY_MS
    t = years_to_expiry(TS0, exp)
    px = bs_price(100.0, 95.0, t, 0.6, 0.0, "P")
    rows = [{"instrument_name": inst, "created_time": str(TS0 + 1000), "price": str(px)},
            {"instrument_name": inst, "created_time": str(TS0), "price": str(px)}]
    pts = iv_points_from_trades(rows, spot_at=lambda ts: 100.0,
                               contracts={inst: _meta(95.0, exp)})
    assert [p.ts_ms for p in pts] == [TS0, TS0 + 1000]


# ─────────────────────────── IVSurface ───────────────────────────

def _surface() -> IVSurface:
    exp = TS0 + 5 * DAY_MS
    contracts = {
        "A": _meta(90.0, exp), "B": _meta(95.0, exp),
        "C": _meta(100.0, exp), "D": _meta(105.0, exp),
        "FAR": _meta(95.0, TS0 + 30 * DAY_MS),
    }
    s = IVSurface(contracts)
    s.add_points([
        _pt(TS0 - 1000, 90.0, 0.80, exp, inst="A"),
        _pt(TS0 - 1000, 100.0, 0.60, exp, inst="C"),
        _pt(TS0 - 1000, 95.0, 0.70, TS0 + 30 * DAY_MS, inst="FAR"),
    ])
    return s


def test_surface_interpolates_missing_strike_from_smile():
    """B(95) 自己没有成交，靠微笑插值拿到 IV。"""
    s = _surface()
    iv = s.iv_for("B", TS0)
    assert iv is not None and 0.60 < iv < 0.80


def test_surface_prefers_own_observation():
    s = _surface()
    assert s.iv_for("A", TS0) == pytest.approx(0.80)
    assert s.iv_for("C", TS0) == pytest.approx(0.60)


def test_surface_ignores_points_from_the_future():
    """ts 之前的点才算 —— 前视偏差会让回测虚高。"""
    s = _surface()
    assert s.smile_at(TS0 - 2 * DAY_MS, TS0 + 5 * DAY_MS, forward=100.0).empty is True
    assert s.smile_at(TS0, TS0 + 5 * DAY_MS, forward=100.0).empty is False


def test_surface_staleness_filter():
    s = IVSurface({"A": _meta(90.0, TS0 + 5 * DAY_MS)}, max_iv_staleness_ms=DAY_MS)
    s.add_points([_pt(TS0 - 3 * DAY_MS, 90.0, 0.8, TS0 + 5 * DAY_MS, inst="A")])
    assert s.smile_at(TS0, TS0 + 5 * DAY_MS, forward=100.0).empty is True
    assert s.stats["stale"] >= 0
    s2 = IVSurface({"A": _meta(90.0, TS0 + 5 * DAY_MS)}, max_iv_staleness_ms=10 * DAY_MS)
    s2.add_points([_pt(TS0 - 3 * DAY_MS, 90.0, 0.8, TS0 + 5 * DAY_MS, inst="A")])
    assert s2.smile_at(TS0, TS0 + 5 * DAY_MS, forward=100.0).empty is False


def test_surface_price_matches_bs_from_smile_iv():
    s = _surface()
    got = s.price_at("B", TS0, 100.0)
    iv = s.iv_for("B", TS0)
    t = years_to_expiry(TS0, TS0 + 5 * DAY_MS)
    assert got == pytest.approx(bs_price(100.0, 95.0, t, iv, 0.0, "P"))


def test_surface_delta_matches_bs_and_is_negative_for_put():
    s = _surface()
    d = s.delta_at("B", TS0, 100.0)
    iv = s.iv_for("B", TS0)
    t = years_to_expiry(TS0, TS0 + 5 * DAY_MS)
    assert d == pytest.approx(bs_delta(100.0, 95.0, t, iv, 0.0, "P"))
    assert d < 0                              # put delta 恒负


def test_surface_unknown_contract_returns_none():
    s = _surface()
    assert s.price_at("NOPE", TS0, 100.0) is None
    assert s.delta_at("NOPE", TS0, 100.0) is None
    assert s.iv_for("NOPE", TS0) is None
    assert s.stats["no_point"] >= 1


def test_surface_expired_contract_returns_none():
    exp = TS0 - DAY_MS
    s = IVSurface({"OLD": _meta(95.0, exp)})
    s.add_points([_pt(TS0 - 2 * DAY_MS, 95.0, 0.7, exp, inst="OLD")])
    assert s.price_at("OLD", TS0, 100.0) is None
    assert s.stats["no_t"] >= 1


def test_surface_smile_cache_returns_same_object():
    s = _surface()
    a = s.smile_at(TS0, TS0 + 5 * DAY_MS, forward=100.0)
    b = s.smile_at(TS0, TS0 + 5 * DAY_MS, forward=100.0)
    assert a is b


# ═══════════ 反解质量门 + 虚值侧优先（2026-09-19）═══════════
#
# 背景：ETH-USD_UM-260904-2050-C（实值 17%、剩 9 天）成交价 536.6 反解出
# **202.5%**；该 strike 上没有 put 成交，这条 202% 就被拿去给同 strike 的
# 深虚 put 定价，回测报价虚高 26 倍（ETH 那次 5.01% ROI 里 62% 权利金来自此处）。
#
# 下面三条锁定最基本的行为：
# ① 病态观测被门挡下（复现 ETH 那笔）
# ② 长到期的深实值不该被误伤（BTC 那笔 51.7% 正常）
# ③ 落地到微笑：被挡下后深虚 put 改由邻近 strike 外推，拿不到 202%

# 真实参数（2026-08-26 19:55 UTC 的归档成交）
_ETH_SPOT, _ETH_K, _ETH_PX, _ETH_DAYS = 2470.80, 2050.0, 536.60, 9


def _trade(inst: str, ts: int, px: float) -> dict:
    return {"instrument_name": inst, "created_time": str(ts), "price": str(px)}


def test_iv_points_rejects_deep_itm_call_eth_case():
    """复现：ETH 那笔深实值 call 必须被质量门挡下。"""
    exp = TS0 + _ETH_DAYS * DAY_MS
    inst = "ETH-USD_UM-260904-2050-C"
    rejects: list[tuple[str, str]] = []
    pts = iv_points_from_trades(
        [_trade(inst, TS0, _ETH_PX)], spot_at=lambda ts: _ETH_SPOT,
        contracts={inst: _meta(_ETH_K, exp, "C")},
        on_reject=lambda w, i: rejects.append((w, i)))
    assert pts == []
    assert len(rejects) == 1 and rejects[0][1] == inst
    assert rejects[0][0] in ("extreme_iv", "deep_itm")


def test_iv_points_eth_case_is_what_the_gate_catches():
    """关掉两道门就是原来那个病态值 —— 证明病态来自反解本身，不是门误判。"""
    exp = TS0 + _ETH_DAYS * DAY_MS
    inst = "ETH-USD_UM-260904-2050-C"
    pts = iv_points_from_trades(
        [_trade(inst, TS0, _ETH_PX)], spot_at=lambda ts: _ETH_SPOT,
        contracts={inst: _meta(_ETH_K, exp, "C")},
        max_abs_delta=0, max_iv=0)
    assert len(pts) == 1
    assert pts[0].iv > 1.5                      # ≈1.97，与归档实测的 202.5% 同量级
    # 两道门的分工（关键）：|delta| 只有 0.78 —— 为了圆上那个虚高价格，σ 被推到 2，
    # σ 一大 d1 反而回落、delta 跟着回落。**病态点不会自我标榜成高 delta**，
    # 所以只靠 delta 判据会漏过它，真正拦住它的是 IV 绝对上限。
    d = bs_delta(_ETH_SPOT, _ETH_K, years_to_expiry(TS0, exp), pts[0].iv, 0.0, "C")
    assert abs(d) < 0.95
    args = dict(spot_at=lambda ts: _ETH_SPOT,
                contracts={inst: _meta(_ETH_K, exp, "C")})
    assert len(iv_points_from_trades([_trade(inst, TS0, _ETH_PX)],
                                    max_iv=0, **args)) == 1     # 只开 delta 门 → 漏过
    assert iv_points_from_trades([_trade(inst, TS0, _ETH_PX)],
                                 max_abs_delta=0, **args) == []  # 只开 IV 门 → 拦住


def test_iv_points_keeps_long_dated_deep_call():
    """BTC-260925-64000-C：实值 18% 但剩 27 天，IV 51.7% 正常 —— 不得误伤。"""
    spot, k, iv, days = 77690.0, 64000.0, 0.517, 27
    exp = TS0 + days * DAY_MS
    px = bs_price(spot, k, years_to_expiry(TS0, exp), iv, 0.0, "C")
    inst = "BTC-USD_UM-260925-64000-C"
    pts = iv_points_from_trades(
        [_trade(inst, TS0, px)], spot_at=lambda ts: spot,
        contracts={inst: _meta(k, exp, "C")})
    assert len(pts) == 1
    assert pts[0].iv == pytest.approx(iv, abs=1e-4)


def test_iv_points_delta_gate_alone_blocks_deep_itm_put():
    """只开 delta 门（max_iv=0）：反解本身正常，但 |delta|>0.95 仍要挡。"""
    spot, k, iv, days = 100.0, 175.0, 0.6, 90
    exp = TS0 + days * DAY_MS
    px = bs_price(spot, k, years_to_expiry(TS0, exp), iv, 0.0, "P")
    inst = "X-175-P"
    args = dict(spot_at=lambda ts: spot, contracts={inst: _meta(k, exp, "P")})
    ok = iv_points_from_trades([_trade(inst, TS0, px)], max_abs_delta=0, max_iv=0,
                               **args)                    # 关掉门 → 正常反解
    assert len(ok) == 1 and ok[0].iv == pytest.approx(iv, abs=1e-4)
    rejects: list[str] = []
    pts = iv_points_from_trades([_trade(inst, TS0, px)], max_iv=0, **args,
                               on_reject=lambda w, i: rejects.append(w))
    assert pts == [] and rejects == ["deep_itm"]


def test_iv_points_iv_gate_alone_blocks_extreme_far_otm():
    """只开 IV 门（max_abs_delta=0）：深虚 put 的 |delta| 很小，只有 IV 门能挡。"""
    spot, k, iv, days = 100.0, 50.0, 2.0, 30
    exp = TS0 + days * DAY_MS
    px = bs_price(spot, k, years_to_expiry(TS0, exp), iv, 0.0, "P")
    inst = "X-50-P"
    rejects: list[str] = []
    pts = iv_points_from_trades(
        [_trade(inst, TS0, px)], spot_at=lambda ts: spot,
        contracts={inst: _meta(k, exp, "P")}, max_abs_delta=0,
        on_reject=lambda w, i: rejects.append(w))
    assert pts == [] and rejects == ["extreme_iv"]


# ─────────────────── fit_smile 虚值侧优先 ───────────────────

def test_fit_smile_prefers_otm_side_regardless_of_arrival_order():
    """同 strike 两侧都有：虚值侧优先，且与到达顺序无关。"""
    itm_call = _pt(100, 2050.0, 2.025, right="C")     # ts 更早，但在实值侧
    otm_put = _pt(50, 2050.0, 0.55, right="P")        # ts 更晚，虚值侧
    fwd = 2400.0                                       # K=2050 < F → put 侧
    for seq in ([itm_call, otm_put], [otm_put, itm_call]):
        sm = fit_smile(seq, forward=fwd)
        assert sm.iv(2050.0) == pytest.approx(0.55)


def test_fit_smile_same_side_keeps_latest():
    a = _pt(100, 95.0, 0.70, right="P")
    b = _pt(200, 95.0, 0.66, right="P")
    assert fit_smile([a, b], forward=100.0).iv(95.0) == pytest.approx(0.66)
    assert fit_smile([b, a], forward=100.0).iv(95.0) == pytest.approx(0.66)


def test_fit_smile_without_forward_falls_back_to_latest():
    """forward 缺失时判不出虚值侧，退回「按时间取最近」（原行为）。"""
    sm = fit_smile([_pt(100, 95.0, 0.70, right="P"),
                    _pt(200, 95.0, 2.0, right="C")], forward=0.0)
    assert sm.iv(95.0) == pytest.approx(2.0)


def test_fit_smile_keeps_sole_observation_even_if_itm():
    """只有一侧观测时不做取舍 —— 否则会把整个到期从链上剔空。"""
    sm = fit_smile([_pt(100, 95.0, 0.7, right="C")], forward=100.0)
    assert sm.strikes == [95.0]
    assert sm.iv(95.0) == pytest.approx(0.7)


def test_surface_eth_case_no_longer_poisons_deep_put():
    """落地到微笑：2050 的病态 call 被门挡下后，深虚 put 改由邻近 strike 外推。"""
    exp = TS0 + _ETH_DAYS * DAY_MS
    contracts = {"P2300": _meta(2300.0, exp, "P"),
                 "P2400": _meta(2400.0, exp, "P")}
    s = IVSurface(contracts)
    s.add_points([_pt(TS0 - 1000, 2300.0, 0.577, exp, inst="P2300"),
                  _pt(TS0 - 1000, 2400.0, 0.487, exp, inst="P2400")])
    sm = s.smile_at(TS0, exp, forward=2470.0)
    assert 2050.0 not in sm.strikes
    assert sm.iv(2050.0) == pytest.approx(0.577)     # 左端常数延拓，不是 2.025
