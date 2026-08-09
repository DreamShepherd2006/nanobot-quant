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


class TestMapTimestep:
    """B3: timestep → OKX bar format must use 1m/5m/15m (not 1Min/5Min).

    "1Min" triggered 51000 Parameter bar error in the live 5m loop.
    """

    @pytest.mark.parametrize(
        "timestep,bar",
        [
            ("minute", "1m"),
            ("5min", "5m"),
            ("15min", "15m"),
            ("hour", "1H"),
            ("4hour", "4H"),
            ("day", "1D"),
            ("week", "1W"),
        ],
    )
    def test_maps_to_okx_bar(self, timestep, bar):
        assert OnchainOSDataSource._map_timestep(timestep) == bar

    def test_unknown_falls_back_to_day(self):
        assert OnchainOSDataSource._map_timestep("decade") == "1D"


class TestGetHistoricalPrices:
    def test_returns_bars_with_asset(self, ds, monkeypatch):
        candles = [
            {"ts": 1700000000000 + i * 86400000, "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "vol": 100.0}
            for i in range(5)
        ]
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_kline",
            lambda addr, bar, limit, chain="solana": candles,
        )
        asset = _asset()
        bars = ds.get_historical_prices(
            asset, length=5, timestep="day", exchange=None, return_polars=False
        )
        assert bars is not None
        assert bars.asset is asset
        assert len(bars.df) == 5
        assert set(bars.df.columns) >= {"open", "high", "low", "close", "volume"}
        assert bars.df.index.tz is not None  # shared parser keeps UTC tz

    def test_accepts_string_ohlcv_values(self, ds, monkeypatch):
        candles = [
            {"ts": 1700000000000 + i * 86400000, "o": "10.0", "h": "11.0", "l": "9.0", "c": "10.5", "vol": "100.0"}
            for i in range(5)
        ]
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_kline",
            lambda addr, bar, limit, chain="solana": candles,
        )
        bars = ds.get_historical_prices(
            _asset(), length=5, timestep="day", exchange=None, return_polars=False
        )
        assert bars is not None
        assert len(bars.df) == 5
        assert bars.df["close"].dtype.kind in "fiu"  # numeric, not object

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
            lambda addr, bar, limit, chain="solana": [],
        )
        with pytest.raises(RuntimeError, match="No kline data returned"):
            ds.get_historical_prices(_asset(), length=5, timestep="day")

    def test_forwards_entry_chain_to_kline(self, monkeypatch):
        """Per-target chain (tokens.json entry) is forwarded to get_kline —
        a BNB target fetches on chain 56, not solana."""
        captured = {}

        def fake_kline(addr, bar, limit, chain="solana"):
            captured["chain"] = chain
            return []

        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_kline",
            fake_kline,
        )
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.token_chain",
            lambda symbol, tokens_json: "bnb",
        )
        ds = OnchainOSDataSource(
            tokens_json=[{"symbol": "SPCXB", "chain": "bnb",
                          "address": "0xbe000000000000000000000000000000000003e1"}]
        )
        with pytest.raises(RuntimeError):
            ds.get_historical_prices(_asset("SPCXB"), length=5, timestep="day")
        assert captured["chain"] == "bnb"

    def test_get_last_price_forwards_chain(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.resolve_token_address",
            lambda symbol, tokens_json: "0xbe000000000000000000000000000000000003e1",
        )
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.token_chain",
            lambda symbol, tokens_json: "bnb",
        )
        monkeypatch.setattr(
            "nanobot_quant.data.onchainos_data_source.get_token_price",
            lambda addr, chain="solana": (
                captured.update(chain=chain) or 137.08
            ),
        )
        ds = OnchainOSDataSource(tokens_json=[])
        price = ds.get_last_price(_asset("SPCXB"))
        assert price == 137.08
        assert captured["chain"] == "bnb"
