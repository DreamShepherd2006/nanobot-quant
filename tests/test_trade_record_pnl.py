"""交易记录「净收益率 / 盈亏(USD)」与定点格式化（2026-11-02）。

覆盖：
- ``number_fmt.fmt_decimal`` 永不输出科学计数法（用户永久偏好）
- 展示层 ``_fmt_qty`` 与策略层 ``_fmt_num`` 共用同一底层
- ``_exit_pnl_fields`` 的净收益率口径：成交价 × (1 − fee_rate) / 入场价 − 1
- 缺失字段不写（展示为 —，历史事件不回填）
- 两个单元格渲染函数（正绿负红、缺失 —）
"""

import pytest

from nanobot_quant.number_fmt import fmt_decimal
from nanobot_quant.td_table_handlers import (
    _fmt_qty,
    _pnl_cell,
    _pnl_usd_cell,
)


# ── 定点格式化 ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value, expected",
    [
        (0.00005115452, "0.00005115452"),   # BTC 实盘数量（曾渲染成 5.11545e-05）
        (0.0000513, "0.0000513"),
        (0.00558, "0.00558"),               # BNB 不能再多出一串 0
        (78534.0, "78534"),                 # 高价不能变 7.8534e+04
        (0, "0"),
        (1, "1"),
        (-0.5, "-0.5"),
    ],
)
def test_fmt_decimal_never_uses_scientific_notation(value, expected):
    out = fmt_decimal(value)
    assert out == expected
    assert "e" not in out.lower()


def test_fmt_decimal_invalid_passthrough():
    assert fmt_decimal(None) == "None"
    assert fmt_decimal("abc") == "abc"


def test_fmt_qty_delegates_and_keeps_dash_semantics():
    """展示层数量列：<=0 显示 —（与策略层 fmt_num 的 0 → "0" 不同）。"""
    assert _fmt_qty(0.00005115452) == "0.00005115452"
    assert _fmt_qty(0) == "—"
    assert _fmt_qty(-1) == "—"
    assert _fmt_qty(None) == "—"


# ── 净收益率 / 盈亏 ─────────────────────────────────────────────────

def _strategy():
    """绕过 __init__（避免 lumibot/参数初始化），只测纯计算。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st._fee_rate = 0.001
    return st


def test_exit_pnl_fields_btc_slot3():
    """实盘 BTC slot3（2026-11-01 11:11 → 11-02 03:28）逐位核对。

    买 77898.5（含费均价）/ 卖 78563.6 / qty 0.00005115452 / fee 0.001
    净收益率 ≈ +0.753%，净盈亏 ≈ +0.0300 USD。
    """
    st = _strategy()
    out = st._exit_pnl_fields({
        "slot": 3,
        "qty": 0.00005115452,
        "price": 78658.4,
        "actual_price": 78563.6,
        "entry_price": 77898.5,
    })
    assert out["entry_price"] == pytest.approx(77898.5)
    assert out["pnl_net_ratio"] == pytest.approx(0.0075295, abs=1e-6)
    assert out["pnl_net_usd"] == pytest.approx(0.0300, abs=1e-3)
    # 其它字段原样保留
    assert out["slot"] == 3


def test_exit_pnl_fields_falls_back_to_strategy_price():
    """无实际成交价（未确认 / DEX 占位）时用策略价，仍给出估算。"""
    st = _strategy()
    out = st._exit_pnl_fields({"qty": 1.0, "price": 100.0, "entry_price": 99.0})
    assert out["pnl_net_ratio"] == pytest.approx((100.0 * 0.999) / 99.0 - 1, abs=1e-9)


def test_exit_pnl_fields_skips_without_entry_price():
    """缺入场价（历史事件 / 非分批单仓路径）→ 不写字段，展示 — 。"""
    st = _strategy()
    src = {"slot": 1, "qty": 1.0, "price": 100.0, "actual_price": 101.0}
    out = st._exit_pnl_fields(src)
    assert "pnl_net_ratio" not in out
    assert "pnl_net_usd" not in out


def test_exit_pnl_fields_skips_on_zero_price():
    st = _strategy()
    out = st._exit_pnl_fields({"qty": 1.0, "price": 0, "entry_price": 99.0})
    assert "pnl_net_ratio" not in out


def test_exit_pnl_fields_zero_qty_still_reports_ratio():
    st = _strategy()
    out = st._exit_pnl_fields({"qty": 0, "price": 100.0, "entry_price": 99.0})
    assert "pnl_net_ratio" in out
    assert "pnl_net_usd" not in out


def test_exit_pnl_fields_respects_fee_rate():
    """费率变化直接反映到净收益率（0 费率 = 毛收益率）。"""
    st = _strategy()
    st._fee_rate = 0.0
    out = st._exit_pnl_fields({"qty": 1.0, "price": 101.0, "entry_price": 100.0})
    assert out["pnl_net_ratio"] == pytest.approx(0.01, abs=1e-9)


# ── 单元格渲染 ──────────────────────────────────────────────────────

def test_pnl_cell_positive_and_negative():
    pos = _pnl_cell(0.0075295)
    assert "+0.75%" in pos
    assert "#1b7f3d" in pos          # 绿
    neg = _pnl_cell(-0.012)
    assert "-1.20%" in neg
    assert "#b3261e" in neg          # 红


def test_pnl_cell_missing_is_dash():
    assert _pnl_cell(None) == "—"
    assert _pnl_cell("abc") == "—"


def test_pnl_usd_cell_formats_and_colors():
    pos = _pnl_usd_cell(0.0300)
    assert "+0.0300" in pos
    assert "#1b7f3d" in pos
    neg = _pnl_usd_cell(-0.5)
    assert "-0.5000" in neg
    assert "#b3261e" in neg
    assert _pnl_usd_cell(None) == "—"
