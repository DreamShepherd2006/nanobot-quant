"""C144（2026-11-17）：取数侧计价币必须跟随执行通道。

背景：OKX 同一标的常有两组现货对（``XCRCL-USDT`` / ``XCRCL-USDC``），两个
订单簿价格不同（实测 |基差| 中位 0.121%、p95 0.482%）。修复前数据源用
``inst_id_for(ticker)`` 的默认 USDT、broker 用 ``quote_ccy`` 拼 USDC ⇒
「按 USDT 盘算信号、在 USDC 盘成交」。

本文件锁定三件事：
1. ``brokers.registry.effective_quote`` 的取值规则（okx_cex 读 quote_ccy，
   其余通道固定 USDT，未知/非法一律 USDT —— 零回归）；
2. ``okx_cex_data.data_quote`` 与之同源；
3. ``okx_cex_data.resolve_data_inst`` 用该计价币解析 instId，缺该币对时
   回退 USDT 并告警一次；且 fetch_* 全部走它（源码里不再有裸 ``inst_id_for``）。
"""
from __future__ import annotations

import ast
import inspect

import pytest

from nanobot_quant.brokers.registry import effective_quote
from nanobot_quant import okx_cex_data


# ── 1. effective_quote 取值规则 ───────────────────────────────────────────

@pytest.mark.parametrize("params,expected", [
    ({"execution_channel": "okx_cex", "quote_ccy": "USDC"}, "USDC"),
    ({"execution_channel": "okx_cex", "quote_ccy": "usdc"}, "USDC"),
    ({"execution_channel": "okx_cex", "quote_ccy": "USDT"}, "USDT"),
    ({"execution_channel": "okx_cex"}, "USDT"),          # 未配置 → 默认 USDT
    ({"execution_channel": "okx_cex", "quote_ccy": "EUR"}, "USDT"),  # 非法值
])
def test_effective_quote_okx_cex_follows_quote_ccy(params, expected):
    assert effective_quote(params=params) == expected


@pytest.mark.parametrize("params", [
    {"execution_channel": "gate", "quote_ccy": "USDC"},
    {"execution_channel": "okx_dex", "quote_ccy": "USDC"},
])
def test_effective_quote_other_channels_ignore_quote_ccy(params):
    """Gate 恒 USDT、DEX 走链上 USDC —— quote_ccy 只对 OKX CEX 生效。"""
    q = effective_quote(params=params)
    assert q in ("USDT", "USDC")
    assert q == effective_quote(channel=params["execution_channel"], params=params)


def test_effective_quote_gate_is_usdt():
    assert effective_quote("gate", {"quote_ccy": "USDC"}) == "USDT"


def test_effective_quote_unknown_channel_is_usdt():
    assert effective_quote("no_such_channel", {}) == "USDT"


def test_effective_quote_reads_exec_params_when_params_omitted(monkeypatch):
    import nanobot_quant.exec_params as ep

    monkeypatch.setattr(ep, "load_exec_params", lambda: {
        "execution_channel": "okx_cex", "quote_ccy": "USDC"})
    assert effective_quote() == "USDC"


def test_effective_quote_survives_broken_exec_params(monkeypatch):
    import nanobot_quant.exec_params as ep

    def _boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(ep, "load_exec_params", _boom)
    assert effective_quote() == "USDT"


# ── 2/3. 取数侧同源解析 ───────────────────────────────────────────────────

@pytest.fixture
def usdc_env(monkeypatch):
    """把「当前执行通道」伪装成 okx_cex + USDC。"""
    import nanobot_quant.exec_params as ep

    monkeypatch.setattr(ep, "load_exec_params", lambda: {
        "execution_channel": "okx_cex", "quote_ccy": "USDC"})


def test_data_quote_matches_effective_quote(usdc_env):
    assert okx_cex_data.data_quote() == "USDC"


def test_data_quote_defaults_to_usdt(monkeypatch):
    import nanobot_quant.exec_params as ep

    monkeypatch.setattr(ep, "load_exec_params", lambda: {})
    assert okx_cex_data.data_quote() == "USDT"


def test_data_quote_survives_broken_config(monkeypatch):
    import nanobot_quant.exec_params as ep

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(ep, "load_exec_params", _boom)
    assert okx_cex_data.data_quote() == "USDT"


def _fake_inst_id_for(pairs):
    calls = []

    def _f(ticker, quote=None, session=None, strict=False):
        calls.append((ticker, quote, strict))
        return pairs.get((ticker, quote), "")

    _f.calls = calls
    return _f


