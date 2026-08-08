"""B1: OnchainOSDataSource live-loop compatibility tests.

Verifies the DataSource signature contract that lumibot v4.5.78's
``Strategy.get_historical_prices`` requires (exchange / return_polars /
quote kwargs) and that returned Bars objects carry the asset — both were
broken in the initial P2 smoke test (TypeError in the live main loop).
"""

import inspect

import pandas as pd
import pytest

from nanobot_quant.data.onchainos_data_source import OnchainOSDataSource


def _asset(symbol="SOL"):
    from lumibot.entities import Asset

    return Asset(symbol=symbol, asset_type="crypto")


@pytest.fixture
def ds(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.data.onchainos_data_source.resolve_token_address",
        lambda symbol, tokens_json: "So11111111111111111111111111111111111111112",
    )
    return OnchainOSDataSource(tokens_json=[])


class TestLiveLoopSignature:
    """lumibot v4.5.78 calls data_source.get_historical_prices with
    exchange / return_polars kwargs — signature must accept them."""

    def test_get_historical_prices_accepts_exchange_and_return_polars(self, ds):
        sig = inspect.signature(ds.get_historical_prices)
        params = set(sig.parameters)
        assert "exchange" in params
        assert "return_polars" in params
        assert "include_after_hours" in params
        assert "quote" in params

    def test_get_last_price_accepts_exchange(self, ds):
        sig = inspect.signature(ds.get_last_price)
        assert "exchange" in sig.parameters


class TestGetHistoricalPrices:
    def test_returns_bars_with_asset(self, ds, monkeypatch):
        candles = [
            {"ts": 1700000000 + i * 86400, "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "vol": 100.0}
            for i in range(5)
        ]
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_kline",
            lambda addr, bar, limit: candles,
        )
        asset = _asset()
        bars = ds.get_historical_prices(
            asset, length=5, timestep="day", exchange=None, return_polars=False
        )
        assert bars is not None
        assert bars.asset is asset
        assert len(bars.df) == 5
        assert set(bars.df.columns) >= {"open", "high", "low", "close", "volume"}

    def test_unresolvable_token_raises(self, ds, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.resolve_token_address",
            lambda symbol, tokens_json: "",
        )
        with pytest.raises(ValueError, match="Cannot resolve token address"):
            ds.get_historical_prices(_asset(), length=5, timestep="day")

    def test_empty_kline_raises(self, ds, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_kline",
            lambda addr, bar, limit: [],
        )
        with pytest.raises(RuntimeError, match="No kline data returned"):
            ds.get_historical_prices(_asset(), length=5, timestep="day")
