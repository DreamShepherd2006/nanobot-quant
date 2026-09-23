"""tools_ashare（A股 数据源可达性体检）单测 —— 全离线，不碰网络。

覆盖：fail-soft（单源失败不阻断）、fail-visible（错误原文回传）、
markdown 结构、以及「只读」的结构性约束。
"""

from __future__ import annotations

import pathlib

import pytest

from nanobot_quant.tools import tools_ashare as ta


def _ok(timeout, echo_samples):
    return ta._row("fake_ok", "假源-可用", "ok", 12, "HTTP 200 · 42B", "样例数据")


def _boom(timeout, echo_samples):
    raise RuntimeError("HTTP 403：被拒（原文照抄）")


def _expected_fail(timeout, echo_samples):
    return ta._row("fake_expected", "假源-已知边界", "fail", 5, "HTTP 403（预期）", expect="预期失败")


@pytest.fixture()
def fake_checks(monkeypatch):
    monkeypatch.setattr(ta, "_CHECKS", (
        ("fake_ok", "假源-可用", _ok),
        ("fake_bad", "假源-报错", _boom),
        ("fake_expected", "假源-已知边界", _expected_fail),
    ))
    return ta.probe_ashare_sources()


def test_probe_fail_soft_keeps_other_sources(fake_checks):
    assert fake_checks["total"] == 3
    assert fake_checks["ok"] == 1
    assert fake_checks["failed"] == 2                   # 含 1 个「预期失败」
    assert fake_checks["unexpected_failures"] == ["fake_bad"]   # 只有它算真问题
    by_src = {r["source"]: r for r in fake_checks["sources"]}
    assert by_src["fake_bad"]["status"] == "fail"
    assert "HTTP 403：被拒（原文照抄）" in by_src["fake_bad"]["detail"]   # fail-visible
    assert by_src["fake_bad"]["display"] == "假源-报错"   # 失败行仍用注册表 display


def test_probe_reports_environment(fake_checks):
    assert "space=" in fake_checks["environment"] and "host=" in fake_checks["environment"]


def test_markdown_shape(fake_checks):
    md = fake_checks["markdown"]
    assert md.startswith("## 🧪 A股 数据源可达性体检（只读）")
    assert "1/3 可用" in md
    assert "| `fake_ok` |" in md and "| `fake_bad` |" in md
    assert "**样例**" in md and "样例数据" in md
    assert "换空间/换机房后请重跑" in md


def test_markdown_flags_unexpected_failure(fake_checks):
    assert "非预期失败" in fake_checks["markdown"]        # _boom 是未预期失败
    # 「预期失败」的源不应被列为告警
    assert "`fake_expected`" not in fake_checks["markdown"].split("**说明**")[-1]


def test_markdown_without_unexpected_failure(monkeypatch):
    monkeypatch.setattr(ta, "_CHECKS", (
        ("fake_ok", "假源-可用", _ok),
        ("fake_expected", "假源-已知边界", _expected_fail),
    ))
    res = ta.probe_ashare_sources(echo_samples=False)
    assert "非预期失败" not in res["markdown"]
    assert res["ok"] == 1 and res["failed"] == 1


def test_checks_registry_covers_key_sources():
    names = [c[0] for c in ta._CHECKS]
    assert names[0] == "sse_yunhq"      # 主力源排第一
    for required in ("sina_quote", "sina_kline", "sina_futures",
                     "tencent_quote", "hcvix", "sse_official", "szse_official"):
        assert required in names
    assert all(len(c) == 3 for c in ta._CHECKS)   # (source, display, fn)


def test_expected_fail_sources_are_documented_as_boundaries():
    """两个已知边界源必须在 md 里注明「预期失败」，避免被当成回归。"""
    assert ta._check_sse_official.__doc__ and "403" in ta._check_sse_official.__doc__
    assert ta._check_szse_official.__doc__ and "reset" in ta._check_szse_official.__doc__


# ── 只读结构约束（与 tools_iv 同款：源码不得出现交易路径符号）──────────

_TRADING_SYMBOLS = ("execute_signal", "submit_order", "place_order", "create_order",
                    "import lumibot", "ledger", "batches", "option_params",
                    "submit_swap", "wallet_switch")


@pytest.mark.parametrize("rel", [
    "src/nanobot_quant/tools/tools_ashare.py",
    "src/nanobot_quant/data_sources/sse_options.py",
])
def test_source_is_read_only(rel):
    src = pathlib.Path(rel).read_text(encoding="utf-8")
    hits = [s for s in _TRADING_SYMBOLS if s in src]
    assert not hits, f"{rel} 命中交易路径符号：{hits}（本模块必须只读）"