def test_resolve_data_inst_uses_channel_quote(usdc_env, monkeypatch):
    fake = _fake_inst_id_for({("XCRCL", "USDC"): "XCRCL-USDC"})
    monkeypatch.setattr(okx_cex_data, "inst_id_for", fake)
    assert okx_cex_data.resolve_data_inst("XCRCL") == "XCRCL-USDC"
    assert fake.calls[0][:2] == ("XCRCL", "USDC")


def test_resolve_data_inst_falls_back_to_usdt_with_warning(usdc_env, monkeypatch):
    """XAUT 这类只有 -USDT 盘的标的：回退 USDT，但必须显式告警（不静默）。"""
    fake = _fake_inst_id_for({("XAUT", "USDT"): "XAUT-USDT"})
    monkeypatch.setattr(okx_cex_data, "inst_id_for", fake)
    warned = []
    monkeypatch.setattr(okx_cex_data, "_warn_once", warned.append)

    assert okx_cex_data.resolve_data_inst("XAUT") == "XAUT-USDT"
    assert warned and "回退 XAUT-USDT" in warned[0]


def test_resolve_data_inst_usdt_channel_never_warns(monkeypatch):
    """通道本身就是 USDT 时不该有「回退」告警（默认零回归）。"""
    import nanobot_quant.exec_params as ep

    monkeypatch.setattr(ep, "load_exec_params", lambda: {
        "execution_channel": "okx_cex", "quote_ccy": "USDT"})
    fake = _fake_inst_id_for({("XCRCL", "USDT"): "XCRCL-USDT"})
    monkeypatch.setattr(okx_cex_data, "inst_id_for", fake)
    warned = []
    monkeypatch.setattr(okx_cex_data, "_warn_once", warned.append)

    assert okx_cex_data.resolve_data_inst("XCRCL") == "XCRCL-USDT"
    assert warned == []


def test_resolve_data_inst_is_idempotent_for_full_inst_id(usdc_env, monkeypatch):
    """期权/补买侧传的完整 instId（含 quote）必须原样透传，不被改写。"""
    fake = _fake_inst_id_for({("XAUT-USDT", "USDC"): "XAUT-USDT"})
    monkeypatch.setattr(okx_cex_data, "inst_id_for", fake)
    assert okx_cex_data.resolve_data_inst("XAUT-USDT") == "XAUT-USDT"


# ── fetch_* 全部走 resolve_data_inst（结构断言 + 行为断言）───────────────

def test_no_bare_inst_id_for_left_in_module():
    """fetch_* 必须统一走 resolve_data_inst（按 AST 查真实的函数调用）。"""
    tree = ast.parse(inspect.getsource(okx_cex_data))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "inst_id_for":
            continue
        if len(node.args) == 1 and isinstance(node.args[0], ast.Name) \
                and node.args[0].id == "ticker" and not node.keywords:
            bad.append(node.lineno)
    assert bad == [], f"仍有裸 inst_id_for(ticker)：行 {bad}"


def _fake_session(captured):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "0", "data": [{
                "last": "1.0", "open24h": "1", "high24h": "1",
                "low24h": "1", "vol24h": "1"}]}

    class _Sess:
        def get(self, url, params=None, timeout=None):
            captured.append(params)
            return _Resp()

    return _Sess()


def test_fetch_ticker_requests_channel_quote(usdc_env, monkeypatch):
    monkeypatch.setattr(okx_cex_data, "_RATE_DELAY", 0)
    monkeypatch.setattr(okx_cex_data, "inst_id_for",
                        _fake_inst_id_for({("XCRCL", "USDC"): "XCRCL-USDC"}))
    captured = []
    okx_cex_data.fetch_ticker("XCRCL", session=_fake_session(captured))
    assert captured and captured[0]["instId"] == "XCRCL-USDC"


def test_fetch_order_book_requests_channel_quote(usdc_env, monkeypatch):
    monkeypatch.setattr(okx_cex_data, "_RATE_DELAY", 0)
    monkeypatch.setattr(okx_cex_data, "inst_id_for",
                        _fake_inst_id_for({("XCRCL", "USDC"): "XCRCL-USDC"}))
    captured = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "0", "data": [{"bids": [], "asks": []}]}

    class _Sess:
        def get(self, url, params=None, timeout=None):
            captured.append(params)
            return _Resp()

    okx_cex_data.fetch_order_book("XCRCL", depth=5, session=_Sess())
    assert captured and captured[0]["instId"] == "XCRCL-USDC"
