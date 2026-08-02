"""
OnchainOS CLI/API error codes and their human-readable descriptions.

Sources:
  - https://web3.okx.com/onchainos/dev-docs/trade/dex-error-code          (Swap)
  - https://web3.okx.com/build/dev-docs-v5/dex-api/dex-balance-error-code (Market)
  - https://web3.okx.com/build/dev-docs/wallet-api/balance-error-code     (Balance)

Each entry is keyed by numeric error code (int).  The ``lookup`` function
accepts an int, str, or a raw response dict and returns a human-readable
description.
"""

from __future__ import annotations

from typing import Any, Optional

# ── Market / Balance API ──────────────────────────────────────────
MARKET_ERRORS: dict[int, str] = {
    50013: "System is busy — try again later",
    50014: "Parameter is invalid",
    50026: "System error — try again later",
    50100: "Invalid request",
    50102: "Access frequency limited — throttle your requests",
    50103: "Missing OK-ACCESS-KEY header",
    50104: "Missing OK-ACCESS-PASSPHRASE header",
    50105: "OK-ACCESS-PASSPHRASE is incorrect",
    50106: "Missing OK-ACCESS-SIGN header",
    50107: "Missing OK-ACCESS-TIMESTAMP header",
    50110: "Invalid parameters",
    50111: "Invalid OK-ACCESS-KEY",
    50112: "Invalid timestamp (OK-ACCESS-TIMESTAMP)",
    50113: "Invalid signature",
    50114: "Invalid Authority — login session expired, re-authorize via wallet_login",
    50115: "Invalid access",
    50121: "Request frequency limit — throttle your requests",
    50122: "This endpoint is not supported for your user class",
    50123: "User verification failed",
    50124: "Invalid balance",
    50125: "Restricted region — DEX is not available in your location",
    50126: "Incorrect passphrase",
    51001: "Unsupported trading pair",
    51002: "Symbol is not supported",
    51003: "Request timeout — try again",
    52001: "Insufficient balance — wallet does not have enough funds",
    52002: "No supported assets in wallet",
}

# ── Swap API ───────────────────────────────────────────────────────
SWAP_ERRORS: dict[int, str] = {
    80000: "Repeated request — duplicate order",
    80001: "CallData exceeds maximum limit — try again in 5 minutes",
    80002: "Token Object count has reached the limit",
    80003: "Native token Object count has reached the limit",
    80004: "SUI Object query timeout",
    80005: "Not enough Sui objects under address for swapping",
    82000: "Insufficient liquidity for this swap",
    82001: "Commission service is unavailable during upgrade",
    82003: "Referrer wallet address is not valid",
    82004: "Commission split for Four.meme swaps is not supported",
    82005: "Commission split for Aspecta swaps is not supported",
    82102: "Swap amount is below the minimum quantity limit",
    82103: "Swap amount exceeds the maximum quantity limit",
    82104: "This token is not supported for swapping",
    82105: "This chain is not supported for swapping",
    82112: (
        "Price slippage too high — the value difference from the quoted route "
        "exceeds the allowed threshold (default 10%). Adjust slippage or try a "
        "smaller amount."
    ),
    82116: "CallData exceeds maximum limit — try again in 5 minutes",
    82130: "This chain does not require authorized transactions",
}

# ── Wallet / Auth ──────────────────────────────────────────────────
WALLET_ERRORS: dict[int, str] = {
    0: "Succeeded",
}

# ── Merged registry ────────────────────────────────────────────────
# Priority: Swap > Market > Wallet (swap-specific codes take precedence)
ERROR_CODES: dict[int, str] = {}
ERROR_CODES.update(MARKET_ERRORS)
ERROR_CODES.update(WALLET_ERRORS)
ERROR_CODES.update(SWAP_ERRORS)


def lookup(code_or_response: Any) -> str:
    """Return a human-readable description for an error code.

    Accepts:
      - ``int``   — direct error code (e.g. 52001)
      - ``str``   — stringified code (e.g. "52001") or raw message
      - ``dict``  — onchainos response dict (reads ``code`` or ``error`` keys)

    Returns the description if found, otherwise the original value as a string.
    """
    code: Optional[int] = None
    raw: str = ""

    if isinstance(code_or_response, int):
        code = code_or_response
        raw = str(code)
    elif isinstance(code_or_response, str):
        raw = code_or_response
        try:
            code = int(raw)
        except ValueError:
            pass
    elif isinstance(code_or_response, dict):
        # Try numeric "code" key first, then "error" message key,
        # then peek into _stdout_parsed / _stderr_parsed (CLI error output)
        for src in (
            code_or_response,
            code_or_response.get("_stdout_parsed") or {},
            code_or_response.get("_stderr_parsed") or {},
        ):
            code_val = src.get("code")
            if isinstance(code_val, int):
                code = code_val
                raw = str(code)
                break
            if isinstance(code_val, str):
                raw = code_val
                try:
                    code = int(code_val)
                    break
                except ValueError:
                    pass
        if code is None:
            # Fallback: peek into data payload ({"ok": false, "data": {"code": ..., ...}})
            data_src = code_or_response.get("data") or {}
            if isinstance(data_src, dict):
                for k in ("code", "error", "message", "errMsg", "msg"):
                    v = data_src.get(k)
                    if v:
                        raw = str(v)
                        try:
                            code = int(v)
                        except (TypeError, ValueError):
                            pass
                        break
            if code is None:
                raw = code_or_response.get("error", str(code_or_response))

    if code is not None and code in ERROR_CODES:
        return f"[{code}] {ERROR_CODES[code]}"
    return raw
