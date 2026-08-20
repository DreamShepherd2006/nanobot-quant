"""CEX DataSource unit tests (mock Gate fetchers — no network).

Covered:
- get_historical_prices signature contract (exchange / return_polars kwargs
  required by lumibot v4.5.78 Strategy.get_historical_prices)
- gate_pair mapping applied to fetch_gate_kline
- Bars carries source + asset; get_last_price from Gate ticker (public)
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
        "gate_symbol": "CRCLXUSDT",
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
def fake(monkeypatch):
    fake = _FakeGate(kline=_df())
    monkeypatch.setattr(
        "nanobot_quant.data.cex_data_source.get_data_source",
        lambda name: fake,
    )
    return fake


@pytest.fixture
def ds(fake):
    return CexDataSource(tokens_json=TOKENS)


class _FakeGate:
    """Registry data source stub — per-test behaviour via attributes."""

    def __init__(self, kline=None, price=None):
        self.kline = kline
        self.price = price
        self.kline_calls = []
        self.price_calls = []

    def fetch_kline(self, symbol, bar="1D", limit=120, start=None, end=None):
        self.kline_calls.append({"symbol": symbol, "bar": bar, "limit": limit})
        if self.kline is None:
            return None  # 模拟真实源网络失败返回 None（DataSource 的 if 检查触发）
        return self.kline.iloc[-limit:]  # 真实源语义：返回最近 limit 根

    def get_price(self, symbol):
        self.price_calls.append(symbol)
        return self.price


class TestLiveLoopSignature:
    """lumibot v4.5.78 calls get_historical_prices with exchange /
    return_polars kwargs — signature must accept them."""

    def test_signature_accepts_kwargs(self, ds):
        sig = inspect.signature(ds.get_historical_prices)
        params = set(sig.parameters)
        assert "exchange" in params
        assert "return_polars" in params


class TestGetHistoricalPrices:
    def test_maps_gate_pair(self, ds, fake):
        """symbol 原样传注册表源；pair 映射（gate_symbol 优先）在源内完成。"""
        bars = ds.get_historical_prices(_asset("CRCLX"), length=2, timestep="day")
        call = fake.kline_calls[-1]
        assert call["symbol"] == "CRCLX"
        assert call["bar"] == "1D"
        assert call["limit"] == 2
        assert bars.source == "GATE_CEX"
        assert bars.asset.symbol == "CRCLX"

    def test_bars_columns_lowercased(self, ds, fake):
        """A 修复回归：lumibot Bars 契约要求小写列（df['close'] 派生 return 列）。

        gate_cex 数据源输出大写列（Open/High/...），CexDataSource 须在构造
        Bars 前小写化，否则真实 v4.5.78 抛 KeyError: 'close'。
        """
        bars = ds.get_historical_prices(_asset("CRCLX"), length=2, timestep="day")
        assert list(bars.df.columns) == ["open", "high", "low", "close", "volume"]

    def test_drops_in_progress_bars_contract(self, ds, fake):
        """A 修复第二部分契约：gate_cex 数据源已过滤进行中 bar（closed=false）。

        策略层据此不再多拉 1 根/再丢弃（双重丢弃 → bars=119 永久 SKIP）。
        """
        assert ds.drops_in_progress_bars is True

    def test_timestep_mapping(self, ds, fake):
        ds.get_historical_prices(_asset(), length=5, timestep="5min")
        assert fake.kline_calls[-1]["bar"] == "5m"

    def test_bar_prefix_passthrough(self, ds, fake):
        """bar: 前缀 = live 直拉场景粒度（策略对 live broker 添加，lumibot
        无法解析 → 原样透传）；数据源 removeprefix 后按原生粒度直拉（如
        "bar:5min" → 5m、"bar:minute" → 1m），绕开 lumibot multi-timeframe
        转换（600 根 1m + resample）。回测（无前缀）路径不受影响。"""
        ds.get_historical_prices(_asset(), length=120, timestep="bar:5min")
        assert fake.kline_calls[-1]["bar"] == "5m"
        assert fake.kline_calls[-1]["limit"] == 120  # 直拉 5m 120 根，非 600×1m
        ds.get_historical_prices(_asset(), length=120, timestep="bar:minute")
        assert fake.kline_calls[-1]["bar"] == "1m"

    def test_length_clamped_to_1000(self, ds, fake):
        ds.get_historical_prices(_asset(), length=5000, timestep="day")
        assert fake.kline_calls[-1]["limit"] == 1000

    def test_empty_kline_raises(self, ds, fake):
        fake.kline = None
        with pytest.raises(RuntimeError, match="No Gate CEX kline"):
            ds.get_historical_prices(_asset(), length=2, timestep="day")


class TestGetLastPrice:
    def test_ok(self, ds, fake):
        fake.price = 74.94
        assert ds.get_last_price(_asset("CRCLX")) == pytest.approx(74.94)
        assert fake.price_calls == ["CRCLX"]

    def test_empty_ticker(self, ds, fake):
        fake.price = None
        assert ds.get_last_price(_asset()) is None

class TestIncrementalCache:
    """S2 增量 K 线缓存（docs/quant-system.md §22.6）—— 数据源集成。"""

    def _df(self, n=130):
        # end=now：elapsed 自适应增量按真实墙钟算缺口，缓存尾必须≈now
        # （真实场景每次拉取更新缓存尾）；固定日期会让第二次调用误判 gap。
        end = pd.Timestamp.now(tz="UTC")
        idx = pd.date_range(end=end, periods=n, freq="60s", tz="UTC")
        return pd.DataFrame({
            "Open": [70.0 + i * 0.01 for i in range(n)],
            "High": [72.0 + i * 0.01 for i in range(n)],
            "Low": [69.0 + i * 0.01 for i in range(n)],
            "Close": [71.5 + i * 0.01 for i in range(n)],
            "Volume": [1000.0] * n,
        }, index=idx)

    def test_first_full_then_incremental(self, fake):
        fake.kline = self._df()
        ds = CexDataSource(tokens_json=TOKENS)  # 默认 use_cache=True
        bars1 = ds.get_historical_prices(_asset(), length=120, timestep="minute")
        assert fake.kline_calls[-1]["limit"] == 120       # 首轮全量预取
        assert len(bars1.df) == 120
        bars2 = ds.get_historical_prices(_asset(), length=120, timestep="minute")
        assert fake.kline_calls[-1]["limit"] == 2         # 次轮增量
        assert len(bars2.df) == 120                        # 缓存尾部返回
        assert list(bars2.df.columns) == ["open", "high", "low", "close", "volume"]

    def test_use_cache_false_full_every_time(self, fake):
        fake.kline = self._df()
        ds = CexDataSource(tokens_json=TOKENS, use_cache=False)
        for _ in range(2):
            ds.get_historical_prices(_asset(), length=120, timestep="minute")
        assert fake.kline_calls[-1]["limit"] == 120
