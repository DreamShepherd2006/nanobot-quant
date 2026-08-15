"""Tests for the data-source registry (docs/quant-system.md §6.1).

Covers the unified DataSourceSpec contract, channel→source binding, and
the thin per-source wrappers (mock the underlying data-access layer).
"""

import pandas as pd
import pytest

from nanobot_quant.data_sources import (
    CHANNEL_DATA_SOURCE,
    data_source_for_channel,
    executable_sources,
    get_data_source,
    list_data_sources,
    research_sources,
)


# ── 注册表形状 ────────────────────────────────────────────────────────


def test_registry_contains_all_five_sources():
    names = set(list_data_sources())
    assert names == {"gate_cex", "onchainos", "okx_cex", "eastmoney", "yfinance"}


def test_kind_split():
    assert executable_sources() == ["gate_cex", "onchainos"]
    assert set(research_sources()) == {"okx_cex", "eastmoney", "yfinance"}


def test_okx_cex_is_research_until_execution_integrated():
    """OKX CEX 保留为注册表成员，但现阶段不参与执行（业务量化未完成）。"""
    spec = get_data_source("okx_cex")
    assert spec.kind == "research"
    assert spec.exchange == "okx"


def test_channel_binding_structural_same_source():
    assert CHANNEL_DATA_SOURCE == {"dex": "onchainos", "cex": "gate_cex"}
    assert data_source_for_channel("cex").name == "gate_cex"
    assert data_source_for_channel("dex").name == "onchainos"


def test_unknown_channel_fail_closed():
    with pytest.raises(KeyError):
        data_source_for_channel("binance")


def test_unknown_source_raises():
    with pytest.raises(KeyError):
        get_data_source("nope")


# ── 统一契约 ──────────────────────────────────────────────────────────


def test_spec_not_implemented_methods():
    spec = get_data_source("eastmoney")  # research: no get_price/order_book
    with pytest.raises(NotImplementedError):
        spec.get_price("AAPL")
    with pytest.raises(NotImplementedError):
        spec.order_book("AAPL")


def test_spec_get_price_fail_closed(monkeypatch):
    spec = get_data_source("gate_cex")

    def bad_price(symbol):
        raise RuntimeError("ticker down")

    monkeypatch.setattr(spec, "_get_price", bad_price)
    assert spec.get_price("CRCLX") == 0.0


# ── gate_cex ──────────────────────────────────────────────────────────


def _rows(n=3, start_ts=1755000000):
    return pd.DataFrame([
        {"time": pd.to_datetime(start_ts + i * 60, unit="s"),
         "open": 70 + i, "high": 71 + i, "low": 69 + i,
         "close": 70.5 + i, "volume": 100 + i}
        for i in range(n)]).set_index("time")


def test_gate_cex_fetch_kline(monkeypatch):
    import nanobot_quant.data_sources.gate_cex as m
    from nanobot_quant.data_sources.gate_cex import fetch_kline

    monkeypatch.setattr(m, "gate_pair", lambda s, t: "CRCLX_USDT")
    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "fetch_gate_kline",
                        lambda pair, bar="1D", limit=120: pd.DataFrame(_rows()))
    df = fetch_kline("CRCLX", bar="1m", limit=3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_gate_cex_get_price(monkeypatch):
    import nanobot_quant.data_sources.gate_cex as m
    from nanobot_quant.data_sources.gate_cex import get_price

    monkeypatch.setattr(m, "gate_pair", lambda s, t: "CRCLX_USDT")
    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "fetch_gate_ticker",
                        lambda pair: {"currency_pair": pair, "last": "73.31"})
    assert get_price("CRCLX") == 73.31


def test_gate_cex_order_book(monkeypatch):
    import nanobot_quant.data_sources.gate_cex as m
    from nanobot_quant.data_sources.gate_cex import order_book

    monkeypatch.setattr(m, "gate_pair", lambda s, t: "CRCLX_USDT")
    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "fetch_gate_order_book",
                        lambda pair, depth=5: {"best_bid": 70.0, "best_ask": 70.1})
    assert order_book("CRCLX") == {"best_bid": 70.0, "best_ask": 70.1}


# ── onchainos ─────────────────────────────────────────────────────────


def test_onchainos_fetch_kline(monkeypatch):
    import nanobot_quant.data_sources.onchainos as m
    from nanobot_quant.data_sources.onchainos import fetch_kline

    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "resolve_token",
                        lambda s, tokens_json=None: {"ok": True,
                                                     "chain": "solana",
                                                     "address": "abc"})
    monkeypatch.setattr(m, "_fetch_kline_chain",
                        lambda chain, addr, bar="1D", limit=120:
                        pd.DataFrame(_rows()))
    df = fetch_kline("SOL", bar="1D", limit=3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_onchainos_fetch_kline_resolve_failure(monkeypatch):
    from nanobot_quant.data_sources.onchainos import fetch_kline

    import nanobot_quant.data_sources.onchainos as m
    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "resolve_token",
                        lambda s, tokens_json=None: {"ok": False,
                                                     "issue": "not found"})
    with pytest.raises(RuntimeError, match="not found"):
        fetch_kline("XYZZY")


def test_onchainos_get_price(monkeypatch):
    import nanobot_quant.data_sources.onchainos as m
    from nanobot_quant.data_sources.onchainos import get_price

    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "resolve_token",
                        lambda s, tokens_json=None: {"ok": True,
                                                     "chain": "solana",
                                                     "address": "abc"})
    monkeypatch.setattr(m, "_cli_price",
                        lambda s, chain="solana", tokens_json=None: "77.25")
    assert get_price("SOL") == 77.25


