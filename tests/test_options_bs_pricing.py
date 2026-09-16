"""Black-Scholes / IV 反解单测（零网络，纯数学）。

关注点（按重要性）：
1. **IV 反解往返一致** —— 用 BS 定价再反解，必须回到原 sigma（回测 delta 的根基）
2. **已知解析值** —— 手工可验的标准案例
3. **put-call parity** —— 两翼定价自洽
4. **fail-closed** —— 无效输入一律 None，不猜、不抛
"""

from __future__ import annotations

import math

import pytest

from nanobot_quant.bs_pricing import (
    SECONDS_PER_YEAR, bs_delta, bs_price, implied_vol, intrinsic,
    norm_cdf, years_to_expiry,
)


# ── 基础函数 ──────────────────────────────────────────────────────────


def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
    assert norm_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


def test_intrinsic_put_and_call():
    assert intrinsic(90, 100, "P") == 10
    assert intrinsic(110, 100, "P") == 0
    assert intrinsic(110, 100, "C") == 10
    assert intrinsic(90, 100, "C") == 0


# ── 已知解析值（手算可验）────────────────────────────────────────────


def test_bs_price_atm_standard_case():
    """S=K=100, T=1, σ=20%, r=0 → call = put = 100×(N(0.1) − N(−0.1)) ≈ 7.9656。"""
    expected = 100 * (norm_cdf(0.1) - norm_cdf(-0.1))
    assert bs_price(100, 100, 1.0, 0.2, 0.0, "C") == pytest.approx(expected)
    assert bs_price(100, 100, 1.0, 0.2, 0.0, "P") == pytest.approx(expected)


def test_bs_price_deep_otm_tends_to_zero():
    px = bs_price(100, 50, 0.02, 0.5, 0.0, "P")
    assert px is not None and 0 <= px < 0.01


def test_bs_price_zero_vol_is_discounted_intrinsic():
    assert bs_price(90, 100, 1.0, 0.0, 0.0, "P") == pytest.approx(10.0)
    assert bs_price(110, 100, 1.0, 0.0, 0.0, "P") == pytest.approx(0.0)


def test_put_call_parity():
    """C − P = S − K·e^(−rT) —— 两翼必须自洽。"""
    s, k, t, sig, r = 97.3, 100.0, 0.011, 0.62, 0.0
    c = bs_price(s, k, t, sig, r, "C")
    p = bs_price(s, k, t, sig, r, "P")
    assert c is not None and p is not None
    assert (c - p) == pytest.approx(s - k * math.exp(-r * t), abs=1e-9)


# ── delta ────────────────────────────────────────────────────────────


def test_bs_delta_atm_is_about_half():
    d_put = bs_delta(100, 100, 1.0, 0.2, 0.0, "P")
    d_call = bs_delta(100, 100, 1.0, 0.2, 0.0, "C")
    assert d_put == pytest.approx(-(1 - norm_cdf(0.1)), abs=1e-12)
    assert d_call == pytest.approx(norm_cdf(0.1), abs=1e-12)
    assert abs(d_put) == pytest.approx(0.46, abs=0.01)
    # put 为负、call 为正（与 OKX ticker 口径一致）
    assert d_put < 0 < d_call


def test_bs_delta_deep_otm_put_near_zero():
    d = bs_delta(100, 60, 0.02, 0.5, 0.0, "P")
    assert d is not None and -0.05 < d <= 0.0


def test_bs_delta_monotonic_in_sigma():
    """同价位下 vol 越高，OTM put 的 |delta| 越大。"""
    lo = abs(bs_delta(100, 90, 0.05, 0.3, 0.0, "P"))
    hi = abs(bs_delta(100, 90, 0.05, 0.9, 0.0, "P"))
    assert lo < hi


# ── IV 反解（核心）──────────────────────────────────────────────────


