"""Gate CEX account management handlers — main/sub-account cards, slot map,
spot balances and main→sub transfer (two-step confirm, mirrors DEX wallet send).

Used by gatekeeper to register /config/gate routes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .gate_credentials import (
    load_gate_credentials,
    load_slot_map,
    fetch_all_balances,
    sub_account_transfer,
)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PAGE_HTML = open(os.path.join(_HERE, "gate_page.html"), encoding="utf-8").read()

# ── Transfer two-step confirm state (in-memory, 30s expiry) ───────
# {tx_id: {"sub": str, "amount": str, "currency": str, "expires_at": float}}
_TRANSFER_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()
_TRANSFER_TTL = 30.0


def _expire_pending() -> None:
    now = time.time()
    for tx_id in [k for k, v in _TRANSFER_PENDING.items() if v["expires_at"] < now]:
        _TRANSFER_PENDING.pop(tx_id, None)


# ── Page / data handlers ─────────────────────────────────────────


async def gate_page(request: Request) -> HTMLResponse:
    """GET /config/gate — account management page."""
    return HTMLResponse(_PAGE_HTML)


def _format_balances(bal: dict) -> dict:
    """Shape balances for display: total USDT available + non-zero holdings."""
    if "__error" in bal:
        return {"error": bal["__error"], "usdt": None, "holdings": []}
    usdt = bal.get("USDT", {}).get("available", 0) or 0
    holdings = sorted(
        (
            {"currency": c, "available": v.get("available", 0), "locked": v.get("locked", 0)}
            for c, v in bal.items()
            if c != "USDT" and ((v.get("available") or 0) > 0 or (v.get("locked") or 0) > 0)
        ),
        key=lambda x: x["available"] + x["locked"],
        reverse=True,
    )
    return {"error": None, "usdt": round(usdt, 6), "holdings": holdings}


async def gate_data(request: Request) -> JSONResponse:
    """GET /config/gate/data — aggregated account snapshot for the page."""
    creds = load_gate_credentials()
    if not creds:
        return JSONResponse({"ok": False, "error": "gate.json 未配置（/config/credentials/gate 录入）"})
    main = creds.get("main") or {}
    subs = creds.get("sub_accounts") or {}
    slot_map = load_slot_map(creds)

    balances = fetch_all_balances(creds)
    main_bal = balances.get("main", {"__error": "无数据"}) if isinstance(balances, dict) else {"__error": "无数据"}
    sub_rows = balances.get("sub_accounts", []) if isinstance(balances, dict) else []
    if isinstance(sub_rows, dict):  # whole-list failure → {"__error": ...}
        sub_by_uid: dict = {}
        sub_err = sub_rows.get("__error", "查询失败")
    else:
        sub_by_uid = {str(r.get("uid")): r.get("balances") or {} for r in sub_rows if r.get("uid")}
        sub_err = None

    def _account_card(name: str, sa: dict, slot: str | None, bal: dict) -> dict:
        return {
            "name": name,
            "uid": sa.get("uid", ""),
            "slot": slot,
            # Balance + transfer both run on the main key — sub cards only need
            # a UID for name↔balance matching, not a sub-account key.
            "configured": bool(sa.get("uid")),
            "balances": _format_balances(bal),
        }

    cards = [_account_card("main", main, None, main_bal)]
    for slot in sorted(slot_map, key=int):
        name = slot_map[slot]
        sa = subs.get(name, {})
        uid = str(sa.get("uid") or "")
        if uid and uid in sub_by_uid:
            bal = sub_by_uid[uid]
        elif sub_err:
            bal = {"__error": sub_err}
        elif not uid:
            bal = {"__error": "UID 未配置（/config/credentials/gate 录入）"}
        else:
            bal = {"__error": f"未匹配到 UID {uid} 的余额"}
        cards.append(_account_card(name, sa, slot, bal))

    return JSONResponse(
        {
            "ok": True,
            "main_uid": main.get("uid", ""),
            "slot_map": slot_map,
            "accounts": cards,
            "subs_configured": sum(1 for n in subs if subs[n].get("uid")),
            "subs_total": len(slot_map),
        }
    )


async def gate_transfer(request: Request) -> JSONResponse:
    """POST /config/gate/transfer — step 1: create one-time tx_id (30s TTL)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
    sub = str(body.get("sub") or "").strip()
    amount = str(body.get("amount") or "").strip()
    currency = str(body.get("currency") or "USDT").strip().upper() or "USDT"
    if not sub or not amount:
        return JSONResponse({"ok": False, "error": "缺少目标子账号或金额"}, status_code=400)
    try:
        amt = float(amount)
    except ValueError:
        return JSONResponse({"ok": False, "error": "金额必须是数字"}, status_code=400)
    if amt <= 0:
        return JSONResponse({"ok": False, "error": "金额必须大于 0"}, status_code=400)

    creds = load_gate_credentials()
    if not creds:
        return JSONResponse({"ok": False, "error": "gate.json 未配置"}, status_code=400)
    slot_map = load_slot_map(creds)
    if sub not in slot_map.values():
        return JSONResponse(
            {"ok": False, "error": f"目标必须是子账号（{sorted(slot_map.values())}）"},
            status_code=400,
        )
    subs = creds.get("sub_accounts") or {}
    if not (subs.get(sub) or {}).get("uid"):
        return JSONResponse({"ok": False, "error": f"子账号 {sub} 未配置 UID"}, status_code=400)

    tx_id = secrets.token_urlsafe(8)
    with _PENDING_LOCK:
        _expire_pending()
        _TRANSFER_PENDING[tx_id] = {
            "sub": sub,
            "amount": f"{amt:.8f}".rstrip("0").rstrip("."),
            "currency": currency,
            "expires_at": time.time() + _TRANSFER_TTL,
        }
    return JSONResponse(
        {
            "ok": True,
            "tx_id": tx_id,
            "ttl": _TRANSFER_TTL,
            "summary": f"主账号 → {sub} {amt} {currency}",
        }
    )


