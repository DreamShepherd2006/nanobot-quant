"""Tests for ``analyze_iv_leadlag`` (tools/tools_iv.py).

只读研究工具：验证参数夹取、markdown 附加、错误路径（异常/非 dict/markdown
渲染失败都不得静默），以及「不触碰交易路径」的结构性边界。
全程 monkeypatch，无网络。
"""

from __future__ import annotations

import pathlib

from nanobot_quant.analysis import iv_leadlag as il
from nanobot_quant.tools import tools_iv as T


# ── 参数夹取 ────────────────────────────────────────────────────────

def test_params_are_clamped_before_reaching_analysis(monkeypatch):
    seen: dict = {}

    def fake(family, **kw):
        seen["family"] = family
        seen.update(kw)
        return {"ok": True, "family": family, "bucket": kw.get("bucket")}

    monkeypatch.setattr(il, "analyze_iv_leadlag", fake)
    res = T.analyze_iv_leadlag(family="SOL-USD_UM", days=99, bucket="15m",
                               window=1, max_lag=99, n_iter=99999)
    assert res["ok"] is True
    assert seen["days"] == 30          # 归档滚动保留约 30 天
    assert seen["window"] == 2
    assert seen["max_lag"] == 24
    assert seen["n_iter"] == 2000      # 上限，防止病态耗时


def test_days_floor_is_one_and_blank_family_falls_back(monkeypatch):
    seen: dict = {}

    def fake(family, **kw):
        seen["family"] = family
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(il, "analyze_iv_leadlag", fake)
    T.analyze_iv_leadlag(family="   ", days=0)
    assert seen["family"] == "SOL-USD_UM"
    assert seen["days"] == 1


def test_progress_callback_is_always_wired(monkeypatch):
    """诊断必须可见（stderr）：progress 回调不能丢。"""
    seen: dict = {}

    def fake(family, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(il, "analyze_iv_leadlag", fake)
    T.analyze_iv_leadlag()
    assert callable(seen["progress"])


# ── markdown 附加 ───────────────────────────────────────────────────

def test_markdown_attached_when_analysis_omits_it(monkeypatch):
    monkeypatch.setattr(il, "analyze_iv_leadlag",
                        lambda *a, **k: {"ok": True, "family": "SOL-USD_UM"})
    monkeypatch.setattr(il, "markdown", lambda res: "## 报告")
    res = T.analyze_iv_leadlag()
    assert res["markdown"] == "## 报告"


def test_markdown_rendered_by_analysis_is_kept(monkeypatch):
    monkeypatch.setattr(il, "analyze_iv_leadlag",
                        lambda *a, **k: {"ok": True, "markdown": "## 已有"})
    monkeypatch.setattr(il, "markdown",
                        lambda res: (_ for _ in ()).throw(AssertionError("不该重复渲染")))
    assert T.analyze_iv_leadlag()["markdown"] == "## 已有"


def test_markdown_failure_does_not_mask_result(monkeypatch):
    """markdown 只是展示层：渲染失败也不能把结果吞掉。"""
    monkeypatch.setattr(il, "analyze_iv_leadlag",
                        lambda *a, **k: {"ok": True, "family": "SOL-USD_UM"})

    def boom(res):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(il, "markdown", boom)
    res = T.analyze_iv_leadlag()
    assert res["ok"] is True and res["family"] == "SOL-USD_UM"


# ── 错误路径（静默不可接受）─────────────────────────────────────────

def test_analysis_exception_returns_structured_error(monkeypatch):
    def boom(*a, **k):
        raise ValueError("不支持的周期 '7m'")

    monkeypatch.setattr(il, "analyze_iv_leadlag", boom)
    res = T.analyze_iv_leadlag(bucket="7m")
    assert res["ok"] is False
    assert "ValueError" in res["error"] and "7m" in res["error"]
    assert res["bucket"] == "7m"


def test_non_dict_result_is_reported_as_error(monkeypatch):
    monkeypatch.setattr(il, "analyze_iv_leadlag", lambda *a, **k: ["不是 dict"])
    res = T.analyze_iv_leadlag()
    assert res["ok"] is False and "非 dict" in res["error"]


# ── 只读边界（结构性）────────────────────────────────────────────────

def test_tool_is_structurally_read_only():
    """工具只依赖分析模块——不得引入任何下单/台账/交易执行入口。"""
    src = pathlib.Path(T.__file__).read_text(encoding="utf-8")
    for forbidden in ("okx_options_trade", "okx_options_broker", "portfolio",
                      "broker", "execute_signal", "option_ledger", "td_live"):
        assert forbidden not in src, f"只读工具不应引用 {forbidden}"
    assert "analysis import iv_leadlag" in src
