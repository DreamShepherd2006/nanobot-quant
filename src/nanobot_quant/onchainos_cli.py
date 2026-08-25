"""Shared OnchainOS CLI wrapper — used by both Quant backtesting and Research enrichment.

Both paths run in the same container where the onchainos CLI binary is available.
This module provides typed wrappers for the official CLI subcommands (v4.3.1 SDK).

Reference: https://github.com/okx/onchainos-skills
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from nanobot_quant.onchainos_errors import lookup as err_lookup

ONCHAINOS_BIN = "/usr/local/bin/onchainos"

# Persistent storage for onchainos session data (survives Factory Rebuilds)
_PERSISTENT_ONCHAINOS_DIR = "/data/legion/credentials/onchainos_sessions"


def ensure_onchainos_dir() -> None:
    """Symlink ~/.onchainos → persistent /data/legion/credentials/onchainos_sessions/.

    After Factory Rebuild ~/.onchainos/ is wiped, but keyring.enc and
    session.json persist under /data/.  This symlink makes wallet login
    survive rebuilds.  Idempotent; safe to call on every CLI entry point.
    """
    home_onchainos = os.path.expanduser("~/.onchainos")

    # Already a correct symlink → done
    if os.path.islink(home_onchainos):
        target = os.readlink(home_onchainos)
        if target == _PERSISTENT_ONCHAINOS_DIR:
            return
        # Wrong target — remove and re-link
        os.unlink(home_onchainos)
    elif os.path.isdir(home_onchainos):
        # Real directory (not a symlink) — clean up
        import shutil
        shutil.rmtree(home_onchainos, ignore_errors=True)
    elif os.path.isfile(home_onchainos):
        os.unlink(home_onchainos)

    # Create persistent target if not yet exists
    os.makedirs(_PERSISTENT_ONCHAINOS_DIR, exist_ok=True)

    # Create symlink
    os.symlink(_PERSISTENT_ONCHAINOS_DIR, home_onchainos)
    print(
        f"[DIAG] ensure_onchainos_dir: {home_onchainos} → {_PERSISTENT_ONCHAINOS_DIR}",
        file=sys.stderr, flush=True,
    )


logger = logging.getLogger("nanobot_quant.onchainos_cli")

_env_injected = False


def _ensure_env() -> None:
    """Inject OKX credentials into os.environ so onchainos CLI can call Market API.

    Market endpoints (kline, price, token search) require OKX_API_KEY /
    OKX_SECRET_KEY / OKX_PASSPHRASE env vars.  Wallet endpoints use
    ~/.onchainos/keyring.enc instead.

    Idempotent — called once before the first CLI invocation.
    """
    global _env_injected
    if _env_injected:
        return
    try:
        from nanobot_quant.okx_credentials import inject_env
        inject_env()
    except Exception:
        pass
    _env_injected = True


def _run(*args, timeout: int = 15) -> Optional[dict | list]:
    """Run onchainos CLI and return parsed JSON output."""
    _ensure_env()
    try:
        r = subprocess.run(
            [ONCHAINOS_BIN, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            stderr_tail = r.stderr.strip()[-200:] if r.stderr else "(no stderr)"
            stdout_tail = r.stdout.strip()[-200:] if r.stdout else "(no stdout)"
            # onchainos CLI writes the error envelope ({"ok": false, "error": ...})
            # to stdout on failure, not stderr. Parse both.
            def _parse_json(text: str) -> Optional[dict]:
                if not text.strip():
                    return None
                try:
                    return json.loads(text.strip())
                except json.JSONDecodeError:
                    return None

            stderr_parsed = _parse_json(r.stderr)
            stdout_parsed = _parse_json(r.stdout)
            # Prefer stdout envelope (actual error message lives there)
            err_desc = err_lookup(stdout_parsed) if stdout_parsed else ""
            if not err_desc or err_desc == str(stdout_parsed):
                err_desc = err_lookup(stderr_parsed) if stderr_parsed else ""
            if not err_desc or err_desc == str(stderr_parsed):
                err_desc = stdout_tail if stdout_tail != "(no stdout)" else stderr_tail
            logger.warning("onchainos CLI exit=%d: %s", r.returncode, err_desc)
            return {
                "_exit_code": r.returncode,
                "_stdout": r.stdout.strip(),
                "_stdout_parsed": stdout_parsed,
                "_stderr": r.stderr.strip(),
                "_stderr_parsed": stderr_parsed,
            }
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


def get_advanced_info(address: str, chain: str = "solana") -> Optional[dict]:
    """Get token security/risk info: risk level, holder concentration, creator stats.

    Returns raw dict from CLI or None on failure.
    Keys: riskControlLevel, top10HoldPercent, devHoldingPercent, etc.
    """
    return _run("token", "advanced-info", "--address", address, "--chain", chain)


def get_holders(address: str, *, include_pnl: bool = False) -> Optional[list]:
    """Get top token holders with amounts and PnL.

    Returns list of holder dicts (top 100 by default) or None on failure.
    """
    args: list[str] = ["--address", address]
    if include_pnl:
        args.append("--pnl")
    return _run("token", "holders", *args)


# ── Market ────────────────────────────────────────────────────────

def get_price(symbol: str, chain: str = "solana", tokens_json: list[dict] | None = None) -> Optional[str]:
    """Get real-time token price in USD.

    Uses the official onchainos CLI pricing path — ``market price``
    (POST /api/v6/dex/market/price, "Get token price by contract address")
    first, then falls back to the aggregated index price
    (``market index``, POST /api/v6/dex/index/current-price, multi-source
    — documented at dev-docs/market/index-price).  Candle closes are NOT
    used as prices (kline is a data endpoint, not a pricing endpoint).
    Accepts a token SYMBOL (e.g. "SOL", "USDC") or a contract address.
    ``tokens_json`` entries are honoured for symbols outside the built-in
    whitelist (e.g. CRCLX registered by the user) — without it, CLI-only
    lookup misses tokens not indexed by OKX DEX token search.
    Returns price as string or None.
    """
    # For stablecoins, return "1"
    if symbol.upper() in ("USDC", "USDT"):
        return "1"

    addr = resolve_token_address(symbol, tokens_json=tokens_json)
    if not addr:
        return None

    # Official pricing path: market price first, aggregated index fallback.
    p = get_market_price(addr, chain=chain)
    if p:
        return p
    idx = get_index_price(addr, chain=chain)
    if idx:
        return idx
    return None


def get_market_price(address: str, chain: str = "solana") -> Optional[str]:
    """Get token market price via the official ``market price`` subcommand.

    Runs ``onchainos market price --address <addr> --chain <chain>`` which
    POSTs /api/v6/dex/market/price ("Get token price by contract address")
    and prints ``{"ok": true, "data": [{"price": ...}]}``.  Returns the
    price string or None (e.g. no price data for the token on this chain).
    """
    result = _run("market", "price", "--address", address, "--chain", chain)
    if isinstance(result, dict):
        items = result.get("data")
        if isinstance(items, list) and items:
            p = items[0].get("price")
            if p is not None:
                return str(p)
        if isinstance(items, dict):
            p = items.get("price")
            if p is not None:
                return str(p)
        p = result.get("price")
        if p is not None:
            return str(p)
    return None


def get_index_price(address: str, chain: str = "solana") -> Optional[str]:
    """Get aggregated index price (multi-source) for a token address.

    Runs ``onchainos market index --address <addr> --chain <chain>`` which
    POSTs /api/v6/dex/index/current-price and prints
    ``{"ok": true, "data": [{"price": ...}]}``.  Returns the price string
    or None.
    """
    result = _run("market", "index", "--address", address, "--chain", chain)
    if isinstance(result, dict):
        items = result.get("data")
        if isinstance(items, list) and items:
            p = items[0].get("price")
            if p is not None:
                return str(p)
        p = result.get("price")
        if p is not None:
            return str(p)
    return None


def get_kline(
    address: str,
    bar: str = "1D",
    limit: int = 100,
    chain: str = "solana",
) -> Optional[list]:
    """Get K-line/candlestick data.

    Returns list of candle dicts ({ts, o, h, l, c, vol, volUsd, confirm})
    or None on failure. Max 299 candles.
    """
    # Parameter order must match onchainos_data._run_cli (verified working):
    #   market kline --address X --chain solana --bar 1D --limit 300
    result = _run("market", "kline", "--address", address, "--chain", chain, "--bar", bar, "--limit", str(limit))
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            return data
    return None


# ── Swap ───────────────────────────────────────────────────────────

WSOL_ADDR = "So11111111111111111111111111111111111111112"

# Well-known Solana tokens (native coin + common trading pairs)
_BUILTIN_TOKENS: dict[str, dict] = {
    "SOL":  {"chain": "solana",   "address": WSOL_ADDR},
    "USDC": {"chain": "solana",   "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
    "USDT": {"chain": "solana",   "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
    # ETH/BTC/BNB use wrapped variants — the actual on-chain DEX pair
    # (WETH/WBTC/WBNB) on their native chains; CEX 交易对由 gate_pair 回退 symbol。
    "ETH":  {"chain": "ethereum", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"},
    "BTC":  {"chain": "ethereum", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"},
    "BNB":  {"chain": "bnb",      "address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"},
    # 2026-08-25 扩展：主流币（地址经官方浏览器核对）
    "AVAX": {"chain": "avalanche", "address": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7"},  # WAVAX
    "LINK": {"chain": "ethereum",  "address": "0x514910771AF9Ca656af840dff83E8264EcF986CA"},
    "UNI":  {"chain": "ethereum",  "address": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"},
    "AAVE": {"chain": "ethereum",  "address": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9"},
    "SHIB": {"chain": "ethereum",  "address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"},
    "PEPE": {"chain": "ethereum",  "address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933"},
    "ARB":  {"chain": "arbitrum",  "address": "0x912CE59144191C1204E64559FE8253a0e49E6548"},
    "OP":   {"chain": "optimism",  "address": "0x4200000000000000000000000000000000000042"},
    "POL":  {"chain": "polygon",   "address": "0x455e53CBB86018Ac2B8092FdCd39d8444aFFC3F6"},  # 原 MATIC 1:1
}

# Common aliases / full names → canonical symbol (L1 tolerance for
# typos and non-symbol inputs such as "SOLANA" or "BITCOIN").
_ALIASES: dict[str, str] = {
    "SOLANA": "SOL",
    "WRAPPEDSOL": "SOL",
    "WRAPPED_SOL": "SOL",
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "TETHER": "USDT",
    "USDCOIN": "USDC",
    "USDTETHER": "USDT",
}

# Guidance for symbols that exist but have NO native token on Solana.
_CHAIN_HINTS: dict[str, str] = {
    "BTC": (
        "BTC has no native token on Solana; on-chain execution is rejected "
        "(fail-closed). Research via CEX data (OKX BTC-USDT) is still available."
    ),
    "ETH": (
        "ETH has no native token on Solana; on-chain execution is rejected "
        "(fail-closed). Research via CEX data (OKX ETH-USDT) is still available."
    ),
}


def normalize_symbol(raw: str) -> str:
    """L0: normalise free-text input to a bare uppercase symbol.

    - trims whitespace and drops a leading ``$`` (``"$SOL"`` → ``"SOL"``)
    - contract addresses pass through UNCHANGED (base58 is case-sensitive)
    - trading-pair suffixes are split off (``"BTC-USDT"`` → ``"BTC"``)
    """
    p = str(raw or "").strip()
    if not p:
        return ""
    if is_contract_address(p):
        return p
    p = p.lstrip("$").strip().upper()
    for sep in ("-", "."):
        if sep in p:
            p = p.split(sep)[0]
    return p


def resolve_token(
    symbol: str,
    tokens_json: list[dict] | None = None,
    chain: str = "solana",
) -> dict:
    """Unified tiered token resolution with validation + confirmation state.

    This is the single source of truth used by every token gate
    (``run_research_chain`` pre-check, ``pipeline.run_from_signals`` terminal
    gate, VT grounding enrichment) so the whitelists cannot drift apart.

    Tier order:
      L0 normalise → L1 builtin (SOL/USDC/USDT + aliases) → L2 tokens.json
      (user-configured, validated; questionable entries need confirmation)
      → L3 CLI query → L4 structured error (typo suggestion / not_found).

    Returns an envelope dict::

        {
          "ok": True/False,
          "address": str|None,
          "chain": str,             # resolved chain — tokens.json entry
                                    # wins over the caller default; builtin
                                    # native coins are always "solana"
          "source": "address"|"builtin"|"tokens_json"|"cli"|None,
          "needs_confirmation": bool,   # True → caller must get explicit
                                        # user confirmation (confirm=True)
          "issue": str|None,            # why confirmation is needed
          "confirmed": bool,            # tokens.json entry confirmed by user
          "category": None|"typo"|"not_found"|"chain_mismatch"|"invalid_address",
          "suggestion": str|None,       # did-you-mean hint
          "hint": str|None,             # guidance for the caller
        }
    """
    raw = normalize_symbol(symbol)
    if not raw:
        return {"ok": False, "address": None, "chain": chain, "source": None,
                "needs_confirmation": False, "issue": None,
                "confirmed": False, "category": "not_found",
                "suggestion": None, "hint": "empty symbol"}

    # L0b: caller already passed a contract address → pass through
    if is_contract_address(raw):
        return {"ok": True, "address": raw, "chain": chain, "source": "address",
                "needs_confirmation": False, "issue": None,
                "confirmed": True, "category": None, "suggestion": None,
                "hint": None}

    # L1: builtin (well-known tokens per chain) — always trusted
    if raw in _BUILTIN_TOKENS:
        _b = _BUILTIN_TOKENS[raw]
        return {"ok": True, "address": _b["address"],
                "chain": _b["chain"], "source": "builtin",
                "needs_confirmation": False, "issue": None,
                "confirmed": True, "category": None, "suggestion": None,
                "hint": None}

    # Alias → canonical, then re-enter resolution (SOLANA→SOL, BITCOIN→BTC…)
    canonical = _ALIASES.get(raw, raw)
    if canonical != raw:
        return resolve_token(canonical, tokens_json=tokens_json, chain=chain)

    # L2: user-configured tokens.json (highest trust — validated, not blind)
    for entry in (tokens_json or []):
        if str(entry.get("symbol", "")).upper() != raw:
            continue
        addr = str(entry.get("address") or "").strip()
        confirmed = bool(entry.get("confirmed", False))
        # The entry's own chain wins over the caller-provided default —
        # tokens.json is the single managed gate that records where a
        # target lives (e.g. SPCXB → bnb).
        entry_chain = str(entry.get("chain") or chain).lower()
        check = _validate_token_entry(entry, chain=entry_chain)
        if check["ok"] or confirmed:
            return {"ok": True, "address": addr, "chain": entry_chain,
                    "source": "tokens_json",
                    "needs_confirmation": (not confirmed and not check["ok"]),
                    "issue": check["issue"],
                    "confirmed": confirmed, "category": None,
                    "suggestion": None, "hint": None}
        return {"ok": True, "address": addr, "chain": entry_chain,
                "source": "tokens_json",
                "needs_confirmation": True, "issue": check["issue"],
                "confirmed": False, "category": check["category"],
                "suggestion": None,
                "hint": ("Check this entry in WebUI 业务管理 → tokens.json, "
                          "then re-run with confirm=true to accept it")}

    # L3: CLI lookup
    addr = search_token(raw)
    if addr:
        return {"ok": True, "address": addr, "chain": chain, "source": "cli",
                "needs_confirmation": False, "issue": None,
                "confirmed": True, "category": None, "suggestion": None,
                "hint": None}

    # L4: structured failure (typo suggestion / not_found / chain hint)
    candidates = list(_BUILTIN_TOKENS) + list(_ALIASES)
    for entry in (tokens_json or []):
        s = str(entry.get("symbol", "")).upper()
        if s and s not in candidates:
            candidates.append(s)
    suggestion = _fuzzy_suggestion(raw, candidates)
    category = "typo" if suggestion else "not_found"
    hint = _CHAIN_HINTS.get(raw) or (
        "Configure the token in WebUI 业务管理 → tokens.json "
        "(symbol + address + chain), or use a native token like SOL/USDC/USDT."
    )
    return {"ok": False, "address": None, "chain": chain, "source": None,
            "needs_confirmation": False, "issue": None,
            "confirmed": False, "category": category,
            "suggestion": suggestion, "hint": hint}


def _validate_token_entry(entry: dict, chain: str = "solana") -> dict:
    """Local validation of a tokens.json entry (no network calls).

    Checks address presence, chain ownership (EVM address on a solana chain)
    and address format. Returns ``{"ok": bool, "issue": str|None,
    "category": str|None}``.
    """
    addr = str(entry.get("address") or "").strip()
    if not addr:
        return {"ok": False, "issue": "missing address",
                "category": "invalid_address"}
    if chain == "solana" and addr.lower().startswith("0x"):
        return {"ok": False,
                "issue": f"address is an EVM (0x…) address, not a {chain} address",
                "category": "chain_mismatch"}
    if not is_contract_address(addr):
        return {"ok": False,
                "issue": f"'{addr[:16]}…' is not a valid contract address",
                "category": "invalid_address"}
    return {"ok": True, "issue": None, "category": None}


def _fuzzy_suggestion(raw: str, candidates: list[str]) -> Optional[str]:
    """Best-effort did-you-mean suggestion (input len>=3, cutoff 0.8)."""
    if len(raw) < 3 or not candidates:
        return None
    import difflib
    matches = difflib.get_close_matches(raw, candidates, n=1, cutoff=0.8)
    return matches[0] if matches else None


def token_json_path() -> Path:
    """Path to the user-configured tokens.json (WebUI 业务管理)."""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / "tokens.json"
        except OSError:
            continue
    return Path.home() / ".tokens.json"


def confirm_token(
    symbol: str,
    tokens_json: list[dict] | None = None,
    address: str | None = None,
) -> dict:
    """Mark a tokens.json entry as user-confirmed (persist ``confirmed=true``).

    Call ONLY after the user explicitly confirmed the entry — this is the
    confirmation memory (scheme C).  A confirmed entry passes the resolution
    gate on later runs without asking again.  If the address is later edited
    in WebUI, the confirmation is automatically reset (the entry is treated
    as unconfirmed until the user confirms the new address).

    Returns a short status dict.
    """
    path = token_json_path()
    if not path.is_file():
        return {"ok": False, "error": f"tokens.json not found at {path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"failed to read tokens.json: {exc}"}
    if not isinstance(data, list):
        return {"ok": False, "error": "tokens.json is not a list"}
    raw = normalize_symbol(symbol)
    changed = False
    for entry in data:
        if str(entry.get("symbol", "")).upper() != raw:
            continue
        if address and str(entry.get("address", "")) != address:
            # Entry changed since the check — do not confirm a stale row.
            continue
        entry["confirmed"] = True
        changed = True
    if not changed:
        return {"ok": False, "error": f"no matching token entry for {raw}"}
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"failed to write tokens.json: {exc}"}
    return {"ok": True, "confirmed_symbol": raw, "path": str(path)}


def resolve_token_address(
    symbol: str,
    tokens_json: list[dict] | None = None,
) -> Optional[str]:
    """Backward-compatible wrapper around ``resolve_token`` (address only)."""
    return resolve_token(symbol, tokens_json=tokens_json).get("address")


def bare_symbol(pair: str) -> str:
    """Strip a trading pair/symbol down to its bare token symbol.

    "BTC-USDT" -> "BTC", "AAPL.US" -> "AAPL", "SOL" -> "SOL".
    Contract addresses pass through unchanged (base58 is case-sensitive, so
    they must NOT be upper-cased).
    """
    p = str(pair or "").strip()
    if is_contract_address(p):
        return p
    p = p.upper()
    for sep in ("-", "."):
        if sep in p:
            return p.split(sep)[0]
    return p


def is_contract_address(s: str) -> bool:
    """Heuristic: is this string already a contract address (not a symbol)?

    Solana base58 addresses are 32 bytes (~43-44 chars); EVM addresses are
    42 chars starting with ``0x``.  Used so the pipeline safety gate treats
    a pre-resolved address (quant line passes ``ticker=address``) as valid
    without re-resolving it as a symbol.
    """
    t = str(s or "").strip()
    if not t:
        return False
    if t.lower().startswith("0x"):
        return len(t) == 42
    base58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return 32 <= len(t) <= 44 and all(c in base58 for c in t)


def supported_symbols(tokens_json: list[dict] | None = None) -> list[str]:
    """Return the symbols that can be resolved on-chain today.

    Native/known tokens plus any user-configured entries from ``tokens.json``.
    """
    syms = ["SOL", "USDC", "USDT"]
    for entry in (tokens_json or []):
        s = str(entry.get("symbol", "")).upper()
        if s and s not in syms:
            syms.append(s)
    return syms


def chain_results_dir(roots: tuple = ("/data", "/mnt/workspace")) -> Path:
    """Persistent directory for research-chain execution outcomes.

    ``{data_root}/legion/research_chains/`` (independent from the credentials
    directory so audit records stay readable; survives Factory Rebuild).
    Falls back to ``~/.research_chains`` when neither HF nor MS root exists.
    The ``roots`` parameter is test-only (inject a tmp dir to keep the test
    environment-independent).
    """
    for root in roots:
        d = Path(root) / "legion" / "research_chains"
        try:
            if d.parent.exists():
                d.mkdir(parents=True, exist_ok=True)
                return d
        except OSError:
            continue
    d = Path.home() / ".research_chains"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backtests_dir(roots: tuple = ("/data", "/mnt/workspace")) -> Path:
    """Persistent directory for async backtest results.

    ``{data_root}/legion/backtests/`` (survives Factory Rebuild; mirrors
    ``chain_results_dir``). Falls back to ``~/.backtests`` when neither HF
    nor MS root exists. ``roots`` is test-only.
    """
    for root in roots:
        d = Path(root) / "legion" / "backtests"
        try:
            if d.parent.exists():
                d.mkdir(parents=True, exist_ok=True)
                return d
        except OSError:
            continue
    d = Path.home() / ".backtests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_token_price(symbol: str, tokens_json: list[dict] | None = None,
                    chain: str = "solana") -> Optional[float]:
    """Get real-time token price as float (USD).

    Accepts a token SYMBOL (e.g. "SOL", "USDC") or a contract address.
    ``tokens_json`` entries are honoured for symbols outside the built-in
    whitelist (see ``get_price``).  ``chain`` is passed through to the
    pricing endpoint (a tokens.json entry with its own ``chain`` wins).
    """
    raw = get_price(symbol, chain=chain, tokens_json=tokens_json)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_token_assets(data) -> list:
    """Extract the token balance list from a ``wallet balance`` response data.

    CLI v4.3.1 puts the detail list at ``data.details[0].tokenAssets``
    (per-token fields: symbol/balance/rawBalance/decimal/tokenPrice/usdValue).
    Older shapes (``data.assets`` / ``data.balances``) are kept as fallback.
    Non-dict entries are filtered out defensively — callers (e.g.
    OnchainOSBroker) iterate the result with ``t.get(...)``.
    """
    if not isinstance(data, dict):
        return []
    details = data.get("details")
    if isinstance(details, list):
        for g in details:
            if isinstance(g, dict):
                ta = g.get("tokenAssets") or g.get("assets")
                if isinstance(ta, list):
                    return [t for t in ta if isinstance(t, dict)]
    for k in ("assets", "balances"):
        v = data.get(k)
        if isinstance(v, list):
            return [t for t in v if isinstance(t, dict)]
    return []


def get_wallet_balance() -> Optional[list]:
    """Get wallet balance from onchainos. Returns list of token dicts.

    CLI v4.3.1 wraps the token list under ``data.details[0].tokenAssets``
    and the CLI JSON envelope wraps that under ``{"ok":true,"data":{...}}``;
    normalise any shape to a flat list of token dicts so callers
    (OnchainOSBroker._pull_positions / _get_balances_at_broker) can iterate
    safely with ``t.get(...)``.

    2026-08-10 修复：此前直接把整个 CLI 信封传给 ``get_token_assets``，
    信封顶层没有 ``details`` 键导致恒返回 []——TD live 循环 portfolio_value
    永远为 0（"TD BLOCK (position_limit) | portfolio value is zero"）。
    """
    data = _run("wallet", "balance")
    if isinstance(data, dict):
        if "_exit_code" in data or data.get("ok") is False:
            print(
                f"[DIAG] wallet balance failed: {str(data)[:300]}",
                file=sys.stderr, flush=True,
            )
            return []
        if data.get("ok") is True and isinstance(data.get("data"), dict):
            data = data["data"]  # 解包 CLI 信封 {"ok":true,"data":{...}}
    assets = get_token_assets(data)
    if not assets:
        print(
            f"[DIAG] wallet balance empty (no tokenAssets): {str(data)[:200]}",
            file=sys.stderr, flush=True,
        )
    return assets


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


# CLI 报错样例: --readable-amount "0.02053879" has more decimal places
#               than this token supports (6 decimals)
_DECIMALS_ERROR_RE = re.compile(
    r"more decimal places than this token supports \((\d+) decimals\)"
)

# 模块级 decimals 缓存：key = f"{from_addr}:{chain}" → token decimals。
# 首次 SELL 被 CLI 拒后解析出实际 decimals 并记住，后续直接按该精度
# 舍入，避免每笔都先试错一次（2026-08-11 SPCX 6 decimals 实证）。
_DECIMALS_CACHE: dict[str, int] = {}


def _round_to(amount: str, decimals: int) -> str:
    """按 token 实际 decimals 舍入 readable-amount。"""
    try:
        return f"{round(float(amount), decimals):.{decimals}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return amount


def swap_execute(
    from_addr: str,
    to_addr: str,
    amount: str,
    slippage: str = "0.01",
    chain: str = "solana",
    wallet: str = "",
) -> Optional[dict]:
    """Execute a swap. Returns dict with swapTxHash / txHash and status.

    Uses ``--readable-amount`` (human-readable token amount, CLI converts
    to minimal units via token decimals). ``chain``/``wallet`` are required
    by onchainos CLI v4.3.x.

    Decimals handling (2026-08-11): the CLI strictly validates readable-amount
    against the from-token's decimals. ``_round_readable_amount`` defaults to
    8 decimals (covers CRCLX/RENDER/SOL), but tokens like SPCX only support 6
    decimals — the CLI then rejects the swap. On rejection we parse the
    ``(N decimals)`` hint out of the CLI error, cache N per (from, chain) and
    retry once with the correct precision. The decimals validation fails
    before any on-chain broadcast, so the retry is safe (deterministic, no
    duplicate orders).
    """
    key = f"{from_addr}:{chain}"
    dec = _DECIMALS_CACHE.get(key)
    amt = _round_to(amount, dec) if dec is not None else _round_readable_amount(amount)
    args = [
        "swap", "execute",
        "--from", from_addr,
        "--to", to_addr,
        "--readable-amount", amt,
        "--chain", chain,
    ]
    if wallet:
        args += ["--wallet", wallet]
    if slippage:
        args += ["--slippage", slippage]
    result = _run(*args, timeout=30)
    if result and result.get("_exit_code") == 1:
        m = _DECIMALS_ERROR_RE.search(
            f"{result.get('_stdout') or ''} {result.get('_stderr') or ''}"
        )
        if m:
            dec = int(m.group(1))
            _DECIMALS_CACHE[key] = dec
            amt2 = _round_to(amount, dec)
            if amt2 != amt:
                args[args.index("--readable-amount") + 1] = amt2
                result = _run(*args, timeout=30)
    return result


def _round_readable_amount(amount: str) -> str:
    """readable-amount 默认舍入到 8 位小数。

    2026-08-11 修复：qty = pv × max_position_pct / price 的浮点除法产生
    15+ 位小数（如 0.042222355341467045），onchainos CLI 按 token decimals
    校验 readable-amount，超限报错 "more decimal places than this token
    supports (8 decimals)" 导致 swap 失败（00:44 CRCLX cd_sell=13 实证）。
    8 位覆盖主流 SPL（CRCLX/RENDER 8 decimals、SOL 9）；SPCX 为 6 decimals，
    首次 SELL 被拒后由 swap_execute 的 decimals 缓存/重试机制自动适配
    （见 _DECIMALS_CACHE）。
    """
    try:
        return f"{round(float(amount), 8):.8f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return amount


# txStatus 数值映射（history.rs map_tx_status）：1/2=PENDING 3=ERROR 4=SUCCESS 6=CANCELLED
_TX_STATUS_MAP = {
    "1": "PENDING", "2": "PENDING", "3": "ERROR",
    "4": "SUCCESS", "6": "CANCELLED",
}


def is_placeholder_tx_hash(h: str) -> bool:
    """Gas Station 广播先返回的占位 tx_hash：32 位 hex（UUID 样式），
    非真实链上 hash——用占位 hash 查 detail 模式必然 UNKNOWN（2026-08-11）。"""
    if not h or len(h) != 32:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in h)


def swap_status(
    tx_hash: str = "", order_id: str = "", chain: str = "solana",
) -> Optional[dict]:
    """查询 swap 链上成交状态（官方 wallet history 机制，2026-08-11）。

    swap.rs 的 nextSteps.checkSwapStatus 生成的就是这个命令：
      onchainos wallet history --tx-hash <hash> --chain <chain>
    Gas Station 广播先返回 orderId，relayer 后填充链上 hash——tx_hash
    为空时 fallback 到 ``--order-id`` 查 /order/detail。

    返回 ``{"tx_status": "SUCCESS"|"PENDING"|"ERROR"|"CANCELLED"|"UNKNOWN",
    "raw": ...}``——UNKNOWN 表示查询失败/无状态字段。
    """
    if not tx_hash and not order_id:
        return None

    def _query(flag: str, value: str) -> Optional[dict]:
        args = ["wallet", "history", flag, value, "--chain", chain]
        result = _run(*args, timeout=15)
        if result is None:
            print(
                f"[DIAG] swap_status {flag} 无返回（CLI 调用异常/超时）",
                file=sys.stderr, flush=True,
            )
            return None
        # 2026-08-11 根因修复：_run 成功路径返回原始 JSON（{"ok":true,...}），
        # 没有 _exit_code 键；只有失败路径（returncode!=0）才返回带 _exit_code
        # 的 dict。此前用 get("_exit_code") != 0 判断，None != 0 → 成功响应被
        # 误判为失败 → detail 查询永远 UNKNOWN（09:30 事件根因）。
        if isinstance(result, dict) and result.get("_exit_code") not in (None, 0):
            print(
                f"[DIAG] swap_status {flag} 失败 exit={result.get('_exit_code')} "
                f"stdout={(result.get('_stdout') or '')[:600]!r} "
                f"stderr={(result.get('_stderr') or '')[:300]!r}",
                file=sys.stderr, flush=True,
            )
            return None
        # 成功路径：原始 JSON（无 _stdout_parsed）；失败兼容：_stdout_parsed
        payload = (
            result.get("_stdout_parsed")
            if isinstance(result, dict) and "_stdout_parsed" in result
            else result
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        status = None
        if isinstance(data, dict):
            status = data.get("txStatus")
        elif isinstance(data, list) and data:
            status = data[0].get("txStatus") if isinstance(data[0], dict) else None
        if status is None:
            print(
                f"[DIAG] swap_status {flag} 空数据 payload={str(payload)[:600]}",
                file=sys.stderr, flush=True,
            )
            return None
        s = str(status)
        print(
            f"[DIAG] swap_status {flag} → txStatus={s} payload={str(payload)[:400]}",
            file=sys.stderr, flush=True,
        )
        return {"tx_status": _TX_STATUS_MAP.get(s, s.upper()), "raw": payload}

    # 2026-08-11 双路径：tx_hash 非空先查 tx-hash（占位 UUID 查不到 →
    # UNKNOWN/None 时 fallback 官方 order-id 路径）；tx_hash 为空直接查 order-id。
    st = _query("--tx-hash", tx_hash) if tx_hash else None
    if st is None and order_id:
        st = _query("--order-id", order_id)
    return st if st is not None else {"tx_status": "UNKNOWN", "raw": None}


def confirm_swap_onchain(
    tx_hash: str, order_id: str, chain: str,
    retries: int = 3, delay: tuple[float, ...] = (3, 5, 8),
) -> str:
    """轮询链上确认，返回 ``"success" / "error" / "pending"``（2026-08-11）。

    链上成交确认是“区块链最有价值的地方”——CLI 的 swap 提交成功
    （submitted_on_chain）不等同链上成交：Gas Station 广播先返回
    orderId，relayer 后填充链上 hash，报价阶段判定失败时返回占位 hash
    （曾致 RENDER 3.06 假成功脱管）。以官方 ``wallet history`` 的
    txStatus 为准：SUCCESS=成交、ERROR/CANCELLED=失败、持续 PENDING=
    待确认（由策略层后续轮询补确认）。
    """
    for i in range(retries):
        st = swap_status(tx_hash, order_id, chain)
        status = st.get("tx_status") if st else "UNKNOWN"
        if status == "SUCCESS":
            return "success"
        if status in ("ERROR", "CANCELLED"):
            return "error"
        if i < retries - 1:
            time.sleep(delay[i])
    return "pending"


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