async def gate_transfer_confirm(request: Request) -> JSONResponse:
    """POST /config/gate/transfer/confirm — step 2: execute the transfer."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
    tx_id = str(body.get("tx_id") or "").strip()
    if not tx_id:
        return JSONResponse({"ok": False, "error": "缺少 tx_id"}, status_code=400)
    with _PENDING_LOCK:
        _expire_pending()
        pending = _TRANSFER_PENDING.pop(tx_id, None)
    if not pending:
        return JSONResponse(
            {"ok": False, "error": "确认已过期（30 秒内未确认）或不存在，请重新发起"},
            status_code=400,
        )
    try:
        result = sub_account_transfer(
            amount=pending["amount"],
            target_sub=pending["sub"],
            currency=pending["currency"],
        )
    except Exception as exc:  # noqa: BLE001 — surface gate API errors
        return JSONResponse({"ok": False, "error": f"划转失败: {exc}"}, status_code=502)
    return JSONResponse(
        {
            "ok": True,
            "result": result,
            "summary": f"主账号 → {pending['sub']} {pending['amount']} {pending['currency']}",
        }
    )


async def sync_subaccounts(request: Request) -> JSONResponse:
    """POST /config/gate/sync-subaccounts — discover sub-account UIDs from Gate.

    Queries ``/wallet/sub_account_balances`` with the main key (needs the
    '子账号' permission) and returns newly discovered sub-accounts with default
    names assigned — WITHOUT writing gate.json. The WebUI credential form is
    filled with the returned rows and the user confirms via save (decision
    2026-08-22: sync fills the form only, save persists).
    """
    creds = load_gate_credentials()
    if not creds:
        return JSONResponse(
            {"ok": False, "error": "gate.json 未配置（/config/credentials/gate 录入）"}
        )
    main = creds.get("main") or {}
    if not main.get("api_key") or not main.get("api_secret"):
        return JSONResponse(
            {"ok": False, "error": "主账号 API Key/Secret 未配置（先录入主账号）"},
            status_code=400,
        )

    from .gate_sdk import sub_account_balances as _list_sub_accounts

    try:
        rows = _list_sub_accounts(main["api_key"], main["api_secret"])
    except Exception as exc:  # noqa: BLE001 — surface Gate API errors
        return JSONResponse(
            {"ok": False, "error": f"查询失败（主 key 需开启「子账号」权限）: {exc}"},
            status_code=502,
        )

    uids = [str(r.get("uid") or "") for r in rows or [] if r.get("uid")]
    subs = creds.get("sub_accounts") or {}
    existing_uids = {str(s.get("uid")) for s in subs.values() if s.get("uid")}
    max_subs = int(creds.get("max_sub_accounts") or 10)
    new_uids = [u for u in uids if u not in existing_uids]
    if len(existing_uids) + len(new_uids) > max_subs:
        return JSONResponse(
            {
                "ok": False,
                "error": f"子账号数将超过上限 {max_subs}（可先在表单修改「子账号上限」）",
            },
            status_code=400,
        )

    used_nums: set[int] = set()
    for name in subs:
        m = re.search(r"(\d+)$", name)
        if m:
            used_nums.add(int(m.group(1)))
    added: list[dict] = []
    for uid in new_uids:
        n = 1
        while n in used_nums:
            n += 1
        used_nums.add(n)
        added.append({"name": f"gate_bot{n}", "uid": uid})
    return JSONResponse({"ok": True, "added": added, "total": len(uids)})


# ── Route registration helper (closure pattern, mirrors wallet_handlers) ──


def register_gate_routes(app, gatekeeper) -> None:
    """Register Gate account routes on the FastAPI app.

    Called by gatekeeper_routes.py (nanobot-quant plugin hook).
    """

    def _guard(user):
        if not user:
            return (401, "请先登录")
        if not gatekeeper._platform.is_commander(user):
            return (403, "仅 Commander 可访问")
        return None

    def _td_locked() -> bool:
        """TD 自主循环运行期间锁定划转（与 DEX 转账同规则）。"""
        try:
            from nanobot_quant.exec_params import load_exec_params

            p = load_exec_params() or {}
            return bool(p.get("td_enabled", False))
        except Exception:  # noqa: BLE001 — 锁检查失败放行
            return False

    async def _gate_page_guarded(request: Request):
        user = request.session.get("user") if request.session else None
        denied = _guard(user)
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        return await gate_page(request)

    async def _data_guarded(request: Request):
        user = request.session.get("user") if request.session else None
        denied = _guard(user)
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        return await gate_data(request)

    async def _transfer_guarded(request: Request):
        user = request.session.get("user") if request.session else None
        denied = _guard(user)
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        if _td_locked():
            return JSONResponse(
                {"ok": False, "error": "TD 自主循环运行中，禁止划转（先关闭 td_enabled）"},
                status_code=409,
            )
        return await gate_transfer(request)

    async def _confirm_guarded(request: Request):
        user = request.session.get("user") if request.session else None
        denied = _guard(user)
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        if _td_locked():
            return JSONResponse(
                {"ok": False, "error": "TD 自主循环运行中，禁止划转（先关闭 td_enabled）"},
                status_code=409,
            )
        return await gate_transfer_confirm(request)

    async def _sync_guarded(request: Request):
        user = request.session.get("user") if request.session else None
        denied = _guard(user)
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        return await sync_subaccounts(request)

    app.get("/config/gate")(_gate_page_guarded)
    app.get("/config/gate/data")(_data_guarded)
    app.post("/config/gate/transfer")(_transfer_guarded)
    app.post("/config/gate/transfer/confirm")(_confirm_guarded)
    app.post("/config/gate/sync-subaccounts")(_sync_guarded)
