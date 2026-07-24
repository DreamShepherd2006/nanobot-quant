"""OnchainOS market data adapter — replace yfinance for crypto backtesting.

Kline data is fetched via OKX's DEX market API with HMAC-SHA256 auth.
Returns pandas DataFrames compatible with the existing TD Sequential /
backtest pipeline.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import pandas as pd

from .okx_credentials import get_okx_api_key, get_okx_passphrase, get_okx_secret_key

logger = logging.getLogger(__name__)

BASE_URL = "https://web3.okx.com"
KLINE_PATH = "/api/v6/dex/market/candles"
MAX_LIMIT = 300  # max candles per request

# ── Common chain IDs ─────────────────────────────────────────────
CHAIN_IDS: dict[str, str] = {
    "ethereum": "1",
    "arbitrum": "42161",
    "base": "8453",
    "bsc": "56",
    "optimism": "10",
    "polygon": "137",
    "solana": "501",
    "avalanche": "43114",
}

# ── Common token addresses (WETH, USDC, WBTC, etc.) ─────────────
TOKENS: dict[str, dict[str, str]] = {
    "WETH": {
        "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "base": "0x4200000000000000000000000000000000000006",
        "bsc": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
        "optimism": "0x4200000000000000000000000000000000000006",
        "polygon": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
    },
    "USDC": {
        "arbitrum": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "ethereum": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "bsc": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "optimism": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "polygon": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    },
    "WBTC": {
        "arbitrum": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "ethereum": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "base": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",
    },
}


def _build_signature(
    method: str,
    path: str,
    body: str = "",
    *,
    secret_key: str,
) -> tuple[str, str]:
    """Return (signature_base64, iso_timestamp) for OK-ACCESS-SIGN."""
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    prehash = f"{ts}{method}{path}{body}"
    mac = hmac.new(secret_key.encode(), prehash.encode(), hashlib.sha256)
    sign = base64.b64encode(mac.digest()).decode()
    return sign, ts


def _headers(method: str, path: str, body: str = "") -> dict[str, str]:
    """Build OKX API authentication headers."""
    api_key = get_okx_api_key()
    secret = get_okx_secret_key()
    passphrase = get_okx_passphrase()

    if not (api_key and secret and passphrase):
        raise ValueError(
            "OKX credentials not configured. "
            "Place okx_credentials.json at /data/okx_credentials.json "
            "with keys: api_key, secret_key, passphrase."
        )

    sign, ts = _build_signature(method, path, body, secret_key=secret)
    return {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "Ok-Access-Client-type": "agent-cli",
        "platform": "agent-cli",
        "ok-client-type": "cli",
    }


def fetch_kline(
    chain: str,
    token_address: str,
    bar: str = "1D",
    limit: int = 100,
    *,
    base_url: str = BASE_URL,
    client: Optional[httpx.Client] = None,
) -> pd.DataFrame:
    """Fetch kline/candlestick data from OnchainOS.

    Args:
        chain: Chain index as string (e.g. "42161") or name (e.g. "arbitrum").
        token_address: Token contract address (0x…).
        bar: Candle interval — "1m", "5m", "15m", "1H", "4H", "1D", "1W".
        limit: Number of candles (max 300 per call).

    Returns:
        DataFrame with columns: open, high, low, close, volume, timestamp.
        Index is datetime64[ns] in UTC.
    """
    chain_id = CHAIN_IDS.get(chain, chain)
    endpoint = KLINE_PATH
    query_params = [
        ("chainIndex", chain_id),
        ("tokenContractAddress", token_address),
        ("bar", bar),
        ("limit", str(min(limit, MAX_LIMIT))),
    ]

    # Build URL path with query for signature
    query_string = "&".join(f"{k}={v}" for k, v in query_params)
    path_with_query = f"{endpoint}?{query_string}"
    hdrs = _headers("GET", path_with_query)

    should_close = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)

    try:
        resp = client.get(
            f"{base_url}{path_with_query}",
            headers=hdrs,
        )
        resp.raise_for_status()
        data = resp.json()
        return _parse_kline_response(data)
    finally:
        if should_close:
            client.close()


def fetch_kline_range(
    chain: str,
    token_address: str,
    start: datetime,
    end: Optional[datetime] = None,
    bar: str = "1D",
    *,
    client: Optional[httpx.Client] = None,
) -> pd.DataFrame:
    """Fetch kline data across a date range (auto-paginates).

    Args:
        chain: Chain ID or name.
        token_address: Token contract address.
        start: Start datetime (UTC).
        end: End datetime (UTC). Defaults to now.
        bar: Candle interval.

    Returns:
        Combined DataFrame for the full range.
    """
    if end is None:
        end = datetime.now(timezone.utc)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    should_close = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)

    try:
        # Strategy: fetch newest candles, then paginate backwards
        all_frames = []
        before = end_ms

        while True:
            chain_id = CHAIN_IDS.get(chain, chain)
            query_params = [
                ("chainIndex", chain_id),
                ("tokenContractAddress", token_address),
                ("bar", bar),
                ("limit", str(MAX_LIMIT)),
                ("before", str(before)),
            ]
            query_string = "&".join(f"{k}={v}" for k, v in query_params)
            path = f"{KLINE_PATH}?{query_string}"
            hdrs = _headers("GET", path)

            resp = client.get(f"{BASE_URL}{path}", headers=hdrs)
            resp.raise_for_status()
            df = _parse_kline_response(resp.json())

            if df.empty:
                break

            all_frames.append(df)

            # Get the earliest timestamp in this batch
            earliest = int(df.index[0].timestamp() * 1000)
            if earliest <= start_ms:
                break
            before = earliest

            # Rate limit protection
            time.sleep(0.5)

        if not all_frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        result = pd.concat(all_frames)
        result = result[~result.index.duplicated()].sort_index()
        return result[(result.index >= start) & (result.index <= end)]
    finally:
        if should_close:
            client.close()


def _parse_kline_response(data: dict | list) -> pd.DataFrame:
    """Convert OnchainOS kline response to pandas DataFrame.

    Raw format: [[ts_ms, o, h, l, c, vol, volUsd, confirm], ...]
    """
    # data can be {"candles": [...]} or just [...]
    if isinstance(data, dict):
        candles = data.get("candles", data.get("data", []))
    elif isinstance(data, list):
        candles = data
    else:
        candles = []

    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # If candles are objects (named from CLI), extract values
    if isinstance(candles[0], dict):
        rows = []
        for c in candles:
            ts = int(c.get("ts", 0))  # ms
            rows.append(
                {
                    "timestamp": pd.Timestamp(ts, unit="ms", tz="UTC"),
                    "open": float(c.get("o", 0)),
                    "high": float(c.get("h", 0)),
                    "low": float(c.get("l", 0)),
                    "close": float(c.get("c", 0)),
                    "volume": float(c.get("vol", 0)),
                }
            )
    else:
        # Raw arrays: [ts, o, h, l, c, vol, volUsd, confirm]
        rows = []
        for c in candles:
            ts = int(c[0])  # ms
            rows.append(
                {
                    "timestamp": pd.Timestamp(ts, unit="ms", tz="UTC"),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                }
            )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.set_index("timestamp").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def resolve_token(chain: str, symbol: str) -> str:
    """Look up a token address by chain and symbol (WETH/USDC/WBTC).

    Returns the token contract address, or the symbol unchanged if not found.
    """
    token_map = TOKENS.get(symbol.upper(), {})
    chain_key = chain.lower()
    # Try exact chain name, then chain ID
    return token_map.get(chain_key) or token_map.get(chain, symbol)


def get_available_tokens(chain: str) -> list[str]:
    """List supported token symbols for a chain."""
    chain_key = chain.lower()
    return [sym for sym, addrs in TOKENS.items() if chain_key in addrs]
