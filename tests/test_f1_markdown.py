"""F1 markdown 渲染 + 异步契约的测试（§33.36 S1）。

fixture 结构照抄 2026-09-21 真实跑出来的输出（601127 1D / 1H），
不臆造字段名 —— 上次「不凭臆想写 fixture」的教训。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"


def _load(name: str, rel: str):
    """直接加载模块文件，绕开 `strategies/__init__` 对 lumibot 的依赖。"""
    import sys

    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SRC / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fm():
    return _load("_f1_markdown_test", "nanobot_quant/f1_markdown.py")


# ── 真实结构 fixture ──────────────────────────────────────────────

TD_PAYLOAD = {
    "kind": "f1_td",
    "data_source": "sina",
    "summary": "数据源=sina；1/1 个 标的×周期 成功；其中 1 个 F1 的 TD 显著。",
    "note": "F1 = ATR_n[t]/ATR_n[t-lookback]。这里还有很多很多后续说明文字。" * 3,
    "results": [
        {
            "symbol": "601127",
            "period": "1D",
            "lookback_bars": 1,
            "status": "ok",
            "source": "sina",
            "bars": 2470,
            "days": 3746.0,
            "cv": 0.0306,
            "cv_hint": "低",
            "f1_buy9": {"n": 1, "median": 2.49, "hit": 1.0, "p": 0.225},
            "f1_sell9": {"n": 5, "median": 5.97, "hit": 1.0, "p": 0.0},
            "f1_buy9_all": {"n": 1, "median": 2.49, "hit": 1.0, "p": 0.225},
            "f1_sell9_all": {"n": 13, "median": 5.97, "hit": 0.92, "p": 0.0},
            "price_buy9": {"n": 40, "median": 0.78, "hit": 0.525, "p": 0.1},
            "price_sell9": {"n": 23, "median": -2.57, "hit": 0.391, "p": 0.95},
        },
        {
            "symbol": "999999",
            "period": "1m",
            "status": "error",
            "error": "ValueError: 新浪数据源不支持 1m 周期",
        },
    ],
}

DD_PAYLOAD = {
    "kind": "f1_drawdown",
    "data_source": "sina",
    "summary": "数据源=sina；1/1 个 标的×周期 成功（全样本）。",
    "results": [
        {
            "symbol": "601127",
            "period": "1H",
            "lookback_bars": 3,
            "status": "ok",
            "bars": 3974,
            "days": 1496.0,
            "horizons": [
                {
                    "k": 12,
                    "segments": [
                        {
                            "segment": "训练",
                            "bars": 2384,
                            "buy9": {
                                "n": 37,
                                "segment_min": {"n": 37, "ratio": 0.7276, "trigger": -0.5, "control": -0.7},
                                "bar_worst": {"n": 37, "ratio": 0.7816, "trigger": -0.3, "control": -0.4},
                                "breach_count": {"n": 37, "ratio": 0.8513, "trigger": 0.4, "control": 0.5},
                            },
                            "sell9": {
                                "n": 17,
                                "segment_min": {"n": 17, "ratio": 1.3056, "trigger": -0.9, "control": -0.7},
                                "bar_worst": {"n": 17, "ratio": 1.1283, "trigger": -0.5, "control": -0.4},
                                "breach_count": {"n": 17, "ratio": 0.7737, "trigger": 0.4, "control": 0.5},
                            },
                            "price_buy9": {
                                "n": 207,
                                "segment_min": {"n": 207, "ratio": 1.2596, "trigger": -0.8, "control": -0.6},
                                "bar_worst": {"n": 207, "ratio": 1.1245, "trigger": -0.4, "control": -0.3},
                                "breach_count": {"n": 207, "ratio": 0.8012, "trigger": 0.5, "control": 0.6},
                            },
                        },
                        {
                            "segment": "留出",
                            "bars": 1590,
                            "sell9": {
                                "n": 21,
                                "segment_min": {"n": 21, "ratio": 1.9932, "trigger": -1.1, "control": -0.6},
                                "bar_worst": {"n": 21, "ratio": 2.0994, "trigger": -0.7, "control": -0.3},
                                "breach_count": {"n": 21, "ratio": 1.6812, "trigger": 0.9, "control": 0.5},
                            },
                        },
                    ],
                }
            ],
        }
    ],
}


# ── 渲染 ──────────────────────────────────────────────────────────


def test_render_empty_returns_blank(fm):
    assert fm.render_markdown(None) == ""
    assert fm.render_markdown({}) == ""


def test_dispatch_by_kind(fm):
    assert "回撤诊断" in fm.render_markdown(DD_PAYLOAD)
    assert "触发统计" in fm.render_markdown(TD_PAYLOAD)


def test_dispatch_structural_fallback(fm):
    """缺 kind 时按结构猜——老记录（无 kind）也要能渲染。"""
    no_kind_dd = {k: v for k, v in DD_PAYLOAD.items() if k != "kind"}
    assert "回撤诊断" in fm.render_markdown(no_kind_dd)
    no_kind_td = {k: v for k, v in TD_PAYLOAD.items() if k != "kind"}
    assert "触发统计" in fm.render_markdown(no_kind_td)


def test_td_markdown_contains_all_rows(fm):
    md = fm.render_markdown(TD_PAYLOAD)
    for label in ("F1 buy9", "F1 sell9", "价格 buy9（对照）", "价格 sell9（对照）"):
        assert label in md
    assert "601127" in md and "1D" in md
    # 参数快照在最前（用户拍板口径：先参数后结论）
    assert md.index("### 📋 参数快照") < md.index("### 📈")
    # 显著性数值要落到表里
    assert "5.97" in md


def test_dd_markdown_has_three_columns(fm):
    """回撤诊断的三个比值列必须并列出现（§33.34 的 UI 固化）。"""
    md = fm.render_markdown(DD_PAYLOAD)
    assert "段回撤比" in md
    assert "单根比" in md
    assert "插针比" in md
    header = [ln for ln in md.splitlines() if ln.startswith("| k |")][0]
    assert header.index("段回撤比") < header.index("单根比") < header.index("插针比")


def test_dd_markdown_marks_direction(fm):
    """>1 标红 🔴、<1 标绿 🟢 —— 方向必须一眼可见。"""
    md = fm.render_markdown(DD_PAYLOAD)
    assert "🔴" in md and "🟢" in md
    # 1.99（留出 sell9 段回撤）应被判为「更深」
    assert "1.99" in md


def test_failed_entries_are_listed(fm):
    """失败条目不得静默——用户永久规则。"""
    md = fm.render_markdown(TD_PAYLOAD)
    assert "失败条目" in md
    assert "999999" in md
    assert "不支持 1m" in md


def test_note_is_truncated(fm):
    """口径说明要截断，否则几千字的背景会淹没结论表。"""
    md = fm.render_markdown(TD_PAYLOAD)
    assert "口径说明" in md
    note_line = [ln for ln in md.splitlines() if ln.startswith("- 口径说明")][0]
    assert len(note_line) < 400


def test_missing_ratio_renders_dash(fm):
    """ratio=None 显示 —，不编造数值。"""
    payload = {
        "kind": "f1_drawdown",
        "results": [
            {
                "symbol": "X",
                "period": "1H",
                "status": "ok",
                "horizons": [
                    {
                        "k": 12,
                        "segments": [
                            {
                                "segment": "训练",
                                "buy9": {
                                    "n": 9,
                                    "segment_min": {"n": 9, "ratio": None},
                                    "bar_worst": {"n": 9, "ratio": 0.9},
                                    "breach_count": {"n": 9, "ratio": None},
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }
    md = fm.render_markdown(payload)
    assert "—" in md


# ── 异步契约 ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def tools_f1():
    return _load("_tools_f1_test", "nanobot_quant/tools/tools_f1.py")


def test_run_f1_analysis_rejects_unknown_kind(tools_f1):
    r = tools_f1.run_f1_analysis(kind="nope", symbols=["601127"])
    assert "error" in r
    assert "f1_td" in r.get("hint", "")


def test_run_f1_analysis_requires_symbols(tools_f1):
    r = tools_f1.run_f1_analysis(kind="f1_td", symbols=[])
    assert "error" in r


def test_get_f1_result_missing_run_id(tools_f1):
    assert "error" in tools_f1.get_f1_result("")


def test_get_f1_result_unknown_run_id(tools_f1):
    r = tools_f1.get_f1_result("f1-19700101-000000-deadbe")
    assert "error" in r


def test_f1_run_prefix_constant(tools_f1):
    """前缀是与现货/期权回测区分的关键（页面历史分开列）。"""
    assert tools_f1.F1_RUN_PREFIX == "f1-"
    assert tools_f1._F1_KINDS == ("f1_td", "f1_drawdown")
