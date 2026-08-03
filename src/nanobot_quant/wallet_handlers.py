"""Wallet management WebUI — /config/wallet page + operation endpoints.

Rendered inside the Legion business-management chat. Displays onchainos
wallet status, addresses, balances, payment tiers and recent transactions,
with refresh / login / add / switch operations.

Reuses the CLI wrappers in tools_wallet.py (which restore the ~/.onchainos
persistent symlink before every call, so the page survives Factory Rebuilds).

Follows the closure pattern of mode_handlers.py / live_handlers.py:
handlers capture `gatekeeper` from register_wallet_routes().
"""

from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .tools.tools_wallet import (
    wallet_add,
    wallet_addresses,
    wallet_balance,
    wallet_history,
    wallet_login_init,
    wallet_login_poll,
    wallet_login_status,
    wallet_status,
    wallet_switch,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(filename: str) -> str:
    with open(os.path.join(_HERE, filename), encoding="utf-8") as f:
        return f.read()


_WALLET_PAGE = _load_template("wallet_page.html")


# ── CLI call helpers (concurrent, timeout-guarded) ────────────────


async def _call(fn, *args, timeout: float = 25.0, **kwargs):
    """Run a sync wallet CLI wrapper in a thread, bounded by a timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs), timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"status": "error", "error": f"{getattr(fn, '__name__', fn)} timed out"}
    except Exception as exc:  # noqa: BLE001 — CLI errors surface to the page
        return {"status": "error", "error": f"{getattr(fn, '__name__', fn)}: {exc}"}


# ── Route registration helper (closure pattern, mirrors live_handlers) ──


def register_wallet_routes(app, gatekeeper) -> None:
    """Register wallet management routes on the FastAPI app.

    Called by gatekeeper_routes.py during app creation (nanobot-quant
    plugin hook, mirroring mode_handlers / live_handlers).
    """

    def _guard(user):
        """Return None if allowed, else (status, body) tuple."""
        if not user:
            return (401, "请先登录")
        if not gatekeeper._platform.is_commander(user):
            return (403, "仅 Commander 可访问")
        return None

    async def _wallet_page(request: Request) -> HTMLResponse:
        denied = _guard(request.session.get("user"))
        if denied:
            return HTMLResponse(
                f"<h3 style='text-align:center;margin-top:60px;color:#888;'>{denied[1]}</h3>",
                status_code=denied[0],
            )
        return HTMLResponse(_WALLET_PAGE)

    async def _wallet_data(request: Request) -> JSONResponse:
        """GET /config/wallet/data — aggregate wallet state for the page.

        Runs the CLI wrappers concurrently; each result is independently
        guarded so one slow/failed call does not stall the whole page.
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])

        status_res, login_res, addr_res, bal_res, hist_res = await asyncio.gather(
            _call(wallet_status, timeout=25),
            _call(wallet_login_status, timeout=10),
            _call(wallet_addresses, timeout=25),
            _call(wallet_balance, timeout=30),
            _call(wallet_history, limit="10", timeout=30),
        )
        return JSONResponse({
            "ok": True,
            "status": status_res,
            "login": login_res,
            "addresses": addr_res,
            "balance": bal_res,
            "history": hist_res,
        })

    async def _wallet_login(request: Request) -> JSONResponse:
        """POST /config/wallet/login — start onchainos social login.

        Body: {phase: "init" | "poll", session_id?: str}
        Returns the login URL (init) or poll result (poll).
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        phase = body.get("phase", "init")
        if phase == "poll":
            result = await _call(wallet_login_poll, body.get("session_id", ""), timeout=15)
            return JSONResponse({"ok": result.get("status") == "logged_in", **result})
        result = await _call(wallet_login_init, timeout=30)
        if result.get("login_url"):
            return JSONResponse({"ok": True, "login_url": result["login_url"],
                                 "auth_session_id": result.get("auth_session_id", "")})
        return JSONResponse({"ok": False, "error": result.get("error", "登录初始化失败")})

    async def _wallet_add(request: Request) -> JSONResponse:
        """POST /config/wallet/add — create a new sub-wallet account."""
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        result = await _call(wallet_add, timeout=60)
        return JSONResponse({"ok": result.get("status") == "ok", **result})

    async def _wallet_switch(request: Request) -> JSONResponse:
        """POST /config/wallet/switch — switch active account.

        Body: {account_id: "..."}
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        account_id = str(body.get("account_id", "")).strip()
        if not account_id:
            return JSONResponse({"ok": False, "error": "account_id 必填"}, status_code=400)
        result = await _call(wallet_switch, account_id, timeout=30)
        return JSONResponse({"ok": result.get("status") == "ok", **result})

    app.add_route("/config/wallet", _wallet_page, methods=["GET"])
    app.add_route("/config/wallet/data", _wallet_data, methods=["GET"])
    app.add_route("/config/wallet/login", _wallet_login, methods=["POST"])
    app.add_route("/config/wallet/add", _wallet_add, methods=["POST"])
    app.add_route("/config/wallet/switch", _wallet_switch, methods=["POST"])
