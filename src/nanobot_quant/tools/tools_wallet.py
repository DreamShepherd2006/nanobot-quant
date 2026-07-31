"""Wallet tools: onchainos login, payment, status.

Consolidated wallet bootstrap: wallet_setup() is the main idempotent entry point.
Individual tools (wallet_login_init/poll/payment_set) are retained for debugging.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

ONCHAINOS_BIN = "/usr/local/bin/onchainos"
_SESSION_ID_FILE = os.path.expanduser("~/.onchainos/last_session_id.txt")


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
