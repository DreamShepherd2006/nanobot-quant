"""Lumibot DataSource backed by onchainos market API.

Implements the three abstract methods of ``lumibot.data_sources.DataSource``
using ``onchainos`` CLI subprocess calls for market data.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import pandas as pd

from nanobot_quant.onchainos_swap import (
    resolve_token_address,
    get_kline,
    get_token_price,
)

logger = logging.getLogger("nanobot_quant.data.onchainos")

try:
    from lumibot.data_sources import DataSource
except ImportError:  # pragma: no cover
    class DataSource:  # type: ignore[no-redef]
        """Fallback when lumibot is not installed (local dev / CI)."""
        pass


class OnchainOSDataSource(DataSource):
    """Lumibot DataSource that fetches OHLCV and prices from onchainos.

    Parameters:
        tokens_json: Optional user-configured token list from tokens.json.
            Each entry: ``{"symbol": "...", "address": "...", "chain": "solana"}``.
    """

    SOURCE = "onchainos"

    def __init__(self, tokens_json: list[dict] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []

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
        quote=None,
        include_after_hours: bool = True,
    ):
        """Fetch OHLCV kline data for *asset* and return a ``Bars`` object."""
        symbol = asset.symbol
        addr = resolve_token_address(symbol, self._tokens_json)
        if not addr:
            raise ValueError(f"Cannot resolve token address for '{symbol}'")

        resolution = self._map_timestep(timestep or "day")
        candles = get_kline(addr, bar=resolution, limit=min(length, 299))

        if not candles:
            raise RuntimeError(f"No kline data returned for {symbol} ({addr})")

        df = pd.DataFrame(candles)
        df.rename(
            columns={
                "ts": "timestamp", "o": "open", "h": "high",
                "l": "low", "c": "close", "vol": "volume",
            },
            inplace=True,
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)

        from lumibot.entities import Bars
        return Bars(df, self.SOURCE)

    def get_last_price(
        self, asset, quote=None, exchange=None
    ) -> Optional[float]:
        """Get real-time price for *asset* from onchainos."""
        symbol = asset.symbol
        addr = resolve_token_address(symbol, self._tokens_json)
        if not addr:
            return None
        return get_token_price(addr)

    # ── helpers ───────────────────────────────────────────────────

    @staticmethod
    def _map_timestep(timestep: str) -> str:
        """Map Lumibot timestep to onchainos bar format."""
        return {
            "minute": "1Min",
            "5min": "5Min",
            "15min": "15Min",
            "hour": "1H",
            "4hour": "4H",
            "day": "1D",
            "week": "1W",
        }.get(timestep.lower(), "1D")
