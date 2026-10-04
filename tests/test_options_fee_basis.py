"""期权手续费「名义基准」口径回归（2026-10-04 批次 ③）。

官方实测（SOL-USD_UM-261002-114-C 交割账单）：
  卖出 1 张、面值 0.1 SOL、权利金 px 3.8、当时标的指数价 ~117.85
  实扣手续费 0.00353550 USDC = 117.85 × 0.1 × 0.03%
⇒ 名义基准是**标的指数价**，不是 strike（按 strike 算 0.00342，偏低 ~0.35%）。
"""
import pytest

from nanobot_quant.okx_options_trade import option_fee_est


def test_basis_is_index_price_matching_official_bill():
    got = option_fee_est(114, 0.1, 1, premium_px=3.8, basis_px=117.85)
    assert got == pytest.approx(0.0035355, rel=1e-4)          # 官方实扣


def test_basis_defaults_to_strike_for_backward_compat():
    old = option_fee_est(114, 0.1, 1, premium_px=3.8)          # 缺省回退 strike
    assert old == pytest.approx(114 * 0.1 * 0.0003, rel=1e-9)
    new = option_fee_est(114, 0.1, 1, premium_px=3.8, basis_px=117.85)
    assert new > old                                           # 指数价基准略高


def test_cap_still_applies_when_basis_given():
    # 薄权利金（px 0.01）：7% 权利金 cap 生效，与基准无关
    got = option_fee_est(114, 0.1, 1, premium_px=0.01, basis_px=117.85)
    assert got == pytest.approx(0.07 * 0.01 * 0.1, rel=1e-9)


def test_selector_and_driver_use_spot_basis():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sel = (root / "src/nanobot_quant/okx_options_select.py").read_text(encoding="utf-8")
    drv = (root / "src/nanobot_quant/backtest/options_driver.py").read_text(encoding="utf-8")
    assert "notional = (spot or strike) * lot" in sel          # 选档：指数价基准
    assert drv.count("basis_px=(self.data.price_of() or None)") == 3   # 回测 3 处调用点
