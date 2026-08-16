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

from .onchainos_cli import get_token_assets, normalize_symbol
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
_WALLETS_OVERVIEW = _load_template("wallets_page.html")


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


# ── tracked token balance merge ────────────────────────────────────


# Map fine-grained wallet chain codes (from wallets.json, e.g. "sol", "xlayer_test")
# to the coarse chain names used in tokens.json (e.g. "solana", "xlayer").
_CHAIN_ALIASES = {
    "sol": "solana",
    "eth": "ethereum",
    "op_eth": "optimism",
    "arb_eth": "arbitrum",
    "base_eth": "base",
    "linea_eth": "linea",
    "scroll_eth": "scroll",
    "blast_eth": "blast",
    "era_eth": "zksync",
    "boba_eth": "boba",
    "matic": "polygon",
    "avax": "avalanche",
    "ftm": "fantom",
    "xdai": "gnosis",
    "klay": "klaytn",
    "mnt": "mantle",
    "ron": "ronin",
    "cfx": "conflux",
    "xlayer_test": "xlayer",
}


# Map human-readable chain names to the OKX CLI chainName (short) form used by
# the /config/wallet send dropdown (e.g. "solana" -> "sol"). Address-book
# entries created before the dropdown sources were unified may store long
# names; normalize on both sides so matching is alias-agnostic.
_NORMALIZE_ALIASES = {
    "solana": "sol",
    "ethereum": "eth",
    "x_layer": "xlayer",
    "x-layer": "xlayer",
    "x layer": "xlayer",
}


def _normalize_chain(chain: str) -> str:
    """Normalize a chain identifier to the OKX CLI chainName form."""
    c = (chain or "").strip().lower()
    return _NORMALIZE_ALIASES.get(c, c)


def _chain_address_map(accounts_res: dict) -> dict[str, str]:
    """Fine-grained chain code -> wallet address for the active sub-account.

    Source is the `wallet accounts` response (wallets.json) that backs the
    📍 钱包地址 card, so the sub-row wallet addresses always match the card.
    """
    if not isinstance(accounts_res, dict) or accounts_res.get("status") != "ok":
        return {}
    data = accounts_res.get("data") or {}
    accounts = data.get("accounts") or []
    if not isinstance(accounts, list) or not accounts:
        return {}
    active = next((a for a in accounts if isinstance(a, dict) and a.get("is_active")), accounts[0])
    if not isinstance(active, dict):
        return {}
    out: dict[str, str] = {}
    for a in active.get("addresses") or []:
        if isinstance(a, dict) and a.get("address"):
            chain = str(a.get("chain") or a.get("chain_index") or "").strip().lower()
            if chain:
                out[chain] = a["address"]
    return out


def _resolve_wallet_address(tokens_chain: str, addr_map: dict[str, str]) -> str:
    tc = (tokens_chain or "").strip().lower()
    if not tc:
        return ""
    for raw_chain, addr in addr_map.items():
        if raw_chain == tc or _CHAIN_ALIASES.get(raw_chain) == tc:
            return addr
    return ""


def _get_token_assets(data: dict) -> list:
    """Extract the token balance list from a `wallet balance` response data.

    CLI v4.3.1 puts the detail list at ``data.details[0].tokenAssets``
    (per-token fields: symbol/balance/rawBalance/decimal/tokenPrice/usdValue).
    Older shapes (``data.assets`` / ``data.balances``) are kept as fallback.
    """
    return get_token_assets(data)


