

# ── 数量/价格定点显示 + 持仓价值列（2026-10-30 用户要求）──────────────

from pathlib import Path


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


def test_pos_wrap_carries_scene_key_for_scroll_restore(monkeypatch, tmp_path: Path):
    """横向滚动保持（2026-11-10）：每个场景的 .pos-wrap 带 data-pos-key。

    轮询整块替换 #live-wrap 会重建 DOM、scrollLeft 归零，前端靠该 key
    定位同一个场景的滚动容器并回填位置（不能靠索引，场景增删会错位）。
    """
    from nanobot_quant import td_live_state
    from nanobot_quant.td_table_handlers import _render_live

    scenes = {"high": {"enabled": True, "sleeptime": "1m",
                       "sub_accounts": ["gate_bot1"]}}
    monkeypatch.setattr("nanobot_quant.td_table_handlers.load_exec_params",
                        lambda: {"execution_channel": "gate", "scenes": scenes})
    monkeypatch.setattr(td_live_state, "events_path",
                        lambda: tmp_path / "td_live_events.jsonl")
    td_live_state.set_loop(True, next_iteration="07:26:00")
    td_live_state.set_positions("high", {"SOL": [{
        "symbol": "SOL", "slot": 1, "qty": 0.04,
        "entry_price": 86.0, "price": 86.1, "pnl_pct": 0.0012,
    }]})

    html = _render_live(with_script=False, tq={})
    assert '<div class="pos-wrap" data-pos-key="high">' in html
    td_live_state.set_positions("high", {})


def test_live_poll_restores_scroll_position():
    """轮询刷新前后保存/回填横向滚动位置，且回填跑两次（同步 + rAF）。"""
    from nanobot_quant.td_table_handlers import _render_live

    html = _render_live(with_script=True, tq={})
    assert "function snapScroll()" in html
    assert "function applyScroll(o)" in html
    assert "window.scrollTo(o.wx, o.wy)" in html
    # 替换 DOM 前先快照、替换后回填，并在下一帧再补一次（布局可能延后）
    assert "var s = snapScroll();" in html
    assert "el.innerHTML = html;" in html
    assert "applyScroll(s);" in html
    assert "requestAnimationFrame(function(){ applyScroll(s); })" in html
    # 旧的裸替换写法不得残留（否则滚动仍会跳回）
    assert "if (el && html) el.innerHTML = html;" not in html
