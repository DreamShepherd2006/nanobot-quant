"""OKX CEX market data adapter.

Fetches candlestick data from OKX v5 REST API (`/api/v5/market/candles`)
and returns pandas DataFrames compatible with TD Sequential calculations.

Public endpoints — no API key required for market data.

Usage::

    from nanobot_quant.okx_cex_data import fetch_kline

    df = fetch_kline("XSPCX")               # 1D bars for tokenized SpaceX
    df = fetch_kline("BTC", bar="4H")       # 4-hour bars for BTC
    df = fetch_kline("XMETA", limit=250)    # 250 bars for Meta
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.okx.com"
_CANDLES_PATH = "/api/v5/market/candles"

# Rate control: OKX allows ~20 req / 2 s on public endpoints.
# We use a conservative 150 ms inter-request delay.
_RATE_DELAY = 0.150

# Mapping from our bar names to OKX bar values.
# OKX accepts: 1m, 3m, 5m, 15m, 30m, 1H, 2H, 4H, 6H, 12H, 1D, 1W, 1M, 3M
_BAR_MAP: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1H",
    "2H": "2H",
    "4H": "4H",
    "6H": "6H",
    "12H": "12H",
    "1D": "1D",
    "1W": "1W",
    "1M": "1M",
    "3M": "3M",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_inst_id(ticker: str) -> str:
    """Convert a ticker to OKX instrument ID format.

    Stock tickers: prefix with X if not already prefixed, suffix -USDT.
    Crypto tickers: suffix -USDT.

    >>> _to_inst_id("AAPL")
    'XAAPL-USDT'
    >>> _to_inst_id("XSPCX")
    'XSPCX-USDT'
    >>> _to_inst_id("BTC")
    'BTC-USDT'
    """
    ticker = ticker.upper().strip()
    if ticker.startswith("X"):
        # Already an X-prefixed tokenized stock
        return f"{ticker}-USDT"

    # If it looks like a stock ticker (1-5 letters, not a common crypto)
    # we prefix with X. Common crypto tickers are left as-is.
    _COMMON_CRYPTO = frozenset({
        "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX",
        "DOT", "LINK", "MATIC", "UNI", "SHIB", "LTC", "ETC",
        "ATOM", "FIL", "APT", "ARB", "OP", "SUI", "PEPE", "WIF",
        "BONK", "JUP", "TIA", "SEI", "STRK", "ZK", "NOT",
    })
    if ticker in _COMMON_CRYPTO or ticker.endswith("USDT"):
        return ticker if "-" in ticker else f"{ticker}-USDT"

    # Treat as stock: prefix X, suffix -USDT
    return f"X{ticker}-USDT"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_kline(
    ticker: str,
    bar: str = "1D",
    limit: int = 100,
    *,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Fetch OHLCV candlestick data from OKX CEX.

    Parameters
    ----------
    ticker:
        Ticker symbol.  For tokenized stocks use e.g. ``"XSPCX"`` or plain
        ``"AAPL"`` (auto-prefixed to ``"XAAPL-USDT"``).  For crypto use
        ``"BTC"``, ``"ETH"``, etc.
    bar:
        Bar size.  One of ``1m``, ``3m``, ``5m``, ``15m``, ``30m``,
        ``1H``, ``2H``, ``4H``, ``6H``, ``12H``, ``1D``, ``1W``, ``1M``,
        ``3M``.  Default ``"1D"``.
    limit:
        Number of bars to request (max ~300).  Default 100.
    session:
        Optional ``requests.Session`` for connection pooling.

    Returns
    -------
    pandas.DataFrame
        Columns: ``Open``, ``High``, ``Low``, ``Close``, ``Volume``.
        Index: ``DatetimeIndex`` (UTC).
        Returns an empty DataFrame when no data is available.

    Raises
    ------
    requests.RequestException
        On HTTP / network errors.
    ValueError
        On invalid parameters.
    """
    # ── validate ──
    bar_okx = _BAR_MAP.get(bar)
    if bar_okx is None:
        raise ValueError(
            f"Unsupported bar size '{bar}'. "
            f"Supported: {', '.join(sorted(_BAR_MAP))}"
        )
    limit = max(1, min(limit, 300))

    inst_id = _to_inst_id(ticker)

    # ── rate-limit ──
    time.sleep(_RATE_DELAY)

    # ── request ──
    _sess = session or requests
    url = f"{_BASE_URL}{_CANDLES_PATH}"
    params: dict = {
        "instId": inst_id,
        "bar": bar_okx,
        "limit": limit,
    }

    resp = _sess.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != "0":
        msg = payload.get("msg", "unknown error")
        raise requests.RequestException(
            f"OKX API error (code={payload.get('code')}): {msg}"
        )

    data = payload.get("data", [])
    if not data:
        logger.warning("fetch_kline(%s, %s): no data", inst_id, bar)
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    # ── parse ──
    # Each candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    rows: list[dict] = []
    for entry in data:
        try:
            ts = datetime.fromtimestamp(int(entry[0]) / 1000, tz=timezone.utc)
            rows.append({
                "ts": ts,
                "Open": float(entry[1]),
                "High": float(entry[2]),
                "Low": float(entry[3]),
                "Close": float(entry[4]),
                "Volume": float(entry[5]),
            })
        except (IndexError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed candle entry: %s — %s", entry, exc)
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    df.set_index("ts", inplace=True)
    df.sort_index(inplace=True)

    logger.info(
        "fetch_kline(%s, %s): %d bars, %s → %s",
        inst_id, bar, len(df),
        df.index[0].strftime("%Y-%m-%d") if len(df) else "N/A",
        df.index[-1].strftime("%Y-%m-%d") if len(df) else "N/A",
    )

    return df


def fetch_ticker(ticker: str, *, session: Optional[requests.Session] = None) -> dict:
    """Fetch latest ticker data (last price, 24h stats).

    Parameters
    ----------
    ticker:
        Ticker symbol (auto-prefixed for stocks).
    session:
        Optional ``requests.Session``.

    Returns
    -------
    dict
        Keys: ``last``, ``open24h``, ``high24h``, ``low24h``, ``vol24h``.
    """
    inst_id = _to_inst_id(ticker)
    time.sleep(_RATE_DELAY)

    _sess = session or requests
    resp = _sess.get(
        f"{_BASE_URL}/api/v5/market/ticker",
        params={"instId": inst_id},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != "0" or not payload.get("data"):
        return {}

    d = payload["data"][0]
    return {
        "last": float(d.get("last", 0)),
        "open24h": float(d.get("open24h", 0)),
        "high24h": float(d.get("high24h", 0)),
        "low24h": float(d.get("low24h", 0)),
        "vol24h": float(d.get("vol24h", 0)),
    }


_ORDER_BOOK_PATH = "/api/v5/market/books"
_ORDER_BOOK_LITE_PATH = "/api/v5/market/books-lite"


def fetch_order_book(
    ticker: str,
    *,
    depth: int = 5,
    session: Optional[requests.Session] = None,
) -> dict | None:
    """Fetch current order book depth from OKX.

    Uses the public books-lite endpoint (no auth).  Returns a dict with
    *best_bid* / *best_ask* / *spread_pct* / *bids* / *asks* or ``None``
    on failure.

    Parameters
    ----------
    ticker:
        Ticker symbol (e.g. ``"BTC"`` → ``"BTC-USDT"``).
    depth:
        Number of bid/ask levels to return (max 25 for books-lite).
    session:
        Optional ``requests.Session``.
    """
    inst_id = _to_inst_id(ticker)
    _sess = session or requests
    url = f"{_BASE_URL}{_ORDER_BOOK_LITE_PATH}"

    try:
        time.sleep(_RATE_DELAY)
        resp = _sess.get(
            url,
            params={"instId": inst_id, "sz": depth},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("fetch_order_book(%s): request failed — %s", inst_id, exc)
        return None

    if payload.get("code") != "0":
        logger.warning(
            "fetch_order_book(%s): API error %s — %s",
            inst_id, payload.get("code"), payload.get("msg", "?"),
        )
        return None

    data_list = payload.get("data", [])
    if not data_list:
        return None

    book = data_list[0]  # single book entry for the instId
    bids = []
    asks = []
    for entry in book.get("bids", []):
        try:
            bids.append((float(entry[0]), float(entry[1])))
        except (IndexError, ValueError, TypeError):
            continue
    for entry in book.get("asks", []):
        try:
            asks.append((float(entry[0]), float(entry[1])))
        except (IndexError, ValueError, TypeError):
            continue

    if not bids or not asks:
        logger.warning("fetch_order_book(%s): empty book", inst_id)
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_pct = ((best_ask - best_bid) / mid) * 100 if mid > 0 else 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": round(spread_pct, 4),
        "bid_depth": round(sum(q for _, q in bids), 2),
        "ask_depth": round(sum(q for _, q in asks), 2),
        "bids": bids[:depth],
        "asks": asks[:depth],
    }


def fetch_kline_range(
    ticker: str,
    start: str,
    end: str,
    bar: str = "1D",
    *,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Fetch OHLCV data for a date range via pagination.

    OKX returns at most 300 candles per request.  This function paginates
    backwards from ``end`` to ``start`` to build a continuous range.

    Parameters
    ----------
    ticker:
        Ticker symbol (auto-prefixed for stocks, e.g. ``"AAPL"`` → ``"XAAPL-USDT"``).
    start:
        Start date as ISO string (``"2024-01-01"``).
    end:
        End date as ISO string (``"2024-12-31"``).
    bar:
        Bar size.  Default ``"1D"``.
    session:
        Optional ``requests.Session``.

    Returns
    -------
    pandas.DataFrame
        Same format as :func:`fetch_kline`.  Sorted oldest→newest.
    """
    bar_okx = _BAR_MAP.get(bar)
    if bar_okx is None:
        raise ValueError(f"Unsupported bar size: {bar!r}")

    inst_id = _to_inst_id(ticker)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)

    _sess = session or requests
    url = f"{_BASE_URL}{_CANDLES_PATH}"

    frames: list[pd.DataFrame] = []
    before: int | None = None  # None on first call → latest candles

    while True:
        time.sleep(_RATE_DELAY)
        params: dict = {
            "instId": inst_id,
            "bar": bar_okx,
            "limit": 300,
        }
        if before is not None:
            params["before"] = before
        resp = _sess.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("code") != "0":
            msg = payload.get("msg", "unknown error")
            raise requests.RequestException(
                f"OKX API error (code={payload.get('code')}): {msg}"
            )

        batch = payload.get("data", [])
        if not batch:
            break

        rows: list[dict] = []
        earliest_ts: Optional[int] = None
        for entry in batch:
            try:
                ts_ms = int(entry[0])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts < start_dt or ts > end_dt:
                    continue
                rows.append({
                    "ts": ts,
                    "Open": float(entry[1]),
                    "High": float(entry[2]),
                    "Low": float(entry[3]),
                    "Close": float(entry[4]),
                    "Volume": float(entry[5]),
                })
                if earliest_ts is None or ts_ms < earliest_ts:
                    earliest_ts = ts_ms
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed candle: %s — %s", entry, exc)
                continue

        if rows:
            df = pd.DataFrame(rows).set_index("ts")
            frames.append(df)

        if batch and len(batch) < 300:
            # Fewer than max — no more data available
            break

        if earliest_ts is None or earliest_ts <= start_ms:
            break

        before = earliest_ts

    if not frames:
        logger.warning("fetch_kline_range(%s): no data %s→%s", inst_id, start, end)
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = pd.concat(frames)
    result.sort_index(inplace=True)
    result = result[~result.index.duplicated(keep="first")]

    # Clip to requested date range
    result = result[(result.index >= start_dt) & (result.index <= end_dt)]

    logger.info(
        "fetch_kline_range(%s, %s): %d bars, %s → %s",
        inst_id, bar, len(result),
        result.index[0].strftime("%Y-%m-%d") if len(result) else "N/A",
        result.index[-1].strftime("%Y-%m-%d") if len(result) else "N/A",
    )

    return result