def _parse_all_accounts_balance(data: dict) -> tuple[list[dict], str]:
    """Parse a `wallet balance --all` response into per-account groups.

    CLI v4.3.1 --all shape (batch API):

        data = {
          "totalValueUsd": "<sum>",
          "details": {                        # map: accountId -> cache entry
            "<accountId>": {                  # snake_case fields
              "updated_at": ...,
              "data": [ {                     # balance group
                "accountId": ..., "accountName": ..., "tokenAssets": [...]
              } ],
              "total_value_usd": "...",
            },
            ...
          }
        }

    Falls back to a flat ``details`` list, then to the single-account shape
    (``data.details[0].tokenAssets``) so the UI stays uniform. Pure function,
    unit-tested.
    """
    if not isinstance(data, dict) or not data:
        return [], ""
    total = str(data.get("totalValueUsd") or data.get("total_value_usd") or "")
    details = data.get("details")
    accounts: list[dict] = []
    if isinstance(details, dict):  # --all batch map: accountId -> entry
        for account_id, entry in details.items():
            if not isinstance(entry, dict):
                continue
            group = entry.get("data")
            if isinstance(group, list) and group and isinstance(group[0], dict):
                group = group[0]
            if not isinstance(group, dict):
                group = {}
            assets = group.get("tokenAssets") or group.get("assets") or []
            accounts.append({
                "account_id": str(group.get("accountId") or group.get("account_id") or account_id or ""),
                "account_name": str(group.get("accountName") or group.get("account_name") or ""),
                "total_value_usd": str(entry.get("total_value_usd") or entry.get("totalValueUsd")
                                        or group.get("totalValueUsd") or group.get("total_value_usd") or ""),
                "is_active": False,
                "assets": assets if isinstance(assets, list) else [],
            })
    elif isinstance(details, list):  # flat list of balance groups
        # A single group without accountId is the legacy single-account
        # shape — treat it as the active account.
        single = len(details) == 1
        for group in details:
            if not isinstance(group, dict):
                continue
            assets = group.get("tokenAssets") or group.get("assets") or []
            accounts.append({
                "account_id": str(group.get("accountId") or group.get("account_id") or ""),
                "account_name": str(group.get("accountName") or group.get("account_name") or ""),
                "total_value_usd": str(group.get("totalValueUsd") or group.get("total_value_usd") or ""),
                "is_active": single,
                "assets": assets if isinstance(assets, list) else [],
            })
    else:
        # single-account response — wrap the one account so the UI stays uniform
        accounts.append({
            "account_id": "",
            "account_name": "",
            "total_value_usd": total,
            "is_active": True,
            "assets": _get_token_assets(data),
        })
    return accounts, total


def _merge_tracked_into_active_account(
    accounts: list[dict], tokens: list[dict], addr_map: dict[str, str] | None = None,
) -> list[dict]:
    """Merge user-registered tokens (tokens.json) into the *active* account.

    tokens.json entries are global (symbol/chain/address) and the address
    book / wallet card only tracks the active account, so tracked tokens are
    attributed to the active account (existing single-account behaviour);
    other accounts keep only what the CLI reports. Pure function, unit-tested.
    """
    if not accounts:
        return accounts
    active = next((a for a in accounts if a.get("is_active")), accounts[0])
    tmp = {"status": "ok", "data": {"details": [{"tokenAssets": active.get("assets") or []}]}}
    merged = _merge_tracked_tokens(tmp, tokens, addr_map)
    active["assets"] = merged["data"].get("assets") or []
    return accounts


def _merge_tracked_tokens(bal_res: dict, tokens: list[dict], addr_map: dict[str, str] | None = None) -> dict:
    """Append user-registered tokens (tokens.json) to balance assets.

    `wallet balance` returns every non-zero asset of the active account, so a
    tracked token that is missing from the response has a zero balance — we
    still show it (marked ``tracked``) so users always see the tokens they
    care about, even at 0. Pure function, unit-tested.

    The merged list is written back to ``data["assets"]`` (the shape the
    WebUI card consumes) while the original ``details`` stays untouched.
    """
    if bal_res.get("status") != "ok" or not isinstance(bal_res.get("data"), dict):
        return bal_res
    data = bal_res["data"]
    assets = _get_token_assets(data)
    if not isinstance(assets, list):
        assets = []
    known = set()
    for a in assets:
        if isinstance(a, dict):
            # Normalize the CLI's per-token field to the card's amount field
            if "amount" not in a and a.get("balance") not in (None, ""):
                a["amount"] = str(a["balance"])
            sym = normalize_symbol(a.get("symbol") or a.get("token") or a.get("tokenSymbol") or "")
            if sym:
                known.add(sym)
    for t in tokens:
        sym = normalize_symbol(t.get("symbol", ""))
        if not sym:
            continue
        if sym in known:
            # The CLI already reports this tracked token with its real
            # balance — enrich that entry with the registered metadata so the
            # sub-rows (chain / contract / wallet address) still show.
            for a in assets:
                if isinstance(a, dict) and normalize_symbol(a.get("symbol") or "") == sym:
                    a.setdefault("tracked", True)
                    a["chain"] = str(t.get("chain") or a.get("chain") or "solana")
                    a["address"] = str(t.get("address") or a.get("tokenAddress") or "")
                    if addr_map:
                        a["wallet_address"] = _resolve_wallet_address(a["chain"], addr_map)
                    break
            continue
        entry = {
            "symbol": sym,
            "amount": "0",
            "tracked": True,
            "chain": str(t.get("chain") or "solana"),
            "address": str(t.get("address") or ""),
        }
        if addr_map:
            entry["wallet_address"] = _resolve_wallet_address(entry["chain"], addr_map)
        assets.append(entry)
        known.add(sym)
    data["assets"] = assets
    return bal_res


