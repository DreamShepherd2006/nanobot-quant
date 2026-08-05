"""OnchainOS market data adapter — replace yfinance for crypto backtesting.

Kline data is fetched via `onchainos` CLI (subprocess) to bypass datacenter
IP restrictions that block direct REST API calls.
Returns pandas DataFrames compatible with the existing TD Sequential /
backtest pipeline.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

MAX_LIMIT = 300  # max candles per CLI call

# ── Common chain names (CLI uses names, not IDs) ─────────────────
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

# ── Common token addresses ───────────────────────────────────────
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
        "solana": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    },
    "WBTC": {
        "arbitrum": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "ethereum": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "base": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",
    },
    "SOL": {
        "solana": "So11111111111111111111111111111111111111112",
    },
    "WSOL": {
        "solana": "So11111111111111111111111111111111111111112",
    },
    "CRCLx": {
        "solana": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
    },
}


def _resolve_chain_name(chain: str) -> str:
    """Resolve chain ID/number to name that CLI accepts (e.g. '501'→'solana')."""
    # If it's a known name, return as-is
    if chain.lower() in CHAIN_IDS:
        return chain.lower()
    # If it's a numeric ID, look up the name
    for name, cid in CHAIN_IDS.items():
        if cid == chain or name.lower() == chain.lower():
            return name
    return chain.lower()


def _run_cli(args: list[str], timeout: int = 30) -> dict:
    """Run onchainos CLI and return parsed JSON response.

    Raises RuntimeError if the CLI fails.
    """
    cmd = ["onchainos"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.strip() or r.stdout.strip()[:300]
        raise RuntimeError(
            f"onchainos {' '.join(args[:3])} failed (exit {r.returncode}): {err}"
        )
    return json.loads(r.stdout)


def fetch_kline(
    chain: str,
    token_address: str,
    bar: str = "1D",
    limit: int = 100,
) -> pd.DataFrame:
    """Fetch kline/candlestick data from OnchainOS via CLI.

    Args:
        chain: Chain name (e.g. "solana", "arbitrum") or ID.
        token_address: Token contract address.
        bar: Candle interval — "1m", "5m", "15m", "1H", "4H", "1D", "1W".
        limit: Number of candles (max 300 per call).

    Returns:
        DataFrame with columns: open, high, low, close, volume, timestamp.
        Index is datetime64[ns] in UTC.
    """
    chain_name = _resolve_chain_name(chain)
    args = [
        "market", "kline",
        "--address", token_address,
        "--chain", chain_name,
        "--bar", bar,
        "--limit", str(min(limit, MAX_LIMIT)),
    ]
    data = _run_cli(args)
    return _parse_kline_response(data)


def fetch_kline_range(
    chain: str,
    token_address: str,
    start: datetime,
    end: Optional[datetime] = None,
    bar: str = "1D",
) -> pd.DataFrame:
    """Fetch kline data across a date range via CLI (auto-paginates backwards).

    Args:
        chain: Chain name (e.g. "solana") or ID.
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
    chain_name = _resolve_chain_name(chain)

    all_frames: list[pd.DataFrame] = []

    # First call: no --before (gets newest candles)
    args = [
        "market", "kline",
        "--address", token_address,
        "--chain", chain_name,
        "--bar", bar,
        "--limit", str(MAX_LIMIT),
    ]
    data = _run_cli(args)
    df = _parse_kline_response(data)
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    all_frames.append(df)

    # Check if we already covered the start
    earliest = int(df.index[0].timestamp() * 1000)
    if earliest <= start_ms:
        return _trim_range(all_frames, start, end)

    # Paginate backwards using --before
    while earliest > start_ms:
        args = [
            "market", "kline",
            "--address", token_address,
            "--chain", chain_name,
            "--bar", bar,
            "--limit", str(MAX_LIMIT),
            "--before", str(earliest),
        ]
        data = _run_cli(args)
        df = _parse_kline_response(data)
        if df.empty:
            break
        all_frames.append(df)
        earliest = int(df.index[0].timestamp() * 1000)
        if earliest <= start_ms:
            break
        time.sleep(0.3)  # rate limit

    return _trim_range(all_frames, start, end)


def _trim_range(
    frames: list[pd.DataFrame],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """Concatenate and trim frame list to [start, end]."""
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    # The kline parser builds a tz-aware UTC index; normalise naive
    # boundaries (e.g. parsed from a date-only form field) to UTC so the
    # comparison below does not raise
    # "Invalid comparison between dtype=datetime64[ns, UTC] and datetime".
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    result = pd.concat(frames)
    result = result[~result.index.duplicated()].sort_index()
    return result[(result.index >= start) & (result.index <= end)]


def _parse_kline_response(data: dict | list) -> pd.DataFrame:
    """Convert OnchainOS kline response to pandas DataFrame.

    CLI format: {"data": [{"ts": ..., "o": ..., "h": ..., "l": ..., "c": ..., "vol": ...}, ...]}
    REST format: [[ts_ms, o, h, l, c, vol, volUsd, confirm], ...]
    """
    if isinstance(data, dict):
        candles = data.get("candles", data.get("data", []))
    elif isinstance(data, list):
        candles = data
    else:
        candles = []

    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # CLI returns dict objects
    if isinstance(candles[0], dict):
        rows = []
        for c in candles:
            ts = int(c.get("ts", 0))
            rows.append({
                "timestamp": pd.Timestamp(ts, unit="ms", tz="UTC"),
                "open": float(c.get("o", 0)),
                "high": float(c.get("h", 0)),
                "low": float(c.get("l", 0)),
                "close": float(c.get("c", 0)),
                "volume": float(c.get("vol", 0)),
            })
    else:
        # Raw arrays: [ts, o, h, l, c, vol, volUsd, confirm]
        rows = []
        for c in candles:
            ts = int(c[0])
            rows.append({
                "timestamp": pd.Timestamp(ts, unit="ms", tz="UTC"),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.set_index("timestamp").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def resolve_token(chain: str, symbol: str) -> str:
    """Look up a token address by chain and symbol (WETH/USDC/WBTC/SOL/etc).

    Returns the token contract address, or the symbol unchanged if not found.
    """
    token_map = TOKENS.get(symbol.upper(), {})
    # Try chain name, then chain ID, then return as-is
    result = token_map.get(chain.lower()) or token_map.get(chain)
    if result:
        return result
    return symbol


def get_available_tokens(chain: str) -> list[str]:
    """List supported token symbols for a chain."""
    chain_key = chain.lower()
    return [sym for sym, addrs in TOKENS.items() if chain_key in addrs]
