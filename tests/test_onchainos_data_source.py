"""OnchainOSDataSource tests — mock the data-source registry (gate_cex style).

2026-08-15: the Lumibot DataSource now consumes the data-source registry
(onchainos source) instead of calling onchainos_cli / onchainos_data
directly; per-target chain resolution moved inside the source.
"""

import inspect

import pandas as pd
import pytest

from nanobot_quant.data.onchainos_data_source import OnchainOSDataSource


def _asset(symbol="SOL"):
    from lumibot.entities import Asset

    return Asset(symbol=symbol, asset_type="crypto")


def _df(n=5, tz="UTC"):
    idx = pd.to_datetime([1700000000000 + i * 86400000 for i in range(n)],
                         unit="ms")
    if tz:
        idx = idx.tz_localize(tz)
    return pd.DataFrame({
        "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n,
        "close": [10.5] * n, "volume": [100.0] * n,
    }, index=idx)


class _FakeOnchain:
    """Registry data source stub — per-test behaviour via attributes."""

    def __init__(self, kline=None, price=137.08, resolve_error=None):
        self.kline = kline
        self.price = price
        self.resolve_error = resolve_error
        self.kline_calls = []
        self.price_calls = []

    def fetch_kline(self, symbol, bar="1D", limit=120, start=None, end=None):
        self.kline_calls.append({"symbol": symbol, "bar": bar, "limit": limit,
                                 "start": start, "end": end})
        if self.resolve_error:
            raise RuntimeError(self.resolve_error)
        if self.kline is None:
            raise RuntimeError("no kline data")
        return self.kline.iloc[-limit:]  # 真实源语义：返回最近 limit 根

    def get_price(self, symbol):
        self.price_calls.append(symbol)
        return self.price


@pytest.fixture
def fake(monkeypatch):
    fake = _FakeOnchain(kline=_df())
    monkeypatch.setattr(
        "nanobot_quant.data.onchainos_data_source.get_data_source",
        lambda name: fake,
    )
    return fake


@pytest.fixture
def ds(fake):
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
    """B3: timestep → OKX bar format must use 1m/5m/15m (not 1Min/5Min)."""

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
    def test_returns_bars_with_asset(self, ds, fake):
        fake.kline = _df(tz="UTC")
        asset = _asset()
        bars = ds.get_historical_prices(
            asset, length=5, timestep="day", exchange=None, return_polars=False
        )
        assert bars is not None
        assert bars.asset is asset
        assert len(bars.df) == 5
        assert set(bars.df.columns) >= {"open", "high", "low", "close", "volume"}
        assert bars.df.index.tz is not None  # shared parser keeps UTC tz

    def test_accepts_string_ohlcv_values(self, ds, fake):
        from nanobot_quant.onchainos_data import parse_kline_response

        candles = [
            {"ts": 1700000000000 + i * 86400000, "o": "10.0", "h": "11.0",
             "l": "9.0", "c": "10.5", "vol": "100.0"}
            for i in range(5)
        ]
        fake.kline = parse_kline_response(candles)
        bars = ds.get_historical_prices(
            _asset(), length=5, timestep="day", exchange=None, return_polars=False
        )
        assert bars is not None
        assert len(bars.df) == 5
        assert bars.df["close"].dtype.kind in "fiu"  # numeric, not object

    def test_unresolvable_token_raises(self, ds, fake):
        fake.resolve_error = "Cannot resolve token address"
        with pytest.raises(RuntimeError, match="Cannot resolve token address"):
            ds.get_historical_prices(_asset(), length=5, timestep="day")

    def test_empty_kline_raises(self, ds, fake):
        fake.kline = pd.DataFrame()
        with pytest.raises(RuntimeError, match="No kline data returned"):
            ds.get_historical_prices(_asset(), length=5, timestep="day")

    def test_forwards_symbol_to_registry(self, ds, fake):
        """Chain resolution moved inside the registry source — the DataSource
        forwards the raw symbol + bar + limit."""
        fake.kline = _df()
        ds.get_historical_prices(_asset("SPCXB"), length=5, timestep="day")
        call = fake.kline_calls[-1]
        assert call["symbol"] == "SPCXB"
        assert call["bar"] == "1D"
        assert call["limit"] == 5

    def test_bar_prefix_passthrough(self, ds, fake):
        """bar: 前缀 = live 直拉场景粒度（策略对 live broker 添加，lumibot
        无法解析 → 原样透传）；数据源 removeprefix 后直拉原生 bar（如
        "bar:5min" → 5m），绕开 lumibot multi-timeframe 转换。"""
        fake.kline = _df()
        ds.get_historical_prices(_asset(), length=120, timestep="bar:5min")
        assert fake.kline_calls[-1]["bar"] == "5m"
        assert fake.kline_calls[-1]["limit"] == 120

    def test_get_last_price_via_registry(self, ds, fake):
        assert ds.get_last_price(_asset("SOL")) == 137.08
        assert fake.price_calls == ["SOL"]

    def test_get_last_price_none_when_zero(self, ds, fake):
        fake.price = None
        assert ds.get_last_price(_asset("SOL")) is None