# ── okx_cex ───────────────────────────────────────────────────────────


def test_okx_cex_fetch_kline(monkeypatch):
    import nanobot_quant.data_sources.okx_cex as m
    from nanobot_quant.data_sources.okx_cex import fetch_kline

    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "okx_ticker", lambda s, t: "XSPCX-USDT")
    monkeypatch.setattr(m, "_fetch_kline_okx",
                        lambda inst, bar="1D", limit=120: pd.DataFrame(_rows()))
    df = fetch_kline("SPCX", bar="1D", limit=3)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_okx_cex_get_price(monkeypatch):
    import nanobot_quant.data_sources.okx_cex as m
    from nanobot_quant.data_sources.okx_cex import get_price

    monkeypatch.setattr(m, "load_tokens_json", lambda: [])
    monkeypatch.setattr(m, "okx_ticker", lambda s, t: "XSPCX-USDT")
    monkeypatch.setattr(m, "fetch_ticker", lambda inst: {"last": "137.28"})
    assert get_price("SPCX") == 137.28


# ── 股票源映射逻辑 ────────────────────────────────────────────────────


def test_stock_secid_mapping():
    from nanobot_quant.data_sources.eastmoney import stock_secid
    assert stock_secid("AAPL") == "105.AAPL"
    assert stock_secid("510050") == "1.510050"   # 沪市 ETF
    assert stock_secid("600000") == "1.600000"   # 沪市 A 股
    assert stock_secid("159915") == "0.159915"   # 深市 ETF
    assert stock_secid("000001") == "0.000001"   # 深市 A 股


def test_yf_symbol_mapping():
    from nanobot_quant.data_sources.yfinance import yf_symbol
    assert yf_symbol("AAPL") == "AAPL"
    assert yf_symbol("510050") == "510050.SS"
    assert yf_symbol("000001") == "000001.SZ"


def test_eastmoney_fetch_kline(monkeypatch):
    import nanobot_quant.data_sources.eastmoney as m
    from nanobot_quant.data_sources.eastmoney import fetch_kline

    body = ('{"data":{"klines":["2026-08-14,100,101,102,99,1000",'
            '"2026-08-15,101,102,103,100,1100"]}}')

    class _FakeResp:
        def __init__(self, b):
            self._b = b.encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=20: _FakeResp(body))
    df = fetch_kline("AAPL", bar="1D", limit=10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None  # 美股日线 → America/New_York


def test_yfinance_fetch_kline(monkeypatch):
    import nanobot_quant.data_sources.yfinance as m
    from nanobot_quant.data_sources.yfinance import fetch_kline

    idx = pd.to_datetime(["2026-08-14", "2026-08-15"])
    raw = pd.DataFrame({
        ("Open", "AAPL"): [99.0, 100.0], ("High", "AAPL"): [102.0, 103.0],
        ("Low", "AAPL"): [98.0, 99.0], ("Close", "AAPL"): [101.0, 102.0],
        ("Volume", "AAPL"): [1e6, 1.1e6],
    }, index=idx)
    raw.columns = pd.MultiIndex.from_tuples(raw.columns)
    monkeypatch.setattr(m.yf, "download", lambda *a, **k: raw)
    df = fetch_kline("AAPL", bar="1D", limit=10)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


# ── td_table 组合消费（stock = eastmoney 主源 + yfinance 兜底）────────


def _td_table():
    import nanobot_quant.td_table_handlers as m
    return m


def test_td_table_stock_combo_eastmoney_ok(monkeypatch):
    m = _td_table()
    calls = []

    class _Em:
        def fetch_kline(self, ticker, bar="1D", limit=60, start=None, end=None):
            calls.append("em")
            return pd.DataFrame(_rows())

    class _Yf:
        def fetch_kline(self, ticker, bar="1D", limit=60, start=None, end=None):
            calls.append("yf")
            raise AssertionError("should not be called")

    monkeypatch.setattr(m, "get_data_source", lambda name: {"eastmoney": _Em(),
                                                            "yfinance": _Yf()}[name])
    df = m._fetch_stock_kline("AAPL", bar="1D", limit=3)
    assert calls == ["em"]
    assert len(df) == 3


def test_td_table_stock_combo_yfinance_fallback(monkeypatch):
    m = _td_table()
    calls = []

    class _Em:
        def fetch_kline(self, ticker, bar="1D", limit=60, start=None, end=None):
            calls.append("em")
            raise RuntimeError("东财挂")

    class _Yf:
        def fetch_kline(self, ticker, bar="1D", limit=60, start=None, end=None):
            calls.append("yf")
            return pd.DataFrame(_rows())

    monkeypatch.setattr(m, "get_data_source", lambda name: {"eastmoney": _Em(),
                                                            "yfinance": _Yf()}[name])
    df = m._fetch_stock_kline("AAPL", bar="1D", limit=3)
    assert calls == ["em", "yf"]
    assert len(df) == 3


def test_td_table_stock_combo_all_fail(monkeypatch):
    m = _td_table()

    class _Both:
        def fetch_kline(self, ticker, bar="1D", limit=60, start=None, end=None):
            raise RuntimeError("down")

    monkeypatch.setattr(m, "get_data_source", lambda name: _Both())
    with pytest.raises(RuntimeError, match="东财: down"):
        m._fetch_stock_kline("AAPL", bar="1D", limit=3)
