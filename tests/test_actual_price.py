"""实际成交价（稳定币计价规则）单测 — 2026-08-13 方案 B。

规则：交易恒以稳定币计价（broker quote=USDC）——找 swap_status 确认
数据 input/output 里的稳定币（USDC/USDT/USDG）作分子、另一侧数量作
分母 → 价格 = 稳定币金额 / 数量。方向与 input/output 语义无关。
"""
import pytest

from nanobot_quant.td_live_state import compute_actual_price


def _d0(input_side, output_side):
    return {"input": input_side, "output": output_side}


def test_buy_sol_usdc_input_sol():
    """BUY SOL 样本：input SOL / output USDC（与直觉相反的真实样本）。"""
    d0 = _d0(
        [{"amount": "0.026914147", "name": "SOL"}],
        [{"amount": "2.037986", "name": "USDC"}],
    )
    assert compute_actual_price(d0) == pytest.approx(2.037986 / 0.026914147)


def test_buy_sol_usdc_input_usdc():
    """同金额反向排列（input USDC / output SOL）→ 同价（规则与方向无关）。"""
    d0 = _d0(
        [{"amount": "2.037986", "name": "USDC"}],
        [{"amount": "0.026914147", "name": "SOL"}],
    )
    assert compute_actual_price(d0) == pytest.approx(2.037986 / 0.026914147)


def test_sell_sol_usdt():
    """USDT 同样识别为稳定币。"""
    d0 = _d0(
        [{"amount": "0.5", "name": "SOL"}],
        [{"amount": "38.0", "name": "USDT"}],
    )
    assert compute_actual_price(d0) == pytest.approx(76.0)


def test_no_stable_returns_none():
    """无稳定币（如 SOL/WBTC 直兑）→ None。"""
    d0 = _d0(
        [{"amount": "0.5", "name": "SOL"}],
        [{"amount": "0.003", "name": "WBTC"}],
    )
    assert compute_actual_price(d0) is None


def test_both_stable_returns_none():
    """两侧都是稳定币（方向无法唯一确定）→ None。"""
    d0 = _d0(
        [{"amount": "10", "name": "USDC"}],
        [{"amount": "9.99", "name": "USDT"}],
    )
    assert compute_actual_price(d0) is None


def test_empty_and_malformed_returns_none():
    assert compute_actual_price({}) is None
    assert compute_actual_price(None) is None
    assert compute_actual_price({"input": [], "output": []}) is None
    assert compute_actual_price({"input": "x", "output": [{"amount": "1", "name": "SOL"}]}) is None


def test_zero_amount_returns_none():
    d0 = _d0(
        [{"amount": "0", "name": "SOL"}],
        [{"amount": "1", "name": "USDC"}],
    )
    assert compute_actual_price(d0) is None
