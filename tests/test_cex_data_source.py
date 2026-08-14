"""P1: CexDataSource unit tests (mock OKX CEX fetchers — no network).

Covered:
- get_historical_prices signature contract (exchange / return_polars kwargs
  required by lumibot v4.5.78 Strategy.get_historical_prices)
- okx_symbol mapping applied to fetch_kline
- Bars carries source + asset; get_last_price from OKX ticker
"""

import inspect

import pandas as pd
import pytest

from nanobot_quant.data.cex_data_source import CexDataSource

TOKENS = [
    {
        "symbol": "CRCLX",
        "chain": "solana",
        "address": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
        "gate_symbol": "CRCLX",
        "okx_symbol": "XCRCL",
    }
]


def _asset(symbol="CRCLX"):
    from lumibot.entities import Asset

    return Asset(symbol=symbol, asset_type="crypto")


def _df():
    idx = pd.to_datetime(["2026-08-01", "2026-08-02"])
    return pd.DataFrame(
        {
            "Open": [70.0, 71.0],
            "High": [72.0, 73.0],
            "Low": [69.0, 70.5],
            "Close": [71.5, 72.5],
            "Volume": [1000.0, 1200.0],
        },
        index=idx,
    )


@pytest.fixture
def ds():
    return CexDataSource(tokens_json=TOKENS)


class TestLiveLoopSignature:
    """lumibot v4.5.78 calls get_historical_prices with exchange /
    return_polars kwargs — signature must accept them."""

    def test_signature_accepts_kwargs(self, ds):
        sig = inspect.signature(ds.get_historical_prices)
        params = set(sig.parameters)
        assert "exchange" in params
        assert "return_polars" in params


class TestGetHistoricalPrices:
    def test_maps_okx_symbol(self, ds, monkeypatch):
        calls = {}

        def fake_fetch_kline(ticker, bar, limit):
            calls.update(ticker=ticker, bar=bar, limit=limit)
            return _df()

        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_kline", fake_fetch_kline
        )
        bars = ds.get_historical_prices(_asset("CRCLX"), length=2, timestep="day")
        assert calls["ticker"] == "XCRCL"
        assert calls["bar"] == "1D"
        assert calls["limit"] == 2
        assert bars.source == "OKX_CEX"
        assert bars.asset.symbol == "CRCLX"

    def test_timestep_mapping(self, ds, monkeypatch):
        calls = {}

        def fake_fetch_kline(ticker, bar, limit):
            calls["bar"] = bar
            return _df()

        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_kline", fake_fetch_kline
        )
        ds.get_historical_prices(_asset(), length=5, timestep="5min")
        assert calls["bar"] == "5m"

    def test_length_clamped_to_300(self, ds, monkeypatch):
        calls = {}

        def fake_fetch_kline(ticker, bar, limit):
            calls["limit"] = limit
            return _df()

        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_kline", fake_fetch_kline
        )
        ds.get_historical_prices(_asset(), length=500, timestep="day")
        assert calls["limit"] == 300

    def test_empty_kline_raises(self, ds, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_kline",
            lambda *a, **k: None,
        )
        with pytest.raises(RuntimeError, match="No OKX CEX kline"):
            ds.get_historical_prices(_asset(), length=2, timestep="day")


class TestGetLastPrice:
    def test_ok(self, ds, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_ticker",
            lambda ticker: {"last": "74.94"},
        )
        assert ds.get_last_price(_asset("CRCLX")) == pytest.approx(74.94)

    def test_empty_ticker(self, ds, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.data.cex_data_source.fetch_ticker", lambda ticker: {}
        )
        assert ds.get_last_price(_asset()) is None
