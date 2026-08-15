"""OnchainOS (OKX DEX) data source — the DEX execution channel's feed.

薄封装 onchainos_data.py（K线，CLI subprocess）+ onchainos_cli.get_price
（官方 market price → market index 取价路径）。symbol 经 resolve_token()
统一解析（L0-L4，tokens.json 登记优先）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from nanobot_quant.gate_credentials import load_tokens_json
from nanobot_quant.onchainos_cli import get_price as _cli_price, resolve_token
from nanobot_quant.onchainos_data import fetch_kline as _fetch_kline_chain
from nanobot_quant.onchainos_data import fetch_kline_range as _fetch_kline_range_chain


def _resolve(symbol, tokens_json) -> dict:
    r = resolve_token(symbol, tokens_json=tokens_json)
    if not r.get("ok"):
        raise RuntimeError(r.get("issue") or f"无法解析 {symbol}")
    return r


def fetch_kline(symbol, bar="1D", limit=120,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """OnchainOS candles for ``symbol`` (resolved chain + contract address)."""
    tokens = load_tokens_json()
    r = _resolve(symbol, tokens)
    if start and end:
        return _fetch_kline_range_chain(r["chain"], r["address"],
                                        start=start, end=end, bar=bar)
    return _fetch_kline_chain(r["chain"], r["address"], bar=bar, limit=limit)


def get_price(symbol) -> Optional[float]:
    """Official onchainos pricing path (market price → aggregated index)."""
    tokens = load_tokens_json()
    r = _resolve(symbol, tokens)
    p = _cli_price(symbol, chain=r.get("chain") or "solana", tokens_json=tokens)
    if p is None:
        return None
    try:
        return float(p)
    except (TypeError, ValueError):
        return None
