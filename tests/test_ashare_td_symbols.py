"""标的符号归一化（腾讯 kline 只认 sh/sz 前缀）与标的注册表一致性。

2026-11-25 实测：把配置代码 ``510050.SH`` 直接拼进腾讯 mkline URL 会返回空
data（rows=0），而 ``paper_runner`` 的 symbols 正是 ``xxx.SH`` 形式 —— 不归一化
就会「轮次在涨、永远无行情」。这里锁死归一化与注册表。
"""

from __future__ import annotations

import pytest

from nanobot_quant.ashare import td_panel as tp


def test_tx_code_normalizes_config_codes():
    assert tp.tx_code("510050.SH") == "sh510050"
    assert tp.tx_code("510300.SH") == "sh510300"
    assert tp.tx_code("588000.SH") == "sh588000"
    assert tp.tx_code("159915.SZ") == "sz159915"


def test_tx_code_passthrough_and_bare_codes():
    assert tp.tx_code("sh510050") == "sh510050"
    assert tp.tx_code("SH588000") == "sh588000"
    assert tp.tx_code("510050") == "sh510050"          # 沪市 5 开头
    assert tp.tx_code("000001") == "sz000001"          # 深市 0 开头
    assert tp.tx_code("") == tp.DEFAULT_SYMBOL         # 空值回退默认标的
    assert tp.tx_code(None) == tp.DEFAULT_SYMBOL


def test_fetch_klines_normalizes_symbol_before_request(monkeypatch):
    """传配置代码也必须以 sh510050 请求，且能解析 data 键。"""
    seen: dict[str, str] = {}

    def fake_get_json(url, timeout=12.0):
        seen["url"] = url
        return {"data": {"sh510050": {"m1": [
            ["202611251455", "2.86", "2.87", "2.88", "2.85", "100"],
            ["202611251456", "2.87", "2.88", "2.89", "2.86", "120"],
        ]}}}

    monkeypatch.setattr(tp, "_get_json", fake_get_json)
    df = tp.fetch_klines("m1", n=2, symbol="510050.SH")

    assert "param=sh510050,m1,,2" in seen["url"]
    assert len(df) == 2
    assert {"dt", "open", "high", "low", "close", "volume", "date"} <= set(df.columns)
    assert list(df["close"]) == [2.87, 2.88]


def test_symbols_table_covers_tape_pool():
    """看板标的表与采集器池子必须同集合（防两处漂移）。"""
    from nanobot_quant.ashare import tape

    pool = {code for code, _name in tape.POOL_UNDERLYINGS}
    assert set(tp.SYMBOLS) == pool
    for code, meta in tp.SYMBOLS.items():
        assert meta["tx"] == tp.tx_code(code)
        assert meta["engine"] in ("futu", "base")
        assert meta["name"]


def test_default_code_resolves():
    assert tp.DEFAULT_CODE in tp.SYMBOLS
    assert tp.SYMBOLS[tp.DEFAULT_CODE]["tx"] == tp.DEFAULT_SYMBOL
