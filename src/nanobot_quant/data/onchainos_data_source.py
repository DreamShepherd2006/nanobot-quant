"""Lumibot DataSource backed by onchainos market API.

Implements the three abstract methods of ``lumibot.data_sources.DataSource``
using ``onchainos`` CLI subprocess calls for market data.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.onchainos_cli import (
    resolve_token_address,
    get_kline,
    get_token_price,
)
from nanobot_quant.onchainos_data import parse_kline_response

logger = logging.getLogger("nanobot_quant.data.onchainos")


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
        addr = resolve_token_address(symbol, self._tokens_json)
        if not addr:
            raise ValueError(f"Cannot resolve token address for '{symbol}'")

        resolution = self._map_timestep(timestep or "day")
        candles = get_kline(addr, bar=resolution, limit=min(length, 299))

        if not candles:
            raise RuntimeError(f"No kline data returned for {symbol} ({addr})")

        # Shared parser — single source of truth for the official CLI
        # contract (string OHLCV + epoch-millisecond ts). Backtest and
        # live loops therefore see byte-identical DataFrames.
        df = parse_kline_response(candles)
        if df.empty:
            raise RuntimeError(f"No kline data returned for {symbol} ({addr})")

        from lumibot.entities import Bars
        return Bars(df, self.SOURCE, asset)

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
