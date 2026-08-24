"""Gate CEX data source — the CEX execution channel's same-exchange feed.

薄封装 gate_cex_data.py（K线/ticker/order_book 数据访问层），symbol 经
gate_pair() 映射（tokens.json gate_symbol 优先，自动 BASE_QUOTE）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from nanobot_quant.gate_cex_data import (
    fetch_gate_kline,
    fetch_gate_kline_range_paged,
    fetch_gate_order_book,
    fetch_gate_ticker,
)
from nanobot_quant.gate_credentials import gate_pair, load_tokens_json


def fetch_kline(symbol, bar="1D", limit=120,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """Gate CEX candles for ``symbol`` (pair via ``gate_pair``)."""
    pair = gate_pair(symbol, load_tokens_json())
    if start and end:
        # 分页向后翻，遇历史深度上限（如 1m ≈ 最近 10000 根 ≈ 6.9 天）
        # 截断保留已拉批次、不报 400——td-table 分析页选超深区间时
        # 返回实际可用数据而非 HTTP Error 400（2026-08-25 修复）。
        return fetch_gate_kline_range_paged(pair, int(start.timestamp()),
                                            int(end.timestamp()), bar=bar)
    return fetch_gate_kline(pair, bar=bar, limit=limit)


def get_price(symbol) -> Optional[float]:
    """Last traded price on Gate (same-exchange ticker, ``[0].last``)."""
    t = fetch_gate_ticker(gate_pair(symbol, load_tokens_json()))
    if not t:
        return None
    try:
        return float(t.get("last") or 0.0)
    except (TypeError, ValueError):
        return None


def order_book(symbol, depth: int = 5) -> Optional[dict]:
    """Gate spot order book (public endpoint, no key)."""
    return fetch_gate_order_book(gate_pair(symbol, load_tokens_json()),
                                 depth=depth)


def ticker(symbol) -> Optional[dict]:
    """Gate ticker snapshot mapped to the enrichment contract.

    Gate /spot/tickers fields: ``last``/``high_24h``/``low_24h``/
    ``quote_volume``/``lowest_ask``/``highest_bid`` → normalised
    ``last``/``high24h``/``low24h``/``vol24h``/``ask``/``bid``.
    """
    t = fetch_gate_ticker(gate_pair(symbol, load_tokens_json()))
    if not t:
        return None
    return {
        "last": _num(t.get("last")),
        "bid": _num(t.get("highest_bid")),
        "ask": _num(t.get("lowest_ask")),
        "high24h": _num(t.get("high_24h")),
        "low24h": _num(t.get("low_24h")),
        "vol24h": _num(t.get("quote_volume")),
    }


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
