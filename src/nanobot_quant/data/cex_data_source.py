"""Lumibot DataSource backed by Gate spot candles (data side of the CEX
Execution happens on Gate (CexBroker) and signal data also comes from Gate
(gate_cex_data.py) — same-exchange, so tokenized assets that only exist on
Gate (e.g. CRCLX_USDT) work end-to-end (docs/quant-system.md §18).
"""

from __future__ import annotations

import logging
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.gate_cex_data import fetch_gate_kline, fetch_gate_ticker
from nanobot_quant.gate_credentials import gate_pair

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
        """Return lumibot Bars for the asset from Gate spot candles."""
        symbol = asset.symbol
        pair = gate_pair(symbol, self._tokens_json)
        bar = _BAR_MAP.get(str(timestep or "").lower(), _DEFAULT_BAR)
        limit = max(1, min(int(length), 1000))
        df = fetch_gate_kline(pair, bar=bar, limit=limit)
        if df is None or df.empty:
            raise RuntimeError(f"No Gate CEX kline for {symbol} (pair={pair}, bar={bar})")
        from lumibot.entities import Bars
        return Bars(df, self.SOURCE, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        """Last price for the asset from Gate spot ticker (public)."""
        symbol = asset.symbol
        pair = gate_pair(symbol, self._tokens_json)
        t = fetch_gate_ticker(pair)
        if not t:
            return None
        last = t.get("last")
        return float(last) if last else None

    def get_timestamp(self):
        import time
        return time.time()
