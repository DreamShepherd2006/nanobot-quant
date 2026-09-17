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