@pytest.mark.parametrize("right", ["P", "C"])
@pytest.mark.parametrize("sigma", [0.20, 0.45, 0.62, 1.10, 2.00])
@pytest.mark.parametrize("s,k,t", [
    (100, 100, 1.0),      # ATM 长周期
    (97.3, 95.0, 0.011),  # 3 天 OTM put（真实场景）
    (97.3, 90.0, 0.011),  # 3 天远 OTM
    (100, 112, 0.03),     # 3 天浅 OTM call
    (100, 103, 0.03),     # 3 天近 ATM call
])
def test_iv_roundtrip(sigma, s, k, t, right):
    """BS 定价 → 反解，必须回到原 sigma（回测 delta 的根基）。"""
    px = bs_price(s, k, t, sigma, 0.0, right)
    assert px is not None and px > 0
    got = implied_vol(px, s, k, t, right)
    assert got is not None
    assert got == pytest.approx(sigma, rel=1e-4)


def test_iv_roundtrip_ignores_currency_scale():
    """面值/张数不影响：同一 (S,K,T,σ) 无论名义规模，反解一致。"""
    px = bs_price(2500.0, 2400.0, 0.02, 0.75, 0.0, "P")
    got = implied_vol(px, 2500.0, 2400.0, 0.02, "P")
    assert got == pytest.approx(0.75, rel=1e-4)


def test_iv_returns_zero_when_only_intrinsic_left():
    """深 ITM 且报价 == 内在价值 → IV = 0（而非 None）。"""
    assert implied_vol(10.0, 90.0, 100.0, 0.5, "P") == pytest.approx(0.0)


def test_iv_tolerance_scales_with_intrinsic():
    """大 intrinsic 时用相对容差，浮点误差不得被误判为「低于内在」。

    K=2500 的深 ITM put：内在 400。BS 定价后会带 ~1e-13 量级的浮点残差，
    纯绝对容差会判成 mark < intr → None（把可解合约误杀）。
    """
    s, k, t, sig = 2100.0, 2500.0, 0.02, 0.55
    px = bs_price(s, k, t, sig, 0.0, "P")
    assert px is not None and px > intrinsic(s, k, "P")
    got = implied_vol(px, s, k, t, "P")
    assert got is not None and got == pytest.approx(sig, rel=1e-4)


def test_iv_rejects_price_below_intrinsic():
    """报价低于内在（数据异常）→ None，不能给出假 IV。"""
    assert implied_vol(5.0, 90.0, 100.0, 0.5, "P") is None


def test_iv_rejects_unreachable_price():
    """报价高到 500% vol 也够不着 → None（不静默收敛到边界值）。"""
    assert implied_vol(200.0, 100.0, 100.0, 0.01, "C") is None


@pytest.mark.parametrize("args", [
    (None, 100, 100, 1.0),
    (0.5, None, 100, 1.0),
    (0.5, 100, None, 1.0),
    (0.5, 100, 100, None),
    (0.5, 100, 100, 0.0),      # T = 0
    (0.5, 100, 100, -1.0),     # T < 0
    (0.0, 100, 100, 1.0),      # mark 为 0
    (-1.0, 100, 100, 1.0),     # mark 为负
    (0.5, 0, 100, 1.0),        # S = 0
    (0.5, 100, 0, 1.0),        # K = 0
])
def test_iv_fail_closed(args):
    assert implied_vol(*args[:4], "P") is None


# ── 期限换算 ─────────────────────────────────────────────────────────


def test_years_to_expiry():
    exp = int(1_800_000_000_000)
    assert years_to_expiry(exp, exp) is None                      # 已到期
    assert years_to_expiry(exp + 1000, exp) is None               # 已过期
    one_year = years_to_expiry(exp, exp + int(SECONDS_PER_YEAR * 1000))
    assert one_year == pytest.approx(1.0, rel=1e-9)
    three_days = years_to_expiry(exp, exp + 3 * 86400 * 1000)
    assert three_days == pytest.approx(3 / 365, rel=1e-9)


# ── bs_price / bs_delta fail-closed ─────────────────────────────────


@pytest.mark.parametrize("fn", [bs_price, bs_delta])
@pytest.mark.parametrize("args", [
    (0, 100, 1.0, 0.2), (100, 0, 1.0, 0.2),
    (100, 100, 0.0, 0.2), (100, 100, -1.0, 0.2),
    (100, 100, 1.0, None), (100, 100, 1.0, -0.1),
])
def test_price_and_delta_fail_closed(fn, args):
    assert fn(*args, right="P") is None