def _extract_balance_amount(q: dict, sym: str) -> str | None:
    """Extract the readable balance for `sym` from a per-token
    `wallet balance --token-address` response. Falls back to raw/decimals
    conversion and to a single-entry response (per-token query)."""
    if not isinstance(q, dict) or q.get("status") != "ok":
        return None
    data = q.get("data")
    if not isinstance(data, dict):
        return None
    assets = _get_token_assets(data)
    if not isinstance(assets, list):
        return None
    for a in assets:
        if not isinstance(a, dict):
            continue
        if normalize_symbol(a.get("symbol") or "") == sym:
            return _amount_of(a)
    if len(assets) == 1 and isinstance(assets[0], dict):
        return _amount_of(assets[0])
    return None


def _amount_of(a: dict) -> str | None:
    """Readable amount from an asset entry, with raw/decimals fallback."""
    amt = a.get("amount") or a.get("balance")
    if amt not in (None, ""):
        return str(amt)
    raw = a.get("raw") or a.get("rawAmount") or a.get("raw_amount") or a.get("rawBalance")
    dec = a.get("decimals") or a.get("decimal")
    if raw is not None and dec is not None:
        try:
            return str(int(raw) / 10 ** int(dec))
        except (ValueError, TypeError):
            return None
    return None


async def _fill_tracked_balances(bal_res: dict, tokens: list[dict]) -> dict:
    """Query the real on-chain balance of tracked tokens that the wallet
    balance summary omitted (e.g. RENDER: OKX balance detail does not list
    every mint, but a per-token --token-address query returns it).

    Only tracked entries with amount "0" and a contract address are queried;
    failures keep the zero placeholder.
    """
    if bal_res.get("status") != "ok" or not isinstance(bal_res.get("data"), dict):
        return bal_res
    data = bal_res["data"]
    assets = data.get("assets") or []
    if not isinstance(assets, list):
        return bal_res
    to_query = []
    for i, a in enumerate(assets):
        if (isinstance(a, dict) and a.get("tracked") and a.get("address")
                and a.get("amount") in (None, "", "0")):
            to_query.append((i, normalize_symbol(a.get("symbol", "")),
                             str(a.get("chain") or "solana"), str(a["address"])))
    if not to_query:
        return bal_res

    async def query(idx: int, sym: str, chain: str, address: str) -> None:
        try:
            q = await asyncio.wait_for(
                asyncio.to_thread(wallet_balance, chain=chain, token_address=address, force=True),
                timeout=25,
            )
            amt = _extract_balance_amount(q, sym)
            if amt is not None:
                assets[idx]["amount"] = amt
        except Exception:  # noqa: BLE001 — per-token probe failures keep the 0 placeholder
            pass

    await asyncio.gather(*(query(i, s, c, a) for i, s, c, a in to_query))
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

    def _td_locked() -> bool:
        """TD 自主循环运行期间锁定子钱包操作（switch/send/add）。

        1+2 组合（2026-08-09 拍板）：活跃账户（selected_account_id）是全局
        状态，TD 循环与 WebUI 并发切换会互相踩——运行期间禁止手动切账户/
        转账/建子钱包，需先关闭 td_enabled。
        """
        try:
            from nanobot_quant.exec_params import load_exec_params

            p = load_exec_params() or {}
            return bool(p.get("td_enabled", False))
        except Exception:  # noqa: BLE001 — 锁检查失败放行（不阻断读取类操作）
            return False

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
        if _normalize_chain(chain) in ("sol", "501"):
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

    async def _wallets_overview(request: Request):
        """GET /config/wallets — wallet-management category page (DEX / CEX)."""
        _u = request.session.get("user")
        if not _u:
            return RedirectResponse("/")
        if not gatekeeper._platform.is_commander(_u):
            return HTMLResponse(
                "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 仅 Commander 可访问</h3>",
                status_code=403,
            )
        return HTMLResponse(_WALLETS_OVERVIEW)

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
            _call(wallet_balance, all_accounts=True, timeout=60),
            _call(wallet_history, limit="10", timeout=30),
            _call(wallet_accounts, timeout=10),
            _call(wallet_chains, timeout=30),
        )
        # --all batch mode: split the response into per-account groups and
        # tag the active account so the balance card shows every sub-account.
        # Tracked-token merge + per-contract probes stay on the active account
        # (existing behaviour); other accounts keep only what the CLI reports.
        tokens = _read_tokens()
        if bal_res.get("status") == "ok" and isinstance(bal_res.get("data"), dict):
            data = bal_res["data"]
            accounts, total_usd = _parse_all_accounts_balance(data)
            # Prefer account names / active flag from the wallet accounts card
            # (wallets.json) — the --all cache entries carry no names.
            acc_meta = {
                a.get("account_id"): a for a in
                ((accounts_res.get("data") or {}).get("accounts") or [])
                if isinstance(a, dict) and a.get("account_id")
            }
            for acc in accounts:
                meta = acc_meta.get(acc["account_id"])
                if meta:
                    if not acc["account_name"]:
                        acc["account_name"] = str(meta.get("account_name") or "")
                    acc["is_active"] = bool(meta.get("is_active"))
            if accounts and not any(a.get("is_active") for a in accounts):
                accounts[0]["is_active"] = True
            # Merge user-registered tokens (tokens.json) into the balance view
            # so tracked tokens show even with zero balance. wallet balance
            # returns all non-zero assets, so a tracked token missing from the
            # response means its balance is 0 ("0" with a 🪙 marker). addr_map
            # (from the accounts card) lets the sub-row also show the wallet
            # address of the token's chain.
            accounts = _merge_tracked_into_active_account(
                accounts, tokens, _chain_address_map(accounts_res))
            # Some mints (e.g. RENDER) are missing from the balance detail even
            # when the account holds them — probe those tracked tokens per-contract
            # so the real quantity shows instead of a hardcoded 0.
            active = next((a for a in accounts if a.get("is_active")), accounts[0] if accounts else None)
            if active is not None:
                tmp = {"status": "ok", "data": {"assets": active.get("assets") or []}}
                tmp = await _fill_tracked_balances(tmp, tokens)
                active["assets"] = tmp["data"].get("assets") or []
            data["accounts"] = accounts
            if total_usd:
                data["totalValueUsd"] = total_usd
            bal_res = {"status": "ok", "data": data}
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
        if _td_locked():
            return JSONResponse({"ok": False, "error": "TD 自主循环运行中，请先在 /config/exec 关闭 td_enabled 再操作子钱包"}, status_code=409)
        result = await _call(wallet_add, timeout=60)
        return JSONResponse({"ok": result.get("status") == "ok", **result})

    async def _wallet_switch(request: Request) -> JSONResponse:
        """POST /config/wallet/switch — switch active account.

        Body: {account_id: "..."}
        """
        denied = _guard(request.session.get("user"))
        if denied:
            return JSONResponse({"ok": False, "error": denied[1]}, status_code=denied[0])
        if _td_locked():
            return JSONResponse({"ok": False, "error": "TD 自主循环运行中，请先在 /config/exec 关闭 td_enabled 再操作子钱包"}, status_code=409)
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
        if _td_locked():
            return JSONResponse({"ok": False, "error": "TD 自主循环运行中，请先在 /config/exec 关闭 td_enabled 再操作子钱包"}, status_code=409)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)

        chain = _normalize_chain(str(body.get("chain", "")).strip().lower())
        to_address = str(body.get("to_address", "")).strip()
        amount = str(body.get("amount", "")).strip()
        token_address = str(body.get("token_address", "")).strip()

        if not chain or not to_address or not amount:
            return JSONResponse({"ok": False, "error": "chain / to_address / amount 必填"}, status_code=400)
        if not _is_valid_address(chain, to_address):
            return JSONResponse({"ok": False, "error": "目标地址格式无效"}, status_code=400)

        book = _load_address_book()
        if not any(
            _normalize_chain(str(e.get("chain") or "")) == chain and e.get("address") == to_address
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
        if _td_locked():
            return JSONResponse({"ok": False, "error": "TD 自主循环运行中，请先在 /config/exec 关闭 td_enabled 再操作子钱包"}, status_code=409)
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
        chain = _normalize_chain(str(body.get("chain", "")).strip().lower())
        address = str(body.get("address", "")).strip()
        if not name or not chain or not address:
            return JSONResponse({"ok": False, "error": "name / chain / address 必填"}, status_code=400)
        if not _is_valid_address(chain, address):
            return JSONResponse({"ok": False, "error": "地址格式无效"}, status_code=400)

        book = _load_address_book()
        if any(
            _normalize_chain(str(e.get("chain") or "")) == chain and e.get("address") == address
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

    app.add_route("/config/wallets", _wallets_overview, methods=["GET"])
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
