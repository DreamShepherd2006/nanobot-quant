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
import json
import os
import re
import time
import uuid

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .onchainos_cli import normalize_symbol
from .token_handlers import _read_tokens
from .tools.tools_wallet import (
    wallet_accounts,
    wallet_add,
    wallet_addresses,
    wallet_balance,
    wallet_chains,
    wallet_history,
    wallet_login_init,
    wallet_login_poll,
    wallet_login_status,
    wallet_send,
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


def _merge_tracked_tokens(bal_res: dict, tokens: list[dict]) -> dict:
    """Append user-registered tokens (tokens.json) to balance assets.

    `wallet balance` returns every non-zero asset of the active account, so a
    tracked token that is missing from the response has a zero balance — we
    still show it (marked ``tracked``) so users always see the tokens they
    care about, even at 0. Pure function, unit-tested.
    """
    if bal_res.get("status") != "ok" or not isinstance(bal_res.get("data"), dict):
        return bal_res
    data = bal_res["data"]
    assets = data.get("assets") or data.get("balances") or []
    if not isinstance(assets, list):
        assets = []
    known = set()
    for a in assets:
        if isinstance(a, dict):
            sym = normalize_symbol(a.get("symbol") or a.get("token") or a.get("tokenSymbol") or "")
            if sym:
                known.add(sym)
    for t in tokens:
        sym = normalize_symbol(t.get("symbol", ""))
        if not sym or sym in known:
            continue
        assets.append({
            "symbol": sym,
            "amount": "0",
            "tracked": True,
            "chain": str(t.get("chain") or "solana"),
            "address": str(t.get("address") or ""),
        })
        known.add(sym)
    data["assets"] = assets
    return bal_res


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

    # ── Address book (persisted to {data_root}/credentials/address_book.json) ──
    # Transfer targets must be pre-registered here; the send endpoint refuses
    # any address that is not in the book (fail-closed by design).

    def _address_book_path() -> str:
        return os.path.join(gatekeeper._platform.data_root, "credentials", "address_book.json")

    def _load_address_book() -> dict:
        try:
            with open(_address_book_path(), encoding="utf-8") as f:
                book = json.load(f)
            if isinstance(book, dict) and isinstance(book.get("addresses"), list):
                return book
        except (OSError, json.JSONDecodeError):
            pass
        return {"addresses": [], "max_amount": None}

    def _save_address_book(book: dict) -> None:
        path = _address_book_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _is_valid_address(chain: str, address: str) -> bool:
        address = address.strip()
        if chain in ("solana", "501"):
            return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address))
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", address))

    # Pending transfer requests (backend two-step confirmation gate).
    # tx_id is server-generated, single-use, and expires after 30 seconds —
    # a client cannot transfer funds with a single API call.
    _pending_sends: dict[str, dict] = {}
    _SEND_TTL = 30.0

    async def _wallet_page(request: Request):
        _u = request.session.get("user")
        if not _u:
            return RedirectResponse("/")
        if not gatekeeper._platform.is_commander(_u):
            return HTMLResponse(
                "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 仅 Commander 可访问</h3>",
                status_code=403,
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

        status_res, login_res, addr_res, bal_res, hist_res, accounts_res, chains_res = await asyncio.gather(
            _call(wallet_status, timeout=25),
            _call(wallet_login_status, timeout=10),
            _call(wallet_addresses, timeout=25),
            _call(wallet_balance, timeout=30),
            _call(wallet_history, limit="10", timeout=30),
            _call(wallet_accounts, timeout=10),
            _call(wallet_chains, timeout=30),
        )
        # Merge user-registered tokens (tokens.json) into the balance view so
        # tracked tokens show even with zero balance. wallet balance returns
        # all non-zero assets, so a tracked token missing from the response
        # means its balance is 0 (displayed as "0" with a 🪙 marker).
        bal_res = _merge_tracked_tokens(bal_res, _read_tokens())
        return JSONResponse({
            "ok": True,
            "status": status_res,
            "login": login_res,
            "addresses": addr_res,
            "balance": bal_res,
            "history": hist_res,
            "accounts": accounts_res,
            "chains": chains_res,
            "address_book": _load_address_book(),
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

    # ── Transfer (two-step backend confirmation) ────────────────────────

    async def _wallet_send(request: Request) -> JSONResponse:
        """POST /config/wallet/send — validate + create a pending transfer.

        Body: {chain, to_address, amount, token_address?}
        Returns {tx_id, preview}; nothing is executed until /send/confirm.
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        chain = str(body.get("chain", "")).strip().lower()
        to_address = str(body.get("to_address", "")).strip()
        amount = str(body.get("amount", "")).strip()
        token_address = str(body.get("token_address", "")).strip()

        if not chain or not to_address or not amount:
            return JSONResponse({"ok": False, "error": "chain / to_address / amount 必填"}, status_code=400)
        if not _is_valid_address(chain, to_address):
            return JSONResponse({"ok": False, "error": "目标地址格式无效"}, status_code=400)

        book = _load_address_book()
        if not any(
            e.get("chain") == chain and e.get("address") == to_address
            for e in book.get("addresses", [])
        ):
            return JSONResponse(
                {"ok": False, "error": "目标地址不在地址簿中 — 请先在「地址簿」添加"}, status_code=400,
            )

        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "金额无效"}, status_code=400)
        if amt <= 0:
            return JSONResponse({"ok": False, "error": "金额必须大于 0"}, status_code=400)
        max_amount = book.get("max_amount")
        if max_amount is not None:
            try:
                if amt > float(max_amount):
                    return JSONResponse(
                        {"ok": False, "error": f"超过单笔限额 {max_amount}"}, status_code=400,
                    )
            except (TypeError, ValueError):
                pass

        # Chain support check — if the chain list is unavailable, allow (non-blocking).
        chains_res = await _call(wallet_chains, timeout=30)
        if chains_res.get("status") == "ok":
            names = {
                str(c.get("chainName") or c.get("chain_name") or "").lower()
                for c in (chains_res.get("data") or [])
                if isinstance(c, dict)
            }
            if names and chain not in names:
                return JSONResponse({"ok": False, "error": f"不支持的链: {chain}"}, status_code=400)

        tx_id = uuid.uuid4().hex
        _pending_sends[tx_id] = {
            "payload": {
                "chain": chain,
                "to_address": to_address,
                "amount": amount,
                "token_address": token_address,
            },
            "expires": time.time() + _SEND_TTL,
        }
        return JSONResponse({
            "ok": True,
            "tx_id": tx_id,
            "preview": {
                "chain": chain,
                "to_address": to_address,
                "amount": amount,
                "token": token_address or None,
            },
        })

    async def _wallet_send_confirm(request: Request) -> JSONResponse:
        """POST /config/wallet/send/confirm — execute a pending transfer.

        Body: {tx_id}
        The pending request is single-use and expires 30s after creation.
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        tx_id = str(body.get("tx_id", "")).strip()
        pending = _pending_sends.pop(tx_id, None) if tx_id else None
        if not pending:
            return JSONResponse(
                {"ok": False, "error": "转账请求不存在、已使用或已过期，请重新发起"}, status_code=400,
            )
        if time.time() > pending["expires"]:
            return JSONResponse({"ok": False, "error": "转账请求已过期，请重新发起"}, status_code=400)

        p = pending["payload"]
        result = await _call(
            wallet_send,
            p["chain"], p["to_address"], p["amount"],
            p.get("token_address") or "",
            timeout=90,
        )
        return JSONResponse({"ok": result.get("status") == "ok", **result})

    # ── Address book management ─────────────────────────────────────────

    async def _address_book_add(request: Request) -> JSONResponse:
        """POST /config/wallet/address-book/add {name, chain, address}"""
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        name = str(body.get("name", "")).strip()
        chain = str(body.get("chain", "")).strip().lower()
        address = str(body.get("address", "")).strip()
        if not name or not chain or not address:
            return JSONResponse({"ok": False, "error": "name / chain / address 必填"}, status_code=400)
        if not _is_valid_address(chain, address):
            return JSONResponse({"ok": False, "error": "地址格式无效"}, status_code=400)

        book = _load_address_book()
        if any(
            e.get("chain") == chain and e.get("address") == address
            for e in book.get("addresses", [])
        ):
            return JSONResponse({"ok": False, "error": "该地址已存在"}, status_code=400)
        book.setdefault("addresses", []).append(
            {"id": uuid.uuid4().hex, "name": name, "chain": chain, "address": address}
        )
        _save_address_book(book)
        return JSONResponse({"ok": True, "address_book": book})

    async def _address_book_remove(request: Request) -> JSONResponse:
        """POST /config/wallet/address-book/remove {id}"""
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        entry_id = str(body.get("id", "")).strip()
        if not entry_id:
            return JSONResponse({"ok": False, "error": "id 必填"}, status_code=400)
        book = _load_address_book()
        before = len(book.get("addresses", []))
        book["addresses"] = [e for e in book.get("addresses", []) if e.get("id") != entry_id]
        if len(book["addresses"]) == before:
            return JSONResponse({"ok": False, "error": "地址不存在"}, status_code=404)
        _save_address_book(book)
        return JSONResponse({"ok": True, "address_book": book})

    async def _address_book_limit(request: Request) -> JSONResponse:
        """POST /config/wallet/address-book/limit {max_amount: number|null}

        Optional per-transfer cap in amount units (token-agnostic).
        Pass null to disable.
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        raw = body.get("max_amount")
        book = _load_address_book()
        if raw is None or raw == "":
            book["max_amount"] = None
        else:
            try:
                limit = float(raw)
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "限额必须是数字或空"}, status_code=400)
            if limit <= 0:
                return JSONResponse({"ok": False, "error": "限额必须大于 0"}, status_code=400)
            book["max_amount"] = limit
        _save_address_book(book)
        return JSONResponse({"ok": True, "address_book": book})

    app.add_route("/config/wallet", _wallet_page, methods=["GET"])
    app.add_route("/config/wallet/data", _wallet_data, methods=["GET"])
    app.add_route("/config/wallet/login", _wallet_login, methods=["POST"])
    app.add_route("/config/wallet/add", _wallet_add, methods=["POST"])
    app.add_route("/config/wallet/switch", _wallet_switch, methods=["POST"])
    app.add_route("/config/wallet/send", _wallet_send, methods=["POST"])
    app.add_route("/config/wallet/send/confirm", _wallet_send_confirm, methods=["POST"])
    app.add_route("/config/wallet/address-book/add", _address_book_add, methods=["POST"])
    app.add_route("/config/wallet/address-book/remove", _address_book_remove, methods=["POST"])
    app.add_route("/config/wallet/address-book/limit", _address_book_limit, methods=["POST"])
