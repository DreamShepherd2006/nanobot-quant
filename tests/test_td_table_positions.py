

# ── 数量/价格定点显示 + 持仓价值列（2026-10-30 用户要求）──────────────

def test_fmt_qty_never_scientific():
    """BTC 0.00005094 不能用 ``.6g`` 渲染成 5.094e-05。"""
    from nanobot_quant.td_table_handlers import _fmt_qty

    assert _fmt_qty(0.00005094) == "0.00005094"
    assert _fmt_qty(0.00558) == "0.00558"
    assert _fmt_qty(0.04085) == "0.04085"
    assert _fmt_qty(0.1) == "0.1"          # 去尾零，不留 0.100000000000
    assert _fmt_qty(0) == "—"
    assert _fmt_qty(None) == "—"


def test_fmt_pos_price_never_scientific():
    from nanobot_quant.td_table_handlers import _fmt_pos_price

    assert _fmt_pos_price(78534) == "78534.00"
    assert _fmt_pos_price(716.92) == "716.92"
    assert _fmt_pos_price(97.92) == "97.92"
    assert _fmt_pos_price(0.09253) == "0.09253"
    assert _fmt_pos_price(None) == "—"


def test_pos_value_uses_price_then_entry():
    from nanobot_quant.td_table_handlers import _pos_value_txt

    # 运行中：数量 × 现价
    assert _pos_value_txt(0.00558, 716.0, 716.92) == "$4.00"
    # 未运行（无现价）：按成本价估算，带 ≈
    assert _pos_value_txt(0.00558, None, 716.92) == "≈$4.00"
    assert _pos_value_txt(0, 700, 700) == "—"


def test_pos_row_has_value_column_and_fixed_decimals():
    from nanobot_quant.td_table_handlers import _pos_row

    row = _pos_row({"symbol": "BTC", "slot": 1, "qty": 0.00005094,
                    "entry_price": 78534.0, "price": 78515.1, "pnl_pct": -0.0002})
    assert "0.00005094" in row and "e-05" not in row
    assert "78534.00" in row and "e+04" not in row
    assert "$4.00" in row
