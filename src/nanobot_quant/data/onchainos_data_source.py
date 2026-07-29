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


class OnchainOSDataSource:
    """Lumibot DataSource that fetches OHLCV and prices from onchainos.

    This is NOT a subclass of ``lumibot.data_sources.DataSource`` at module
    level (to avoid ImportError when lumibot is not installed).  The subclass
    relationship is patched at runtime when the strategy engine imports it.

    Parameters:
        tokens_json: Optional user-configured token list from tokens.json.
            Each entry: ``{"symbol": "...", "address": "...", "chain": "solana"}``.
    """

    SOURCE = "onchainos"

    def __init__(self, tokens_json: list[dict] | None = None, **kwargs):
        super().__init__(**kwargs)  # type: ignore[misc]
        self._tokens_json = tokens_json or []

    # ── abstract methods ──────────────────────────────────────────

    def get_chains(self, asset, quote=None) -> dict:  # type: ignore[override]
        """Solana SPL tokens don't have option chains. Return empty."""
        return {}

    def get_historical_prices(  # type: ignore[override]
        self,
        asset,
        length: int,
        timestep: str = "",
        timeshift: Optional[timedelta] = None,
        quote=None,
        include_after_hours: bool = True,
    ):
        """Fetch OHLCV kline data for *asset* and return a ``Bars`` object.

        Calls ``onchainos market kline``, converts the candle list to a
        DataFrame, wraps it in ``Bars``.
        """
        symbol = asset.symbol
        addr = resolve_token_address(symbol, self._tokens_json)
        if not addr:
            raise ValueError(f"Cannot resolve token address for '{symbol}'")

        resolution = self._map_timestep(timestep or "day")
        candles = get_kline(addr, bar=resolution, limit=min(length, 299))

        if not candles:
            raise RuntimeError(f"No kline data returned for {symbol} ({addr})")

        # Convert to DataFrame
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

        # Lumibot Bars wrapper
        from lumibot.entities import Bars
        return Bars(df, self.SOURCE)

    def get_last_price(  # type: ignore[override]
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


# ── Runtime patch ─────────────────────────────────────────────────

def _patch_for_lumibot():
    """Monkey-patch OnchainOSDataSource to inherit from lumibot DataSource.

    We avoid a hard import of lumibot at module level so the package remains
    importable even when lumibot is not installed (e.g. local dev or CI).
    """
    try:
        from lumibot.data_sources import DataSource

        # Only patch if not already a subclass
        if DataSource not in OnchainOSDataSource.__mro__:  # type: ignore[attr-defined]
            OnchainOSDataSource.__bases__ = (DataSource,)
            logger.debug("OnchainOSDataSource patched to inherit from lumibot DataSource")
    except ImportError:
        logger.debug("lumibot not installed — DataSource patch skipped")
