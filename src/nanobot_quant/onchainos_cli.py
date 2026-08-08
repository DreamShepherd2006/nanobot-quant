"""Shared OnchainOS CLI wrapper — used by both Quant backtesting and Research enrichment.

Both paths run in the same container where the onchainos CLI binary is available.
This module provides typed wrappers for the official CLI subcommands (v4.3.1 SDK).

Reference: https://github.com/okx/onchainos-skills
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

from nanobot_quant.onchainos_errors import lookup as err_lookup

ONCHAINOS_BIN = "/usr/local/bin/onchainos"

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
_BUILTIN_TOKENS: dict[str, str] = {
    "SOL": WSOL_ADDR,
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
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
        return {"ok": False, "address": None, "source": None,
                "needs_confirmation": False, "issue": None,
                "confirmed": False, "category": "not_found",
                "suggestion": None, "hint": "empty symbol"}

    # L0b: caller already passed a contract address → pass through
    if is_contract_address(raw):
        return {"ok": True, "address": raw, "source": "address",
                "needs_confirmation": False, "issue": None,
                "confirmed": True, "category": None, "suggestion": None,
                "hint": None}

    # L1: builtin (native coin + well-known SPL) — always trusted
    if raw in _BUILTIN_TOKENS:
        return {"ok": True, "address": _BUILTIN_TOKENS[raw], "source": "builtin",
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
        check = _validate_token_entry(entry, chain=chain)
        if check["ok"] or confirmed:
            return {"ok": True, "address": addr, "source": "tokens_json",
                    "needs_confirmation": (not confirmed and not check["ok"]),
                    "issue": check["issue"],
                    "confirmed": confirmed, "category": None,
                    "suggestion": None, "hint": None}
        return {"ok": True, "address": addr, "source": "tokens_json",
                "needs_confirmation": True, "issue": check["issue"],
                "confirmed": False, "category": check["category"],
                "suggestion": None,
                "hint": ("Check this entry in WebUI 业务管理 → tokens.json, "
                          "then re-run with confirm=true to accept it")}

    # L3: CLI lookup
    addr = search_token(raw)
    if addr:
        return {"ok": True, "address": addr, "source": "cli",
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
        "(symbol + address), or use a native token like SOL/USDC/USDT."
    )
    return {"ok": False, "address": None, "source": None,
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
    return len(t) in (32, 44) and all(c in base58 for c in t)


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


def get_token_price(symbol: str, tokens_json: list[dict] | None = None) -> Optional[float]:
    """Get real-time token price as float (USD).

    Accepts a token SYMBOL (e.g. "SOL", "USDC") or a contract address.
    ``tokens_json`` entries are honoured for symbols outside the built-in
    whitelist (see ``get_price``).
    """
    raw = get_price(symbol, tokens_json=tokens_json)
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
    chain: str = "solana",
    wallet: str = "",
) -> Optional[dict]:
    """Execute a swap. Returns dict with swapTxHash / txHash and status.

    Uses ``--readable-amount`` (human-readable token amount, CLI converts
    to minimal units via token decimals). ``chain``/``wallet`` are required
    by onchainos CLI v4.3.x.
    """
    args = [
        "swap", "execute",
        "--from", from_addr,
        "--to", to_addr,
        "--readable-amount", amount,
        "--chain", chain,
    ]
    if wallet:
        args += ["--wallet", wallet]
    if slippage:
        args += ["--slippage", slippage]
    return _run(*args, timeout=30)


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
