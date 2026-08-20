"""Lumibot DataSource backed by Gate spot candles (data side of the CEX
Execution happens on Gate (CexBroker) and signal data also comes from Gate
(gate_cex_data.py) — same-exchange, so tokenized assets that only exist on
Gate (e.g. CRCLX_USDT) work end-to-end (docs/quant-system.md §18).
"""

from __future__ import annotations

import logging
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.data.kline_cache import KlineCache
from nanobot_quant.data_sources import get_data_source

logger = logging.getLogger("nanobot_quant.data.cex")

_BAR_MAP = {
    "minute": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "hour": "1H",
    "4hour": "4H",
    "day": "1D",
    "week": "1W",
}

_DEFAULT_BAR = "1D"


class CexDataSource(DataSource):
    """Lumibot DataSource wrapper over Gate spot candles (fetch_gate_kline)."""

    SOURCE = "gate_cex"

    # 数据源契约：gate_cex_data.rows_to_df 已丢弃进行中 bar（closed=false，
    # 仅返回已收盘 K 线）。策略层据此不再多拉 1 根/再丢弃，避免双重丢弃
    # 导致 bars=119 < min_history 永久 SKIP（2026-08-17 A 修复第二部分）。
    drops_in_progress_bars = True

    def __init__(self, tokens_json: Optional[list[dict]] = None,
                 use_cache: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []
        self._use_cache = use_cache
        self._cache = KlineCache(self._fetch_kline) if use_cache else None

    def _fetch_kline(self, symbol: str, bar: str, limit: int) -> "pd.DataFrame":
        """Registry fetch + non-empty guard (shared by full/incremental paths)."""
        df = get_data_source("gate_cex").fetch_kline(symbol, bar=bar, limit=limit)
        if df is None or df.empty:
            raise RuntimeError(f"No Gate CEX kline for {symbol} (bar={bar})")
        return df

    def get_chains(self, asset=None, quote=None):
        return {}

    def get_historical_prices(
        self,
        asset,
        length,
        timestep: str = "",
        timeshift=None,
        exchange=None,
        include_after_hours: bool = True,
        quote=None,
        return_polars: bool = False,
    ):
        """Return lumibot Bars for the asset from Gate spot candles.

        K 线统一经数据源注册表（gate_cex = CEX 执行通道同所）获取；S2 增量
        K 线缓存（docs/quant-system.md §22.6）：首轮全量预取，后续每轮只拉
        最近 2 根增量合并，缺口自动全量重拉。
        """
        symbol = asset.symbol
        bar = _BAR_MAP.get(str(timestep or "").lower(), _DEFAULT_BAR)
        limit = max(1, min(int(length), 1000))
        if self._cache is not None:
            df = self._cache.get(symbol, bar, limit)
        else:
            df = self._fetch_kline(symbol, bar, limit)
        # lumibot v4.5.78 Bars.__init__ 构造时访问小写列 df["close"] 派生 return 列；
        # 而 gate_cex 数据源（rows_to_df）输出大写列 Open/High/Low/Close/Volume（td-table
        # 页面契约）——列名不一致会抛 KeyError: 'close'（2026-08-17 A 修复）。此处统一
        # 小写化对齐 lumibot 契约；策略层 col_map 再映射回大写供 calculate() 使用。
        df = df.rename(columns=str.lower)
        from lumibot.entities import Bars
        return Bars(df, self.SOURCE, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        """Last price for the asset from Gate spot ticker (public)."""
        p = get_data_source("gate_cex").get_price(asset.symbol)
        return p if p else None

    def get_timestamp(self):
        import time
        return time.time()
