"""Lumibot DataSource backed by onchainos market API.

Implements the three abstract methods of ``lumibot.data_sources.DataSource``
using ``onchainos`` CLI subprocess calls for market data.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.data.kline_cache import KlineCache
from nanobot_quant.data_sources import get_data_source

logger = logging.getLogger("nanobot_quant.data.onchainos")


class OnchainOSDataSource(DataSource):
    """Lumibot DataSource that fetches OHLCV and prices from onchainos.

    Parameters:
        tokens_json: Optional user-configured token list from tokens.json.
            Each entry: ``{"symbol": "...", "address": "...", "chain": "solana"}``.
    """

    SOURCE = "onchainos"

    def __init__(self, tokens_json: list[dict] | None = None,
                 use_cache: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []
        self._use_cache = use_cache
        self._cache = KlineCache(self._fetch_kline) if use_cache else None

    def _fetch_kline(self, symbol: str, bar: str, limit: int) -> "pd.DataFrame":
        """Registry fetch + non-empty guard (shared by full/incremental paths)."""
        df = get_data_source("onchainos").fetch_kline(
            symbol, bar=bar, limit=min(limit, 299))
        if df is None or df.empty:
            raise RuntimeError(f"No kline data returned for {symbol}")
        return df

    # ── abstract methods ──────────────────────────────────────────

    def get_chains(self, asset, quote=None) -> dict:
        """Solana SPL tokens don't have option chains. Return empty."""
        return {}

    def get_historical_prices(
        self,
        asset,
        length: int,
        timestep: str = "",
        timeshift: Optional[timedelta] = None,
        exchange=None,
        include_after_hours: bool = True,
        quote=None,
        return_polars: bool = False,
    ):
        """Fetch OHLCV kline data for *asset* and return a ``Bars`` object.

        Signature must accept the full kwarg set lumibot v4.5.78 passes
        (exchange / return_polars / include_after_hours / quote); polars
        output is not supported — we always return pandas Bars.
        """
        symbol = asset.symbol

        # Per-target chain resolution happens inside the onchainos source
        # (resolve_token: tokens.json entry wins, default solana).
        # bar: 前缀 = live 直拉场景粒度（策略对 live broker 添加，lumibot
        # 无法解析 → 原样透传）；removeprefix 后直拉原生 bar（如 5m），
        # 绕开 lumibot multi-timeframe 转换（600 根 1m + resample）。
        resolution = self._map_timestep((timestep or "day").removeprefix("bar:"))
        limit = max(1, min(int(length), 299))
        try:
            if self._cache is not None:
                df = self._cache.get(symbol, resolution, limit)
            else:
                df = self._fetch_kline(symbol, resolution, limit)
        except Exception as exc:
            raise RuntimeError(f"No kline data returned for {symbol}: {exc}")
        if df is None or df.empty:
            raise RuntimeError(f"No kline data returned for {symbol}")

        from lumibot.entities import Bars
        return Bars(df, self.SOURCE, asset)

    def get_last_price(
        self, asset, quote=None, exchange=None
    ) -> Optional[float]:
        """Get real-time price for *asset* from onchainos (official path)."""
        p = get_data_source("onchainos").get_price(asset.symbol)
        return p if p else None

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_timestep(timestep: str) -> str:
        """Map Lumibot timestep to onchainos bar format.

        OKX DEX `market kline` accepts 1m/5m/15m/1H/4H/1D/1W only
        ("1Min" triggers 51000 Parameter bar error).
        """
        return {
            "minute": "1m",
            "5min": "5m",
            "15min": "15m",
            "hour": "1H",
            "4hour": "4H",
            "day": "1D",
            "week": "1W",
        }.get(timestep.lower(), "1D")
