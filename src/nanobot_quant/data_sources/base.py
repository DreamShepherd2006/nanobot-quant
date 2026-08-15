"""Data source registry — unified access to every market data feed.

设计（docs/quant-system.md §6.1，2026-08-15 定稿）：

- 所有数据源实现统一契约 ``DataSourceSpec``：``fetch_kline`` /
  ``get_price`` / 可选 ``order_book``（CEX 类源，VT grounding 用）。
- ``CHANNEL_DATA_SOURCE`` 把 execution_channel（dex/cex）结构性绑定到
  数据源——TD live、取价、分析页默认源、grounding 全部从这条映射取，
  同源约束从「约定」变「结构」。加新交易所 = 注册表加条目 + 通道映射
  加一行 + broker，上层（TD 信号、执行链）零改动。
- ``kind`` 区分可执行源（executable，关联执行通道）与纯研究源
  （research，仅展示/回测，如东财/yfinance/okx_cex 现阶段）。
"""

from __future__ import annotations

from typing import Callable, Optional

import pandas as pd


class DataSourceSpec:
    """A market data feed (kline / price / optional order book).

    All sources return the same DataFrame shape: lowercase ohlcv columns
    (``open``/``high``/``low``/``close``/``volume``) with a DatetimeIndex
    (naive or tz-aware — consumers normalise, mirroring OnchainOS).
    """

    def __init__(
        self,
        name: str,
        display: str,
        kind: str = "research",
        exchange: Optional[str] = None,
        fetch_kline: Optional[Callable] = None,
        get_price: Optional[Callable] = None,
        order_book: Optional[Callable] = None,
        ticker: Optional[Callable] = None,
        bars: tuple = (),
    ) -> None:
        self.name = name
        self.display = display
        self.kind = kind          # "executable" | "research"
        self.exchange = exchange  # "gate" | "okx" | None
        self.bars = tuple(bars)   # supported bar sizes (empty = any)
        self._fetch_kline = fetch_kline
        self._get_price = get_price
        self._order_book = order_book
        self._ticker = ticker

    # ── 统一契约 ──────────────────────────────────────────────────────
    def fetch_kline(self, symbol, bar="1D", limit=120,
                    start=None, end=None) -> pd.DataFrame:
        """OHLCV candles for ``symbol`` (source-specific resolution inside).

        ``start``/``end`` are optional ``datetime`` bounds; when both are
        given a range fetch is performed, otherwise the latest ``limit``
        closed candles are returned.  Returns an OnchainOS-shaped DataFrame
        (lowercase ohlcv columns).
        """
        if self._fetch_kline is None:
            raise NotImplementedError(f"{self.name}: fetch_kline 未实现")
        return self._fetch_kline(symbol, bar=bar, limit=limit,
                                 start=start, end=end)

    def get_price(self, symbol) -> float:
        """Latest tradable price in USD (stablecoins fixed at 1.0).

        Fail-closed: returns 0.0 on failure (consumers must not trade on 0).
        """
        if self._get_price is None:
            raise NotImplementedError(f"{self.name}: get_price 未实现")
        try:
            return float(self._get_price(symbol) or 0.0)
        except (TypeError, ValueError, RuntimeError):
            return 0.0

    def order_book(self, symbol, depth: int = 5) -> Optional[dict]:
        """Order-book depth summary (CEX sources only).

        Returns ``{"best_bid", "best_ask", "spread_pct", "bids", "asks"}``
        or ``None`` when unavailable.
        """
        if self._order_book is None:
            raise NotImplementedError(f"{self.name}: order_book 未实现")
        return self._order_book(symbol, depth=depth)

    def ticker(self, symbol) -> Optional[dict]:
        """Full ticker snapshot (CEX sources only, for enrichment).

        Returns a dict with ``last``/``bid``/``ask``/``high24h``/
        ``low24h``/``vol24h`` keys or ``None`` when unavailable.
        """
        if self._ticker is None:
            raise NotImplementedError(f"{self.name}: ticker 未实现")
        return self._ticker(symbol)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataSourceSpec {self.name} [{self.kind}]>"


REGISTRY: dict[str, DataSourceSpec] = {}


def register(spec: DataSourceSpec) -> DataSourceSpec:
    """Register a data source (idempotent by name)."""
    REGISTRY[spec.name] = spec
    return spec


def get_data_source(name: str) -> DataSourceSpec:
    if name not in REGISTRY:
        raise KeyError("未知数据源 %r（可选：%s）"
                       % (name, ", ".join(REGISTRY)))
    return REGISTRY[name]


def list_data_sources() -> list[str]:
    return list(REGISTRY)


def executable_sources() -> list[str]:
    return [n for n, s in REGISTRY.items() if s.kind == "executable"]


def research_sources() -> list[str]:
    return [n for n, s in REGISTRY.items() if s.kind == "research"]


# execution_channel → data source（结构性同源）。
# 未来 OKX CEX 业务量化接入：加一行 "okx_cex": "okx_cex" + broker 即可。
CHANNEL_DATA_SOURCE = {
    "dex": "onchainos",
    "cex": "gate_cex",
}


def data_source_for_channel(channel: str) -> DataSourceSpec:
    """Resolve the data source bound to an execution channel.

    Raises KeyError for unknown channels — fail-closed, never falls back
    to a different exchange's data.
    """
    if channel not in CHANNEL_DATA_SOURCE:
        raise KeyError("执行通道 %r 未绑定数据源（可选：%s）"
                       % (channel, ", ".join(CHANNEL_DATA_SOURCE)))
    return get_data_source(CHANNEL_DATA_SOURCE[channel])
