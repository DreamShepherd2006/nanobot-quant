"""Lumibot DataSource backed by OKX CEX market data (data side of the CEX
execution channel).

Execution happens on Gate (CexBroker) while signal data comes from OKX CEX
(okx_cex_data.py) — data/execution separation (docs/quant-system.md §18).
The same tokenized asset may use different tickers per exchange; the mapping
lives in tokens.json (``gate_symbol`` / ``okx_symbol``).
"""

from __future__ import annotations

import logging
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.gate_credentials import okx_ticker
from nanobot_quant.okx_cex_data import fetch_kline, fetch_ticker

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
    """Lumibot DataSource wrapper over OKX CEX candles (fetch_kline)."""

    SOURCE = "okx_cex"

    def __init__(self, tokens_json: Optional[list[dict]] = None, **kwargs):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []

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
        """Return lumibot Bars for the asset from OKX CEX candles."""
        symbol = asset.symbol
        ticker = okx_ticker(symbol, self._tokens_json)
        bar = _BAR_MAP.get(str(timestep or "").lower(), _DEFAULT_BAR)
        limit = max(1, min(int(length), 300))
        df = fetch_kline(ticker, bar=bar, limit=limit)
        if df is None or df.empty:
            raise RuntimeError(f"No OKX CEX kline for {symbol} (ticker={ticker}, bar={bar})")
        from lumibot.entities import Bars
        return Bars(df, self.SOURCE, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        """Last price for the asset from OKX CEX ticker."""
        symbol = asset.symbol
        ticker = okx_ticker(symbol, self._tokens_json)
        t = fetch_ticker(ticker)
        if not t:
            return None
        last = t.get("last")
        return float(last) if last else None

    def get_timestamp(self):
        import time
        return time.time()
