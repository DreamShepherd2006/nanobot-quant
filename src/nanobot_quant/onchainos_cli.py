"""Shared OnchainOS CLI wrapper — used by both Quant backtesting and Research enrichment.

Both paths run in the same container where the onchainos CLI binary is available.
This module provides typed wrappers for the official CLI subcommands (v4.3.1 SDK).

Reference: https://github.com/okx/onchainos-skills
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

ONCHAINOS_BIN = "/usr/local/bin/onchainos"

logger = logging.getLogger("nanobot_quant.onchainos_cli")


def _run(*args, timeout: int = 15) -> Optional[dict | list]:
    """Run onchainos CLI and return parsed JSON output."""
    try:
        r = subprocess.run(
            [ONCHAINOS_BIN, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.warning("onchainos CLI non-zero exit: %s", r.returncode)
            return None
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


# ── Token ─────────────────────────────────────────────────────────

def search_token(query: str) -> Optional[str]:
    """Search for a token by name/symbol and return its contract address.

    Returns None if not found or CLI unavailable.
    """
    result = _run("token", "search", "--query", query)
    if not result:
        return None
    items = result if isinstance(result, list) else result.get("items") or []
    if isinstance(items, list) and items:
        addr = items[0].get("tokenContractAddress") or items[0].get("address")
        if addr:
            return addr
    return None


def get_advanced_info(address: str) -> Optional[dict]:
    """Get token security/risk info: risk level, holder concentration, creator stats.

    Returns raw dict from CLI or None on failure.
    Keys: riskControlLevel, top10HoldPercent, devHoldingPercent, etc.
    """
    return _run("token", "advanced-info", "--address", address)


def get_holders(address: str, *, include_pnl: bool = False) -> Optional[list]:
    """Get top token holders with amounts and PnL.

    Returns list of holder dicts (top 100 by default) or None on failure.
    """
    args: list[str] = ["--address", address]
    if include_pnl:
        args.append("--pnl")
    return _run("token", "holders", *args)


# ── Market ────────────────────────────────────────────────────────

def get_price(address: str) -> Optional[str]:
    """Get real-time token price in USD.

    Returns price as string or None.
    """
    result = _run("market", "price", "--address", address)
    if isinstance(result, dict):
        return result.get("price")
    return None


def get_kline(
    address: str,
    bar: str = "1D",
    limit: int = 100,
) -> Optional[list]:
    """Get K-line/candlestick data.

    Returns list of candle dicts ({ts, o, h, l, c, vol, volUsd, confirm})
    or None on failure. Max 299 candles.
    """
    return _run("market", "kline", "--address", address, "--bar", bar, "--limit", str(limit))


# ── Swap ───────────────────────────────────────────────────────────

WSOL_ADDR = "So11111111111111111111111111111111111111112"


def resolve_token_address(
    symbol: str,
    tokens_json: list[dict] | None = None,
) -> Optional[str]:
    """Resolve a token symbol to its contract address.

    1. Check ``tokens_json`` (user-configured in WebUI).
    2. Query onchainos CLI ``token search``.
    3. Return WSOL address for "SOL".
    """
    symbol_upper = symbol.upper()
    if symbol_upper == "SOL":
        return WSOL_ADDR

    # 1) User-configured list
    for entry in (tokens_json or []):
        if entry.get("symbol", "").upper() == symbol_upper:
            return entry.get("address")

    # 2) CLI query
    return search_token(symbol)


def get_token_price(address: str) -> Optional[float]:
    """Get real-time token price as float (USD)."""
    raw = get_price(address)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_wallet_balance() -> Optional[list]:
    """Get wallet balance from onchainos. Returns list of token dicts."""
    return _run("wallet", "balance")


def swap_quote(
    from_addr: str,
    to_addr: str,
    amount: str,
    slippage: str = "0.01",
) -> Optional[dict]:
    """Get a swap quote. Returns dict with toAmount, routes, etc."""
    return _run(
        "swap", "quote",
        "--from", from_addr,
        "--to", to_addr,
        "--amount", amount,
        "--slippage", slippage,
        timeout=15,
    )


def swap_execute(
    from_addr: str,
    to_addr: str,
    amount: str,
    slippage: str = "0.01",
) -> Optional[dict]:
    """Execute a swap. Returns dict with swapTxHash / txHash and status."""
    return _run(
        "swap", "execute",
        "--from", from_addr,
        "--to", to_addr,
        "--amount", amount,
        "--slippage", slippage,
        timeout=30,
    )


def swap_status(tx_hash: str) -> Optional[dict]:
    """Check swap transaction status."""
    return _run("swap", "status", "--tx-hash", tx_hash)


# ── Extraction helpers ────────────────────────────────────────────

def extract_symbol(user_vars: dict) -> Optional[str]:
    """Extract bare token name from swarm user_vars, stripping trading pair suffixes.

    user_vars example: {"target": "BTC-USDT", "market": "crypto"}
    Returns "BTC" for crypto pairs, "SPCX" for stocks.
    """
    target = user_vars.get("target", "").strip().upper()
    if not target:
        return None
    # Strip trading pair suffixes: BTC-USDT → BTC
    for suffix in ("-USDT", "-USD", "-USDC"):
        if target.endswith(suffix):
            target = target[:-len(suffix)]
            break
    # Strip stock suffix: SPCX.US → SPCX
    base = target.split(".")[0]
    return base if base else None


def format_risk_level(raw: dict) -> dict[str, str]:
    """Extract human-readable risk fields from advanced-info response."""
    levels = {"0": "Unknown", "1": "Low", "2": "Medium", "3": "Med-High", "4": "High"}
    rl = raw.get("riskControlLevel", "?")
    return {
        "risk_level": levels.get(str(rl), str(rl)),
        "top10_pct": raw.get("top10HoldPercent", "?"),
        "dev_pct": raw.get("devHoldingPercent", "?"),
        "bundle_pct": raw.get("bundleHoldingPercent", "?"),
        "suspicious_pct": raw.get("suspiciousHoldingPercent", "?"),
        "snipers": raw.get("snipersTotal", "?"),
        "creator_rugs": raw.get("devRugPullTokenCount", "?"),
        "creator_tokens": raw.get("devCreateTokenCount", "?"),
    }
