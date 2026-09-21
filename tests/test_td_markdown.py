"""td-table markdown 输出测试（用户拍板 2026-09-21：复制粘给助手看）。

fixture 列名照抄 ``td_table_handlers._display`` 的真实产物
（``_time`` / ``_time_utc`` / ``_pct`` / ``recommendation`` / ``combined_score``
/ ``buy_setup_count`` ...），不臆造字段名。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.td_markdown import (
    fmt_pct,
    fmt_price,
    fmt_score,
    md_table,
    params_snapshot_rows,
    render_bars_markdown,
    render_f1_markdown,
)
from nanobot_quant.td_table_handlers import (
    _fmt_price,
    _fmt_price_dash,
    _md_block,
    _md_notes,
)


def _disp() -> pd.DataFrame:
    """两行展示表（形状 = _display 输出 + 引擎计数列）。"""
    return pd.DataFrame(
        [
            {
                "Close": 100.0, "_pct": float("nan"),
                "_time": "2026-09-20 08:00", "_time_utc": "2026-09-20 00:00",
                "buy_setup_count": 8, "sell_setup_count": 0,
                "buy_countdown_count": 0, "sell_countdown_count": 0,
                "tdst_support": 91.5, "tdst_resistance": 118.25,
                "combined_score": 12.3456, "recommendation": "HOLD",
            },
            {
                "Close": 111.16, "_pct": 1.234,
                "_time": "2026-09-21 08:00", "_time_utc": "2026-09-21 00:00",
                "buy_setup_count": 0, "sell_setup_count": 4,
                "buy_countdown_count": 0, "sell_countdown_count": 0,
                "tdst_support": 92.0, "tdst_resistance": 119.0,
                "combined_score": 20.5, "recommendation": "SELL (Setup Complete)",
            },
        ]
    )


def test_fmt_price_parity_with_handler():
    """markdown 与页面数字格式必须一致（防止两边静默漂移）。

    NaN 是**有意分歧**：页面旧 ``_fmt_price`` 对 NaN 输出字面量 ``nan``
    （C38 ③ 缺陷），markdown 输出空、F1 单元格改走 ``_fmt_price_dash`` →「—」。
    """
    for v in (0, 75.87, 111.164, 0.09253, 1234.5678, -3.5, None):
        assert fmt_price(v) == _fmt_price(v), v
    assert fmt_price(float("nan")) == ""        # markdown：空
    assert _fmt_price(float("nan")) == "nan"   # 旧页面行为（仅用于确认分歧）
    assert _fmt_price_dash(float("nan")) == "—"


def test_fmt_price_dash_and_scalars():
    assert _fmt_price_dash(float("nan")) == "—"
    assert _fmt_price_dash(None) == "—"
    assert _fmt_price_dash(0) == "0"
    assert _fmt_price_dash(111.16) == "111.16"
    assert fmt_pct(1.2344) == "+1.23%"
    assert fmt_pct(-2.5) == "-2.50%"
    assert fmt_pct(float("nan")) == ""
    assert fmt_score(12.3456) == "12.35"
    assert fmt_score(None) == ""


def test_md_table_align_and_escape():
    out = md_table(["时间", "收盘"], [["2026-09-21 08:00", "111.16"],
                                      ["a|b", None]])
    lines = out.splitlines()
    assert lines[0] == "| 时间 | 收盘 |"
    assert lines[1] == "|:--|--:|"          # 数值列右对齐
    assert lines[2] == "| 2026-09-21 08:00 | 111.16 |"
    assert lines[3] == "| a\\|b |  |"        # 竖线转义 + None → 空
    assert md_table([], []) == ""


def test_params_snapshot_rows_sources():
    params = {"setup_period": 9, "countdown_period": 13, "compare_length": 4,
              "recycle_threshold": 18, "score_threshold": 0.0, "tdst_filter": False,
              "entry_setup": 9, "exit_setup": 9, "exit_countdown": 13}
    rows = params_snapshot_rows(params, "td_sequential", entry_setup=9,
                                exit_setup=9, exit_cd=13, trend_period="15m",
                                execution_channel="gate")
    flat = ["|".join(r) for r in rows]
    assert any("td_params.json（td_sequential）" in r for r in flat)
    assert any("15m|exec_params.trend_period" in r for r in flat)
    assert any("gate|exec_params.execution_channel" in r for r in flat)
    assert any("9 / 13" in r for r in flat)
    # 整数型数值不留小数点（0.0 → 0）
    assert any(r.startswith("Score 阈值") and "|0 / 关|" in r for r in flat)


def test_render_bars_markdown_structure():
    md = render_bars_markdown(
        title="📊 TD 序列分析 · 实时快照（TD 价格）",
        meta=[("数据源", "Gate CEX（SOL_USDT）"), ("序列", "TD 价格"), ("标的", "SOL"),
              ("周期", "1D"), ("根数", "2（最近）")],
        params_rows=[["策略", "td_sequential", "strategy.json"]],
        trend_line="大周期趋势（15m）：涨势 setup_buy=0 · setup_sell=3（只读展示，不参与交易）",
        status={"策略": "td_sequential", "最新收盘": "111.16"},
        disp=_disp(), setup=9, notes=_md_notes("SOL", "cex"),
    )
    assert "## 📊 TD 序列分析 · 实时快照（TD 价格）" in md
    assert "Gate CEX（SOL_USDT）" in md
    assert "### 📋 参数快照（实际生效）" in md
    assert "大周期趋势（15m）：涨势" in md
    assert "### 📈 K 线（2 根）" in md
    assert "| 时间 | UTC 时间 | 收盘 | 涨跌% | Buy Setup | Sell Setup |" in md
    assert "TDST 支撑" in md and "Score" in md
    assert "SELL (Setup Complete)" in md
    assert "+1.23%" in md
    assert "TDST 突破仅展示、不触发下单" in md     # 口径说明写入
    # HOLD → —（与页面同口径）
    assert "| 8 |  |" in md

def test_render_bars_markdown_stats_block():
    agg = {"BUY": {3: {"rate": 72.5, "win": 29, "n": 40}},
           "SELL": {5: {"rate": 31.0, "win": 9, "n": 29}}}
    sig = [{"time": "2026-09-21T08:00", "direction": "SELL", "price": 111.16,
            "pct3": -0.5, "pct5": None, "pct10": 1.1}]
    md = render_bars_markdown(
        title="t", meta=[("标的", "SOL")], params_rows=[],
        disp=_disp(), setup=9, stats=(sig, agg), notes=[],
    )
    assert "72.5%（29/40）" in md
    assert "31.0%（9/29）" in md
    assert "-0.50%" in md and "2026-09-21 08:00" in md


def test_render_f1_markdown_nan_renders_dash():
    disp = pd.DataFrame([
        {"_time": "2026-07-02 00:00", "_time_utc": "2026-07-01 16:00",
         "Close": float("nan")},
        {"_time": "2026-09-21 00:00", "_time_utc": "2026-09-20 16:00",
         "Close": 0.977618},
    ])
    eng = pd.DataFrame([
        {"buy_setup_count": 1, "sell_setup_count": 0},
        {"buy_setup_count": 2, "sell_setup_count": 0},
    ])
    pct = pd.Series([float("nan"), 0.133])
    md = render_f1_markdown(
        title="📊 TD 序列分析 · TD F1（只读诊断）",
        meta=[("序列", "F1（ATR20 扩张率）"), ("lookback", "1 根（≈3h 语义）")],
        params_rows=[], disp=disp, eng=eng, pct=pct, setup=9, lb=1, win=150,
        has_cd=False, notes=["F1 只读诊断"],
    )
    # 新行在上：最新一根 = 2026-09-21；预热行 F1 值为空、没有字面量 nan
    body = [ln for ln in md.splitlines() if ln.startswith("| 2026-")]
    assert body[0].startswith("| 2026-09-21 00:00")
    assert "nan" not in md.lower()
    assert "13.3%" in md                      # 分位 ×100 一位小数
    assert body[1].split("|")[3].strip() == ""  # 预热区 F1 值 → 空（页面显示 —）


def test_md_notes_split_predicate_matches_display():
    """A股 切分说明只在真正切分时出现（1D 不切、日内切）——与展示层同一判据。"""
    def has_note(ticker, source, bar):
        return any("按交易日切分" in n for n in _md_notes(ticker, source, bar))

    assert has_note("588000", "stock", "30m")     # A股 日内 → 切分
    assert has_note("510050", "stock", "5m")
    assert not has_note("588000", "stock", "1D")  # A股 日线 → 不切（与页面标注一致）
    assert not has_note("SOL", "cex", "1D")       # 非 A股


def test_md_block_and_notes():
    html = _md_block("实时快照", "## t\n| a | b |\n|:--|:--|\n| <script> | 1 |")
    assert 'data-md-copy="1"' in html
    assert "📋 复制 Markdown（实时快照）" in html
    assert "<pre class=\"md-src\"" in html
    assert "&lt;script&gt;" in html            # markdown 原文做 HTML 转义
    assert "navigator.clipboard.writeText" in html
    assert _md_block("x", "") == ""            # 空 markdown 不渲染区块
    assert any("A股" in n for n in _md_notes("588000", "stock", "30m"))
    assert not any("A股" in n for n in _md_notes("SOL", "cex", "1D"))
