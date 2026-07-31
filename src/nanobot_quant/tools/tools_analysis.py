"""run_td_sequential: TD Sequential analysis on onchain tokens.

Fetches daily K-line data via OnchainOS CLI, computes DeMark TD
Setup/Countdown/TDST/score, and returns a structured TickerSignal.
"""

from __future__ import annotations

import sys


def run_td_sequential(
    address: str,
    chain: str = "solana",
    bar: str = "1D",
    limit: int = 299,
) -> dict:
    """Run TD Sequential analysis on an OnchainOS token.

    Fetches K-line data via OnchainOS CLI, calculates TD Sequential
    and returns a TickerSignal dict.
    """
    # ── Guard MCP stdio from library import-time logging ─────────
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from nanobot_quant.onchainos_data import fetch_kline as _fetch_kline
        from nanobot_quant.strategies.td_sequential import calculate as _calculate
    finally:
        sys.stdout = _saved_stdout

    chain_name = chain if chain in ("solana", "arbitrum", "ethereum", "base", "bnb", "optimism", "polygon", "xdai") else "solana"

    print(
        f"[DIAG] run_td_sequential: address={address[:12]}... chain={chain_name} bar={bar} limit={limit}",
        file=sys.stderr, flush=True,
    )

    try:
        df = _fetch_kline(
            chain=chain_name,
            token_address=address,
            bar=bar,
            limit=limit,
        )
    except Exception as exc:
        return {"error": f"Failed to fetch kline data: {exc}"}

    if df is None or df.empty:
        return {"error": "No kline data returned from OnchainOS"}

    print(
        f"[DIAG] run_td_sequential: fetched {len(df)} candles ({df.index[0]} → {df.index[-1]})",
        file=sys.stderr, flush=True,
    )

    try:
        result = _calculate(df)
    except Exception as exc:
        return {"error": f"TD Sequential calculation failed: {exc}"}

    result["source"] = "quant"
    result["ticker"] = address
    result["chain"] = chain_name

    print(
        f"[DIAG] run_td_sequential: {result['recommendation']} "
        f"(setup_buy={result['setup_buy']} cd_buy={result['cd_buy']} score={result['score']})",
        file=sys.stderr, flush=True,
    )
    return result
