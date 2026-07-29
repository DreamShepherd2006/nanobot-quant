"""OnchainOS Swap CLI wrapper — used by OnchainOSBroker for DEX execution.

All functions run ``onchainos`` subprocess calls with ``--format json``
and return parsed Python dicts.  Each call spawns a process — QPS < 5
is fine for a minute-level trading loop.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

ONCHAINOS_BIN = "/usr/local/bin/onchainos"

logger = logging.getLogger("nanobot_quant.onchainos_swap")


def _run(*args, timeout: int = 15) -> Optional[dict | list]:
    """Run onchainos CLI and return parsed JSON output.

    Returns None on any failure (non-zero exit, timeout, parse error).
    """
    try:
        r = subprocess.run(
            [ONCHAINOS_BIN, "--format", "json", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.warning(
                "onchainos CLI non-zero exit %d: %s", r.returncode, r.stderr[:200]
            )
            return None
        return json.loads(r.stdout) if r.stdout.strip() else None
    except subprocess.TimeoutExpired:
        logger.warning("onchainos CLI timed out after %ds: %s", timeout, args[:4])
        return None
    except Exception:
        logger.exception("onchainos CLI failed: %s", args[:4])
        return None


# ── Swap ──────────────────────────────────────────────────────────

def swap_quote(
    from_token: str,
    to_token: str,
    from_amount: str,
    slippage: str = "0.01",
) -> Optional[dict]:
    """Get a swap quote.  Returns estimated output + route info.

    Keys in returned dict (best-effort, CLI output may vary):
        fromAmount, toAmount, routes, calldata, priceImpact
    """
    return _run(
        "swap", "quote",
        "--from-token", from_token,
        "--to-token", to_token,
        "--from-amount", from_amount,
        "--from-chain", "solana",
        "--to-chain", "solana",
        "--slippage", slippage,
    )


def swap_execute(
    from_token: str,
    to_token: str,
    from_amount: str,
    slippage: str = "0.01",
) -> Optional[dict]:
    """Execute a swap.  Fire-and-forget — returns txHash immediately.

    On-chain confirmation may take seconds; use :func:`swap_status`
    to poll.

    Keys in returned dict:
        swapTxHash, status, fromAmount, toAmount, ...
    """
    return _run(
        "swap", "execute",
        "--from-token", from_token,
        "--to-token", to_token,
        "--from-amount", from_amount,
        "--from-chain", "solana",
        "--to-chain", "solana",
        "--slippage", slippage,
        "--force",
        timeout=60,
    )


def swap_status(tx_hash: str) -> Optional[dict]:
    """Check swap transaction status by txHash.

    Keys: status ("success"/"pending"/"failed"), fromAmount, toAmount, ...
    """
    return _run("swap", "status", "--tx-hash", tx_hash)


# ── Wallet ────────────────────────────────────────────────────────

def get_wallet_balance() -> Optional[list]:
    """Get wallet token balances.

    Returns list of token dicts (best-effort):
        [{symbol, address, balance, price, valueUsd}, ...]
    """
    result = _run("wallet", "balance")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("tokens") or []
    return None


def get_token_price(address: str) -> Optional[float]:
    """Get real-time token price in USD.  Returns float or None."""
    result = _run("market", "price", "--address", address)
    if isinstance(result, dict):
        raw = result.get("price")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return None


# ── Token Resolution ──────────────────────────────────────────────

# Protocol-level constants — these are not "user tokens", they're
# part of the Solana protocol itself.
NATIVE_SOL = "11111111111111111111111111111111"       # native SOL
WSOL_ADDR  = "So11111111111111111111111111111111111111112"  # Wrapped SOL (SPL)


def resolve_token_address(
    symbol: str,
    tokens_json: list[dict] | None = None,
) -> str | None:
    """Resolve token symbol to Solana contract address.

    Priority:
    1. ``tokens_json`` (user-configured from WebUI, if provided)
    2. ``onchainos token search`` CLI dynamic lookup
    3. Returns None

    ``SOL`` always resolves to the Wrapped SOL (SPL) address.
    """
    if symbol.upper() == "SOL":
        return WSOL_ADDR

    # ① User-configured tokens.json
    if tokens_json:
        for t in tokens_json:
            if t.get("symbol", "").upper() == symbol.upper():
                return t.get("address")

    # ② CLI fallback
    result = _run("token", "search", "--query", symbol)
    if result:
        items = result if isinstance(result, list) else result.get("items") or []
        if isinstance(items, list) and items:
            addr = items[0].get("tokenContractAddress") or items[0].get("address")
            if addr:
                return addr

    return None


def get_kline(address: str, bar: str = "1D", limit: int = 100) -> Optional[list]:
    """Get K-line/candlestick data from onchainos.

    Returns list of candle dicts: {ts, o, h, l, c, vol, volUsd, confirm}
    Max 299 candles per call.
    """
    return _run("market", "kline", "--address", address, "--bar", bar, "--limit", str(limit))
