"""get_historical_prices 的 length 上限（C95 第二层）。

2026-11-01 实盘日志实证：策略请求 2137 根（EMA 折价门 n=610 + 5τ 预热），
但 ``CexDataSource.get_historical_prices`` 里 ``min(length, 1000)`` 把它截到
1000 根 —— ``[KLINE-CACHE] prefetch n=1000``。这层 clamp 是 Gate candlesticks
单次上限时代的遗留；数据源现支持超限分页，故放宽到 _MAX_HISTORY_BARS。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.data import cex_data_source
from nanobot_quant.data.cex_data_source import CexDataSource


class _FakeCache:
    """记录 KlineCache.get 收到的 length。"""

    def __init__(self) -> None:
        self.seen: list[int] = []

    def get(self, symbol: str, bar: str, length: int):
        self.seen.append(length)
        idx = pd.date_range("2026-11-01", periods=length, freq="min", tz="UTC")
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            index=idx,
        )


@pytest.fixture
def ds(monkeypatch):
    """绕过 lumibot DataSource.__init__，只测 length 计算。"""
    obj = CexDataSource.__new__(CexDataSource)
    obj._source = "okx_cex"
    obj._bar_map = {"1m": "1m", "minute": "1m"}
    obj._cache = _FakeCache()
    return obj


def _ask(ds, length, monkeypatch):
    import lumibot.entities as ents

    calls: dict = {}

    class _Asset:
        symbol = "BTC"

    class _Bars:
        def __init__(self, df, source, asset):
            calls["rows"] = len(df)

    monkeypatch.setattr(ents, "Bars", _Bars, raising=False)
    ds.get_historical_prices(_Asset(), length=length, timestep="bar:1m")
    return calls


def test_ema_fold_pregel_length_passes_through(ds, monkeypatch):
    """2137（折价门 n=610）必须原样传给缓存，不能被截成 1000。"""
    _ask(ds, 2137, monkeypatch)

    assert ds._cache.seen == [2137]


def test_default_td_bars_unaffected(ds, monkeypatch):
    """默认 td_bars=300 仍走原值（零行为变化）。"""
    _ask(ds, 300, monkeypatch)

    assert ds._cache.seen == [300]


def test_over_hard_cap_clamped(ds, monkeypatch):
    """超过护栏上限才截断（防误传超大值）。"""
    _ask(ds, 999_999, monkeypatch)

    assert ds._cache.seen == [cex_data_source._MAX_HISTORY_BARS]


def test_hard_cap_covers_largest_fibonacci_n(ds, monkeypatch):
    """护栏必须覆盖斐波那契族最大 n=1597 所需的 5592 根。"""
    assert cex_data_source._MAX_HISTORY_BARS >= 5592
