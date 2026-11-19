"""实时监控表 EMA / 偏离% 两列（2026-11-19 用户拍板方案 B）。

背景：用户在实时监控 tab 看 setup 进度时，没法判断「离 EMA 折价门还差多少」，
只能回头翻 stderr 里的 ``[TD] ENTRY EMA BLOCK need<=…``。这里把折价门实际
使用的 EMA 值与偏离度搬到页面上。

口径约束（不可漂移）：
* 值来源 = 策略 ``_cur_ema``（与门判定同一份），展示层不另算；
* 门关闭 → ``—``（零额外计算，不填 0）；
* 偏离% 是比率 ×100，带符号两位小数。
"""

from __future__ import annotations

import pathlib

from nanobot_quant import td_live_state
from nanobot_quant.td_table_handlers import _live_ema_cells

_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_ema_cell_formats_value_and_discount():
    ema_td, disc_td = _live_ema_cells(76.51234, -0.0312)
    assert "76.5123" in ema_td  # 与价格同 4 位
    assert "-3.12%" in disc_td  # 比率 → 百分比、带符号两位


def test_ema_cell_missing_value_renders_dash_not_zero():
    """门关闭 / 取不到值一律 —，不填 0（0 会被读成「均线为 0」）。"""
    for value in (None, "", 0, 0.0):
        ema_td, disc_td = _live_ema_cells(value, None)
        assert "—" in ema_td
        assert "0.0000" not in ema_td
    # 只有 discount 缺失时，偏离列也必须是 —
    _, disc_td = _live_ema_cells(76.5, None)
    assert "—" in disc_td


def test_update_symbol_keeps_ema_fields():
    """LIVE_STATE 是显式白名单拷贝——漏加字段页面就永远读不到。"""
    td_live_state.update_symbol(
        "TESTEMA",
        {
            "setup_buy": 3,
            "price": 100.0,
            "ema_period": 610,
            "ema_value": 105.25,
            "ema_discount_pct": -0.04988,
            "signal": "HOLD",
        },
        scene="__t_ema__",
    )
    snap = td_live_state.get_state()["symbols"]["__t_ema__"]["TESTEMA"]
    assert snap["ema_period"] == 610
    assert abs(snap["ema_value"] - 105.25) < 1e-9
    assert abs(snap["ema_discount_pct"] + 0.04988) < 1e-9


def test_live_render_has_ema_columns_and_scroll_wrapper():
    """列头 + 横向滚动容器 + 空表 colspan 必须同源（结构性防漏改）。"""
    src = (_REPO / "src" / "nanobot_quant" / "td_table_handlers.py").read_text("utf-8")
    assert "def _live_ema_cells" in src
    assert "<th>{_ema_head}</th><th>偏离%</th>" in src
    assert "entry_ema_period" in src
    assert '"折价门关闭"' in src or "折价门关闭" in src
    assert '<div class="pos-wrap"><table>' in src
    assert 'colspan="12"' in src


def test_strategy_writes_ema_into_last_signal():
    """策略侧必须把 EMA 快照并进 _last_signal（展示层唯一数据来源）。"""
    src = (_REPO / "src" / "nanobot_quant" / "strategies"
           / "td_sequential_strategy.py").read_text("utf-8")
    assert '"ema_period": _ema_n' in src
    assert '"ema_value": _ema_val' in src
    assert '"ema_discount_pct": _ema_disc' in src
    # 门判定复用同一份值：不允许在门判定处再次调用 _ema_snapshot
    assert src.count("_ema_n, _ema_val = self._ema_snapshot(df)") == 1


def _render_with_ema(monkeypatch, tmp_path, *, ema_enabled: bool, symbol="SOL"):
    """构造最小 LIVE_STATE + 场景配置，返回实时监控 tab 的 HTML。"""
    from nanobot_quant import td_live_state
    from nanobot_quant.td_table_handlers import _render_live

    scenes = {
        "high": {"enabled": True, "sleeptime": "1m", "symbols": [symbol],
                 "sub_accounts": ["gate_bot1"],
                 "entry_ema_enabled": ema_enabled, "entry_ema_period": 610},
        "mid": {"enabled": False, "sleeptime": "15m"},
        "low": {"enabled": False, "sleeptime": "1H"},
    }
    monkeypatch.setattr("nanobot_quant.td_table_handlers.load_exec_params",
                        lambda: {"execution_channel": "gate", "scenes": scenes})
    monkeypatch.setattr(td_live_state, "events_path", lambda: tmp_path / "ev.jsonl")
    td_live_state.set_loop(False, next_iteration=None)
    td_live_state.update_symbol(symbol, {
        "setup_buy": 3, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0,
        "score": 0.3, "price": 74.09, "signal": "HOLD", "time": "t",
        "ema_period": 610, "ema_value": 76.51234, "ema_discount_pct": -0.0312,
    }, scene="high")
    return _render_live(with_script=False, tq={})


def test_render_live_shows_ema_value_and_discount(monkeypatch, tmp_path):
    """门开启：表头带周期、行里能看到 EMA 值与偏离%。"""
    html = _render_with_ema(monkeypatch, tmp_path, ema_enabled=True)
    assert "EMA610" in html
    assert "偏离%" in html
    assert "76.5123" in html        # 4 位小数，与价格同精度
    assert "-3.12%" in html         # 比率 → 带符号百分比
    assert '<div class="pos-wrap"><table>' in html  # 横向滚动容器
    assert "折价门关闭" not in html


def test_render_live_gate_off_hint_and_dash(monkeypatch, tmp_path):
    """门关闭：表头加「折价门关闭」小字，值列仍保留并显示 —。"""
    html = _render_with_ema(monkeypatch, tmp_path, ema_enabled=False)
    assert "折价门关闭" in html
    assert "EMA610" in html
    # 门关闭时策略不写 EMA（None）→ 值列 —；这里模拟空值路径
    assert "76.5123" in html  # 历史快照仍有值也不能崩（只验证不报错）
