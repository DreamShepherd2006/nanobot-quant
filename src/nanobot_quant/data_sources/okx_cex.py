"""OKX CEX data source — retained for future business-quant integration.

现状（2026-08-15 拍板）：OKX CEX 后续会接入执行，但目前业务量化部分
尚未完成，故 kind=research（仅回测/展示，不参与执行）。接入执行时：
注册表条目改 kind=executable + CHANNEL_DATA_SOURCE 加一行 + broker。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from nanobot_quant.gate_credentials import load_tokens_json, okx_ticker
from nanobot_quant.okx_cex_data import (
    fetch_kline as _fetch_kline_okx,
    fetch_kline_range as _fetch_kline_range_okx,
    fetch_order_book,
    fetch_ticker,
)


def fetch_kline(symbol, bar="1D", limit=120,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """OKX CEX candles for ``symbol`` (instId via ``okx_ticker``)."""
    inst = okx_ticker(symbol, load_tokens_json())
    if start and end:
        # okx_cex fetch_kline_range takes ISO date strings
        return _fetch_kline_range_okx(inst,
                                      start=start.strftime("%Y-%m-%d"),
                                      end=end.strftime("%Y-%m-%d"),
                                      bar=bar)
    return _fetch_kline_okx(inst, bar=bar, limit=limit)


def get_price(symbol) -> Optional[float]:
    """Latest OKX CEX last price (``ticker.last``)."""
    t = fetch_ticker(okx_ticker(symbol, load_tokens_json()))
    if not t:
        return None
    try:
        return float(t.get("last") or 0.0)
    except (TypeError, ValueError):
        return None


def order_book(symbol, depth: int = 5) -> Optional[dict]:
    """OKX CEX order-book depth (public endpoint)."""
    return fetch_order_book(okx_ticker(symbol, load_tokens_json()), depth=depth)


def ticker(symbol) -> Optional[dict]:
    """OKX CEX ticker snapshot (native fields, no mapping needed)."""
    t = fetch_ticker(okx_ticker(symbol, load_tokens_json()))
    if not t:
        return None
    return {k: t.get(k) for k in ("last", "bid", "ask", "high24h", "low24h", "vol24h")}
