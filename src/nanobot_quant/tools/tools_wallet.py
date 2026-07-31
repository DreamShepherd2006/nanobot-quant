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


def wallet_login_init() -> dict:
    """Initiate onchainos social login. Returns loginUrl for the user.
    Tries multiple CLI syntaxes for cross-version compatibility."""
    candidates = [
        [ONCHAINOS_BIN, "wallet", "login"],
        [ONCHAINOS_BIN, "wallet", "login", "--phase", "init"],
        [ONCHAINOS_BIN, "wallet", "login", "init"],
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

    alt_args: list[str] = [ONCHAINOS_BIN, "wallet", "login", "poll"]
    if session_id:
        alt_args.append(session_id)
    candidates.append(alt_args)

    for attempt, args in enumerate(candidates, 1):
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=310,
            )
        except subprocess.TimeoutExpired:
            return {"error": "onchainos wallet login poll timed out (310s)"}

        print(
            f"[DIAG] wallet_login_poll attempt {attempt}: {args[1:]!r} "
            f"rc={proc.returncode} "
            f"stdout={proc.stdout.strip()[:300]!r} "
            f"stderr={proc.stderr.strip()[:300]!r}",
            file=sys.stderr, flush=True,
        )

        if proc.returncode != 0:
            if "10018" in proc.stderr or "not ready" in proc.stderr.lower():
                return {"status": "pending", "message": "User has not completed login yet"}
            if "timed out" in proc.stderr.lower() or "timeout" in proc.stderr.lower():
                return {"status": "timeout", "message": proc.stderr.strip()[:500]}
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
            if inner.get("success", False) or inner.get("accessToken"):
                print("[DIAG] wallet_login_poll SUCCESS", file=sys.stderr, flush=True)
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
            args, capture_output=True, text=True, timeout=30,
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
        [ONCHAINOS_BIN, "wallet", "login", "poll"],
    ]:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=310)
        except subprocess.TimeoutExpired:
            results.append({"args": args, "error": "timeout"})
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
            capture_output=True, text=True, timeout=15,
        )
        status["payment_basic"] = proc.returncode == 0
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "get", "--tier", "premium"],
            capture_output=True, text=True, timeout=15,
        )
        status["payment_premium"] = proc.returncode == 0
    except Exception:
        pass

    return status


def wallet_setup() -> dict:
    """One-shot onchainos wallet bootstrap.

    Call this repeatedly until it returns phase="done".
    - Try poll first: if there's a pending auth session, wait for user.
    - If no pending session: init → return login_url for user.
    - When fully done: returns phase="done".
    """
    status = wallet_login_status()

    if status["logged_in"] and status["payment_basic"] and status["payment_premium"]:
        return {"phase": "done", "message": "Wallet already logged in and payment tiers set."}

    if not status["logged_in"]:
        # Try poll first — user may have authorized since last init
        poll_result = wallet_login_poll()
        if poll_result.get("status") == "logged_in":
            # Poll succeeded, proceed to payment setup below
            pass
        elif poll_result.get("status") in ("pending", "timeout"):
            # User hasn't authorized yet, keep waiting (don't re-init)
            return {
                "phase": "polling",
                "poll_status": poll_result["status"],
                "message": poll_result.get("message", "Waiting for browser authorization..."),
            }
        else:
            # No pending session — start a new one
            init_result = wallet_login_init()
            if init_result.get("login_url"):
                return {
                    "phase": "login_required",
                    "login_url": init_result["login_url"],
                    "auth_session_id": init_result.get("auth_session_id", ""),
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
