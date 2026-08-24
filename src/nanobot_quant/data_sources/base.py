"""Data source registry — unified access to every market data feed.

设计（docs/quant-system.md §6.1，2026-08-15 定稿）：

- 所有数据源实现统一契约 ``DataSourceSpec``：``fetch_kline`` /
  ``get_price`` / 可选 ``order_book``（CEX 类源，VT grounding 用）。
- 通道→数据源经 broker spec 的 ``data_source`` 字段单一事实源（方案 C，
  2026-08-17）：``data_source_for_channel`` = ``spec_for_channel`` 的
  data_source 解析，同源约束从「约定」变「结构」，且杜绝通道→broker 与
  通道→数据源两表漂移。TD live、取价、分析页默认源、grounding 全部从
  这条路径取。加新交易所 = 注册 BrokerSpec（含 data_source 字段）+
  EXECUTION_CHANNELS/enum_groups 加一项，上层零改动。
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
        interval_map: Optional[dict] = None,
    ) -> None:
        self.name = name
        self.display = display
        self.kind = kind          # "executable" | "research"
        self.exchange = exchange  # "gate" | "okx" | None
        self.bars = tuple(bars)   # supported unified period names (empty = any)
        # 统一周期名 → 该所 API interval 字符串（缺省 = 同名映射）
        self.interval_map = dict(interval_map) if interval_map else {
            p: p for p in self.bars
        }
        self._fetch_kline = fetch_kline
        self._get_price = get_price
        self._order_book = order_book
        self._ticker = ticker

    # ── 周期映射 ──────────────────────────────────────────────────────
    def interval_for(self, bar: str) -> str:
        """统一周期名 → 该所 API interval 字符串。

        Fail-closed：不在 ``bars`` 内的周期直接 KeyError（上层在 UI 下拉
        就用 spec.bars 过滤，传错即 bug，不静默回退到默认粒度）。
        """
        if bar not in self.interval_map:
            raise KeyError(
                f"{self.name}: 不支持的周期 {bar!r}（支持: {list(self.bars)}）"
            )
        return self.interval_map[bar]

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


# execution_channel（实例名）→ data source：从绑定 broker spec 的 data_source 字段取
# （单一事实源，杜绝通道→broker 与通道→数据源两表漂移；方案 C，2026-08-17）。
# 未来新增执行所：注册 BrokerSpec（含 data_source 字段）即可，此处零改动。


def data_source_for_channel(channel: str) -> DataSourceSpec:
    """Resolve the data source bound to an execution channel (via broker spec).

    Same-exchange data is guaranteed structurally: the broker spec carries
    its own data_source name. Legacy ``dex``/``cex`` values are normalized.
    Raises KeyError for unknown channels — fail-closed, never falls back
    to a different exchange's data.
    """
    from nanobot_quant.brokers.registry import spec_for_channel
    return get_data_source(spec_for_channel(channel).data_source)
