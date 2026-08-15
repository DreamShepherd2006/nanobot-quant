"""Gate.io spot candlesticks — data side of the CEX execution channel.

Execution (CexBroker) and signal data both come from Gate (same-exchange),
so tokenized assets that exist only on Gate (e.g. CRCLX_USDT) work
end-to-end. Public REST: GET /api/v4/spot/candlesticks (no API key).

Gate row format (array, ascending by ts):
    [ts(sec), quote_volume, close, high, low, open, base_volume, closed]

The trailing ``closed`` flag marks the in-progress bar (false) — it is
dropped so signals are always computed on closed bars (same rule as the
on-chain DEX live path, docs/quant-system.md 方案 C).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

_API = "https://api.gateio.ws/api/v4/spot/candlesticks"

# td-table / lumibot bar names -> Gate interval
_BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1h", "4H": "4h", "1D": "1d", "1W": "7d",
}

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _map_bar(bar) -> str:
    return _BAR_MAP.get(str(bar), "1d")


def _request(pair: str, interval: str, limit: int,
             from_ts: Optional[int] = None, to_ts: Optional[int] = None) -> list:
    params = {
        "currency_pair": pair,
        "interval": interval,
        "limit": max(1, min(int(limit), 1000)),
    }
    if from_ts:
        params["from"] = int(from_ts)
    if to_ts:
        params["to"] = int(to_ts)
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "[]")


def rows_to_df(rows: list) -> pd.DataFrame:
    """Gate candlestick rows -> OnchainOS-shaped DataFrame (UTC index).

    Drops in-progress bars (``closed == false``) and malformed rows.
    Column order: Open/High/Low/Close/Volume.
    """
    recs = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 8:
            continue
        if str(r[7]).lower() == "false":
            continue  # in-progress bar — not closed
        try:
            recs.append({
                "Open": float(r[5]),
                "High": float(r[3]),
                "Low": float(r[4]),
                "Close": float(r[2]),
                "Volume": float(r[6]),
                "_ts": datetime.fromtimestamp(int(r[0]), tz=timezone.utc),
            })
        except (ValueError, TypeError):
            continue
    if not recs:
        return pd.DataFrame(columns=_COLUMNS)
    df = pd.DataFrame(recs).set_index("_ts")
    df.index.name = None
    return df[_COLUMNS]


def fetch_gate_kline(pair: str, bar: str = "1D", limit: int = 120) -> pd.DataFrame:
    """Latest ``limit`` closed candles for a Gate spot pair (e.g. CRCLX_USDT)."""
    rows = _request(pair, _map_bar(bar), limit)
    return rows_to_df(rows)


def fetch_gate_kline_range(pair: str, start_ts: int, end_ts: int,
                           bar: str = "1D") -> pd.DataFrame:
    """Closed candles in [start_ts, end_ts] (unix seconds) for a Gate pair."""
    rows = _request(pair, _map_bar(bar), 1000, from_ts=start_ts, to_ts=end_ts)
    return rows_to_df(rows)


def fetch_gate_ticker(pair: str) -> Optional[dict]:
    """GET /spot/tickers?currency_pair= -> latest ticker dict (public, no key).

    Falls back to the list endpoint shape used by the broker: returns the
    first entry, whose ``last`` field is the last traded price.
    """
    url = ("https://api.gateio.ws/api/v4/spot/tickers?currency_pair="
           + urllib.parse.quote(pair))
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError:
        return None
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


def fetch_gate_order_book(pair: str, depth: int = 5) -> Optional[dict]:
    """GET /spot/order_book?currency_pair=&limit= -> depth summary (public).

    Returns ``{"best_bid", "best_ask", "spread_pct", "bids", "asks"}``
    (same shape as the OKX CEX order book used for VT grounding) or
    ``None`` on failure.  ``bids``/``asks`` are ``[price, amount]`` lists.
    """
    url = ("https://api.gateio.ws/api/v4/spot/order_book?"
           + urllib.parse.urlencode({
               "currency_pair": pair,
               "limit": max(1, min(int(depth), 100)),
           }))
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
    except (urllib.error.HTTPError, OSError, ValueError):
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    try:
        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None
    except (TypeError, ValueError, IndexError):
        best_bid = best_ask = None
    spread_pct = None
    if best_bid and best_ask:
        spread_pct = (best_ask - best_bid) / best_bid * 100.0
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread_pct,
        "bids": [[float(x), float(y)] for x, y in bids],
        "asks": [[float(x), float(y)] for x, y in asks],
    }
