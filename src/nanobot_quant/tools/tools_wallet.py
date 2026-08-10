"""Wallet tools: onchainos login, payment, status.

Consolidated wallet bootstrap: wallet_setup() is the main idempotent entry point.
Individual tools (wallet_login_init/poll/payment_set) are retained for debugging.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from nanobot_quant.onchainos_cli import ensure_onchainos_dir, ONCHAINOS_BIN

_SESSION_ID_FILE = os.path.expanduser("~/.onchainos/last_session_id.txt")


def _ensure_onchainos_dir() -> None:
    """Backward-compat alias → shared onchainos_cli.ensure_onchainos_dir."""
    ensure_onchainos_dir()


def _run_cli(args: list[str], timeout: int = 30, label: str = "") -> dict:
    """Run an onchainos CLI subcommand and return its JSON envelope.

    Returns {"ok": bool, "data": ..., "error": ..., "rc": int} on success;
    {"error": ...} on timeout / missing binary. Diagnostic output goes to
    stderr only (never stdout — the MCP JSON-RPC channel).
    """
    _ensure_onchainos_dir()  # restore ~/.onchainos symlink after Factory Rebuild
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}
    except subprocess.TimeoutExpired:
        return {"error": f"{label or args[1:]} timed out after {timeout}s"}

    print(
        f"[DIAG] {label or ' '.join(args[1:])} rc={proc.returncode} "
        f"stdout={proc.stdout.strip()[:400]!r} "
        f"stderr={proc.stderr.strip()[:400]!r}",
        file=sys.stderr, flush=True,
    )

    text = (proc.stdout or "").strip()
    if not text:
        text = (proc.stderr or "").strip()
    data = None
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass
    if isinstance(data, dict):
        data.setdefault("rc", proc.returncode)
        return data
    return {
        "ok": proc.returncode == 0,
        "rc": proc.returncode,
        "data": data if data is not None else text[:2000],
        "stderr": (proc.stderr or "").strip()[:2000],
    }


def _ok_data(resp: dict) -> dict:
    """Normalize a CLI envelope into a friendly result dict."""
    if resp.get("ok"):
        return {"status": "ok", "data": resp.get("data")}
    err = resp.get("error") or resp.get("stderr") or "unknown CLI error"
    if isinstance(err, dict):
        err = json.dumps(err, ensure_ascii=False)
    return {"status": "error", "error": str(err)[:2000]}


# ── Wallet management tools (agentic-wallet-skills) ────────────────

def wallet_status() -> dict:
    """Show current wallet status: email, login type, active account, policy."""
    return _ok_data(_run_cli(
        [ONCHAINOS_BIN, "wallet", "status"],
        timeout=30, label="wallet_status",
    ))


def wallet_addresses(chain: str = "") -> dict:
    """List wallet addresses grouped by chain category (XLayer, EVM, Solana).
    Optional chain filter: chain name or ID (e.g. "solana" or "501", "ethereum" or "1")."""
    args = [ONCHAINOS_BIN, "wallet", "addresses"]
    if chain:
        args += ["--chain", chain]
    return _ok_data(_run_cli(args, timeout=30, label="wallet_addresses"))


def wallet_balance(
    all_accounts: bool = False,
    chain: str = "",
    token_address: str = "",
    force: bool = False,
) -> dict:
    """Query wallet balances.
    - all_accounts: query all accounts' assets
    - chain: chain name/ID filter (requires --all or account)
    - token_address: filter by token contract address (requires --chain)
    - force: bypass caches and re-fetch from API
    """
    args = [ONCHAINOS_BIN, "wallet", "balance"]
    if all_accounts:
        args.append("--all")
    if chain:
        args += ["--chain", chain]
    if token_address:
        args += ["--token-address", token_address]
    if force:
        args.append("--force")
    return _ok_data(_run_cli(args, timeout=60, label="wallet_balance"))


def wallet_chains() -> dict:
    """List all supported chains (cached locally, refreshes every 10 minutes)."""
    return _ok_data(_run_cli(
        [ONCHAINOS_BIN, "wallet", "chains"],
        timeout=30, label="wallet_chains",
    ))


def wallet_history(
    chain: str = "",
    address: str = "",
    limit: str = "",
    page_num: str = "",
) -> dict:
    """Query wallet transaction history.
    Optional filters: chain (name/ID), address, limit (page size), page_num."""
    args = [ONCHAINOS_BIN, "wallet", "history"]
    if chain:
        args += ["--chain", chain]
    if address:
        args += ["--address", address]
    if limit:
        args += ["--limit", limit]
    if page_num:
        args += ["--page-num", page_num]
    return _ok_data(_run_cli(args, timeout=60, label="wallet_history"))


def wallet_add() -> dict:
    """Create a new sub-wallet account (up to 50 per wallet)."""
    return _ok_data(_run_cli(
        [ONCHAINOS_BIN, "wallet", "add"],
        timeout=60, label="wallet_add",
    ))


def wallet_switch(account_id: str) -> dict:
    """Switch the active wallet account."""
    if not account_id:
        return {"status": "error", "error": "account_id is required"}
    return _ok_data(_run_cli(
        [ONCHAINOS_BIN, "wallet", "switch", account_id],
        timeout=30, label="wallet_switch",
    ))


def wallet_send(
    chain: str,
    to_address: str,
    readable_amount: str,
    contract_token: str = "",
    from_address: str = "",
    force: bool = False,
) -> dict:
    """Transfer funds from the active account to an arbitrary address.

    TEE-signed and irreversible — the caller must confirm before invoking.
    `readable_amount` is a human-readable amount (CLI converts by token
    decimals). `contract_token` is the SPL/ERC-20 contract address for token
    transfers; omit it to send the chain's native coin.
    """
    if not chain or not to_address or not readable_amount:
        return {"status": "error", "error": "chain / to_address / readable_amount are required"}
    args = [
        ONCHAINOS_BIN, "wallet", "send",
        "--chain", chain,
        "--recipient", to_address,
        "--readable-amount", readable_amount,
    ]
    if contract_token:
        args += ["--contract-token", contract_token]
    if from_address:
        args += ["--from", from_address]
    if force:
        args.append("--force")
    return _ok_data(_run_cli(args, timeout=90, label="wallet_send"))


def get_active_wallet_address(chain: str = "solana") -> Optional[str]:
    """Resolve the active account's address for a chain (Agentic Wallet).

    The broadcast/quote address is the Agentic Wallet active account
    (selected account in keyring.enc) — NOT the user's personal wallet
    address. Returns None when not logged in or the chain has no address.
    """
    resp = wallet_addresses(chain)
    if resp.get("status") != "ok":
        return None
    data = resp.get("data") or {}
    group = (
        "solana" if chain in ("solana", "501")
        else "xlayer" if chain in ("xlayer", "196")
        else "evm"
    )
    addrs = data.get(group) or []
    if not addrs:
        return None
    return addrs[0].get("address") or None


def _get(d: dict, *keys, default=None):
    """Read a possibly-camelCase/snake_case key from a dict (first hit wins)."""
    for k in keys:
        if k in d:
            return d[k]
    return default


def wallet_accounts() -> dict:
    """List all sub-wallet accounts with their addresses.

    The onchainos CLI has no "list accounts" command, so this reads
    ~/.onchainos/wallets.json directly (same store the CLI uses; the
    symlink to /data/legion/credentials/onchainos_sessions survives
    Factory Rebuilds). Returns:
    {"status": "ok", "data": {"selected_account_id": ..., "accounts": [
        {"account_id", "account_name", "is_default", "is_active", "addresses": [
            {"chain", "chain_index", "address", "type"}]}]}}
    """
    _ensure_onchainos_dir()
    wallets_path = os.path.expanduser("~/.onchainos/wallets.json")
    if not os.path.exists(wallets_path):
        return {"status": "error", "error": "wallets.json 不存在 — 请先登录钱包"}
    try:
        with open(wallets_path, encoding="utf-8") as f:
            wallets = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"wallets.json 读取失败: {exc}"}

    selected = _get(wallets, "selected_account_id", "selectedAccountId", default="") or ""
    accounts = _get(wallets, "accounts", default=[]) or []
    accounts_map = _get(wallets, "accounts_map", "accountsMap", default={}) or {}

    result = []
    for acc in accounts:
        account_id = _get(acc, "account_id", "accountId", default="") or ""
        entry = accounts_map.get(account_id) or {}
        address_list = _get(entry, "address_list", "addressList", default=[]) or []
        addresses = []
        for addr in address_list:
            addresses.append({
                "chain": _get(addr, "chain_name", "chainName", default="") or "",
                "chain_index": _get(addr, "chain_index", "chainIndex", default="") or "",
                "address": _get(addr, "address", default="") or "",
                "type": _get(addr, "address_type", "addressType", default="") or "",
            })
        result.append({
            "account_id": account_id,
            "account_name": _get(acc, "account_name", "accountName", default=account_id) or account_id,
            "is_default": bool(_get(acc, "is_default", "isDefault", default=False)),
            "is_active": account_id == selected,
            "addresses": addresses,
        })

    return {
        "status": "ok",
        "data": {
            "selected_account_id": selected,
            "accounts": result,
        },
    }


def wallet_login_init() -> dict:
    """Initiate onchainos social login. Returns loginUrl for the user.
    Tries multiple CLI syntaxes for cross-version compatibility."""
    candidates = [
        [ONCHAINOS_BIN, "wallet", "login"],
        [ONCHAINOS_BIN, "wallet", "login", "--phase", "init"],
    ]

    for attempt, args in enumerate(candidates, 1):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}
        except subprocess.TimeoutExpired:
            return {"error": f"onchainos wallet login attempt {attempt} timed out"}

        print(
            f"[DIAG] wallet_login_init attempt {attempt}: {args[1:]!r} "
            f"rc={proc.returncode} "
            f"stdout={proc.stdout.strip()[:200]!r} "
            f"stderr={proc.stderr.strip()[:200]!r}",
            file=sys.stderr, flush=True,
        )

        if proc.returncode != 0:
            continue

        for source, label in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
            text = source.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            inner = data.get("data", data) if isinstance(data.get("data"), dict) else data
            login_url = inner.get("loginUrl", "")
            if login_url:
                print(
                    f"[DIAG] wallet_login_init SUCCESS ({label}): session={inner.get('authSessionId','?')[:12]}...",
                    file=sys.stderr, flush=True,
                )
                return {
                    "login_url": login_url,
                    "auth_session_id": inner.get("authSessionId", ""),
                    "opened": inner.get("opened", False),
                }

    return {
        "error": "all wallet login attempts failed",
        "details": "No loginUrl returned from onchainos wallet login",
    }


def wallet_login_poll(session_id: str = "") -> dict:
    """Poll for social login completion. Provide session_id from wallet_login_init result.
    If session_id is empty, auto-detects the most recent session."""
    candidates: list[list[str]] = []
    base_args = [ONCHAINOS_BIN, "wallet", "login", "--phase", "poll"]
    if session_id:
        base_args.extend(["--session-id", session_id])
    candidates.append(base_args)

    for attempt, args in enumerate(candidates, 1):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            return {"status": "pending", "message": "Still waiting for browser authorization (retry)"}

        print(
            f"[DIAG] wallet_login_poll attempt {attempt}: {args[1:]!r} "
            f"rc={proc.returncode} "
            f"stdout={proc.stdout.strip()[:300]!r} "
            f"stderr={proc.stderr.strip()[:300]!r}",
            file=sys.stderr, flush=True,
        )

        if proc.returncode != 0:
            combined = (proc.stderr + proc.stdout).lower()
            if "10018" in combined or "not ready" in combined or "login timed out" in combined:
                return {"status": "pending", "message": "User has not completed login yet — finish in browser then retry"}
            if "timed out" in combined or "timeout" in combined:
                return {"status": "timeout", "message": (proc.stderr + proc.stdout).strip()[:500]}
            continue

        for source, label in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
            text = source.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            # OKX CLI convention: {"ok": true, "data": {...}}
            if data.get("ok"):
                print("[DIAG] wallet_login_poll SUCCESS (ok=true)", file=sys.stderr, flush=True)
                return {"status": "logged_in", "message": "Wallet login completed"}

            inner = data.get("data", data) if isinstance(data.get("data"), dict) else data
            if inner.get("ok") or inner.get("success", False) or inner.get("accessToken"):
                print("[DIAG] wallet_login_poll SUCCESS (inner)", file=sys.stderr, flush=True)
                return {"status": "logged_in", "message": "Wallet login completed"}

    return {
        "status": "error",
        "message": f"all poll attempts failed (tried {len(candidates)} syntaxes — check stderr for DIAG lines)",
    }


def wallet_payment_set(tier: str, asset: str = "", chain: str = "", name: str = "") -> dict:
    """Set default payment asset and tier. Uses known USDG on X Layer as defaults."""
    if not asset:
        asset = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"  # USDG
    if not chain:
        chain = "196"  # X Layer
    if not name:
        name = "USDG"

    args = [
        ONCHAINOS_BIN, "payment", "default", "set",
        "--asset", asset,
        "--chain", chain,
        "--tier", tier,
        "--name", name,
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"error": "payment default set timed out"}
    except FileNotFoundError:
        return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}

    print(
        f"[DIAG] wallet_payment_set tier={tier} rc={proc.returncode} "
        f"stdout={proc.stdout.strip()[:300]!r} "
        f"stderr={proc.stderr.strip()[:300]!r}",
        file=sys.stderr, flush=True,
    )

    if proc.returncode != 0:
        return {
            "error": f"payment default set failed (rc={proc.returncode})",
            "stdout": proc.stdout.strip()[:1000],
            "stderr": proc.stderr.strip()[:1000],
        }

    return {
        "status": "ok",
        "tier": tier,
        "asset": asset,
        "chain": chain,
        "name": name,
    }


def wallet_login_raw_diag() -> dict:
    """Run onchainos wallet login --phase poll and return raw output for debugging."""
    results = []
    for args in [
        [ONCHAINOS_BIN, "wallet", "login", "--phase", "poll"],
    ]:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            results.append({"args": args, "error": "timeout (10s)"})
            continue
        results.append({
            "args": args,
            "rc": proc.returncode,
            "stdout": proc.stdout.strip()[:2000],
            "stderr": proc.stderr.strip()[:2000],
        })
    return {"results": results}


def wallet_login_status() -> dict:
    """Check onchainos login / payment status without side effects."""
    status = {"logged_in": False, "payment_basic": False, "payment_premium": False}

    session_path = os.path.expanduser("~/.onchainos/session.json")
    if os.path.isfile(session_path):
        status["logged_in"] = True

    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "get", "--tier", "basic"],
            capture_output=True, text=True, timeout=5,
        )
        status["payment_basic"] = proc.returncode == 0
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "get", "--tier", "premium"],
            capture_output=True, text=True, timeout=5,
        )
        status["payment_premium"] = proc.returncode == 0
    except Exception:
        pass

    return status


def _read_saved_session_id() -> str:
    """Read authSessionId persisted from a previous init call."""
    try:
        if os.path.isfile(_SESSION_ID_FILE):
            with open(_SESSION_ID_FILE) as f:
                return f.read().strip()
    except OSError:
        pass
    return ""


def _save_session_id(session_id: str) -> None:
    """Persist authSessionId so the next call can poll with it."""
    if not session_id:
        return
    try:
        os.makedirs(os.path.dirname(_SESSION_ID_FILE), exist_ok=True)
        with open(_SESSION_ID_FILE, "w") as f:
            f.write(session_id)
    except OSError:
        pass


def _clear_saved_session_id() -> None:
    """Remove the persisted session id after a successful poll."""
    try:
        os.remove(_SESSION_ID_FILE)
    except (OSError, FileNotFoundError):
        pass


def wallet_setup() -> dict:
    """One-shot onchainos wallet bootstrap.

    Call this repeatedly until it returns phase="done".
    Follows the official OKX flow: init → save session_id → user authorizes
    in browser → poll with --session-id → payment setup.

    The session_id is persisted to ~/.onchainos/last_session_id.txt so
    subsequent calls (even after MCP restarts) can pick up the pending
    authorization without re-init (which would generate a fresh, different
    session and invalidate the one the user just authorized).
    """
    _ensure_onchainos_dir()  # persistent symlink survives Factory Rebuilds
    status = wallet_login_status()

    if status["logged_in"] and status["payment_basic"] and status["payment_premium"]:
        _clear_saved_session_id()
        return {"phase": "done", "message": "Wallet already logged in and payment tiers set."}

    if not status["logged_in"]:
        # Try to continue a previous init: poll with the saved session_id
        session_id = _read_saved_session_id()
        if session_id:
            print(
                f"[DIAG] wallet_setup: found saved session_id={session_id[:16]}..., trying poll",
                file=sys.stderr, flush=True,
            )
            poll_result = wallet_login_poll(session_id=session_id)
            if poll_result.get("status") == "logged_in":
                # Poll succeeded — login complete, remove saved file and proceed
                print(
                    "[DIAG] wallet_setup: poll succeeded, clearing saved session_id",
                    file=sys.stderr, flush=True,
                )
                _clear_saved_session_id()
                # Re-check status (keyring should be written now)
                status = wallet_login_status()
            elif poll_result.get("status") in ("pending",):
                # Not yet authorized, return pending (don't re-init!)
                return {
                    "phase": "polling",
                    "session_id": session_id,
                    "message": poll_result.get(
                        "message", "Waiting for browser authorization..."
                    ),
                }
            else:
                # Poll failed (expired, invalid). Clear and start fresh.
                print(
                    f"[DIAG] wallet_setup: poll failed ({poll_result.get('status')!r}), clearing saved session_id",
                    file=sys.stderr, flush=True,
                )
                _clear_saved_session_id()
                session_id = ""

        # No valid session — start a new one
        if not session_id:
            init_result = wallet_login_init()
            if init_result.get("login_url"):
                sid = init_result.get("auth_session_id", "")
                if sid:
                    _save_session_id(sid)
                    print(
                        f"[DIAG] wallet_setup: init new session_id={sid[:16]}..., saved to file",
                        file=sys.stderr, flush=True,
                    )
                return {
                    "phase": "login_required",
                    "login_url": init_result["login_url"],
                    "auth_session_id": sid,
                    "message": (
                        "Open the login_url in browser, complete authorization, "
                        "then call wallet_setup() again."
                    ),
                }
            return {"phase": "error", "error": init_result.get("error", "init failed")}

    # Logged in but payment not set
    results = {}
    for tier in ["basic", "premium"]:
        if not status.get(f"payment_{tier}"):
            results[tier] = wallet_payment_set(tier=tier)
        else:
            results[tier] = {"status": "already_set"}

    return {
        "phase": "done",
        "message": "Wallet login and payment tiers configured.",
        "payment_results": results,
    }
