"""gate.json credentials + CEX symbol mapping (Gate execution / OKX data).

Gate CEX is the second execution channel (``execution_channel="cex"``) —
see docs/quant-system.md §18. Credentials layout::

    {
      "main": {"api_key": "...", "api_secret": "...", "uid": "15119093"},
      "sub_accounts": {
        "gate_bot1": {"uid": "59175220", "api_key": "...", "api_secret": "..."},
        ...
      }
    }

- main key: sub-account management + main->sub transfers
  (``WalletApi.transfer_with_sub_account``)
- sub-account keys: place orders and read balances for that sub-account only

Symbol mapping: the same underlying tokenized asset uses different tickers
per exchange (CRCLX on Gate ↔ XCRCL on OKX). tokens.json entries may carry
``gate_symbol`` / ``okx_symbol`` overrides; defaults fall back to the entry
symbol itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GATE_API_BASE = "https://api.gateio.ws"


def _credential_paths() -> list[str]:
    """Candidate gate.json paths: Space persistent volume first, then local dev."""
    paths: list[str] = []
    for root in ("/data", "/mnt/workspace"):
        for sub in ("legion/credentials", "legion/legion/credentials"):
            paths.append(os.path.join(root, sub, "gate.json"))
    paths.append(os.path.expanduser("~/gate_creds.json"))
    return paths


def load_gate_credentials() -> Optional[dict]:
    """Load gate.json; return None when missing/unreadable.

    Supports both nested (main/sub_accounts, P1 CLI 形态) and flat
    (WebUI 凭证表单形态：{api_key, api_secret, uid}) layouts — flat is
    normalised to the nested layout so consumers need one shape.
    """
    for p in _credential_paths():
        try:
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "main" in data:
                return data
            # flat（WebUI 写入形态）→ 归一化为主键 + 空子账号
            if isinstance(data, dict) and data.get("api_key"):
                return {"main": data, "sub_accounts": {}}
        except (OSError, ValueError):
            continue
    return None


def get_api_credentials(
    credentials: Optional[dict] = None, sub_account: Optional[str] = None
) -> dict:
    """Return an API credentials dict (api_key/api_secret/uid).

    ``sub_account`` may be a sub-account name ("gate_bot1") or a uid
    ("59175220"). When omitted, the main key is returned.
    """
    creds = credentials or load_gate_credentials()
    if not creds:
        raise FileNotFoundError(
            "gate.json not found in " + ", ".join(_credential_paths())
        )
    if sub_account:
        subs = creds.get("sub_accounts") or {}
        if sub_account in subs:
            return subs[sub_account]
        for name, sa in subs.items():
            if str(sa.get("uid")) == str(sub_account):
                return sa
        raise KeyError(
            f"sub-account {sub_account!r} not in gate.json (have: {sorted(subs)})"
        )
    return creds.get("main") or creds


def load_tokens_json() -> list[dict]:
    """Load tokens.json (list of {symbol, chain, address, gate_symbol?, okx_symbol?})."""
    for p in _credential_paths():
        base = os.path.dirname(p)
        if not base:
            continue
        tp = os.path.join(base, "tokens.json")
        try:
            if os.path.isfile(tp):
                with open(tp, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
        except (OSError, ValueError):
            continue
    return []


def gate_pair(symbol: str, tokens_json: Optional[list[dict]] = None) -> str:
    """Gate spot pair for a symbol: CRCLX -> CRCLX_USDT (tokens.json gate_symbol wins).

    Gate API uses ``BASE_QUOTE`` with an underscore (``BTC_USDT``), unlike
    OKX (``BTC-USDT``) or Binance (``BTCUSDT``). Any input separator is
    normalised away before rebuilding the underscore form.
    """
    sym = str(symbol).upper().strip()
    tokens_json = tokens_json if tokens_json is not None else load_tokens_json()
    for e in tokens_json:
        if isinstance(e, dict) and str(e.get("symbol", "")).upper() == sym:
            gs = str(e.get("gate_symbol") or e.get("symbol") or "").upper().strip()
            if gs:
                sym = gs
            break
    base = sym.replace("-", "").replace("_", "")
    if base.endswith("USDT"):
        base = base[:-4]
    return f"{base}_USDT"


def okx_ticker(symbol: str, tokens_json: Optional[list[dict]] = None) -> str:
    """OKX CEX ticker for a symbol: CRCLX -> XCRCL (tokens.json okx_symbol wins)."""
    sym = str(symbol).upper().strip()
    tokens_json = tokens_json if tokens_json is not None else load_tokens_json()
    for e in tokens_json:
        if isinstance(e, dict) and str(e.get("symbol", "")).upper() == sym:
            osym = str(e.get("okx_symbol") or e.get("symbol") or "").upper().strip()
            if osym:
                return osym
            break
    return sym


def signed_request(
    method: str,
    path: str,
    query: str = "",
    body: str = "",
    api_key: str = "",
    api_secret: str = "",
    timeout: int = 15,
) -> tuple[int, Any]:
    """Signed Gate API request for endpoints missing from gate-api SDK.

    The path must include the ``/api/v4`` prefix (Gate signature uses the full
    API path, e.g. ``/api/v4/spot/accounts``; signing ``/spot/accounts``
    yields HTTP 401). Signature::

        SIGN = HMAC_SHA512(secret, f"{METHOD}\\n{path}\\n{query}\\n{sha512(body)}\\n{ts}")
    """
    ts = str(int(time.time()))
    body_hash = hashlib.sha512(body.encode()).hexdigest()
    sign = hmac.new(
        api_secret.encode(),
        f"{method}\n{path}\n{query}\n{body_hash}\n{ts}".encode(),
        hashlib.sha512,
    ).hexdigest()
    url = _GATE_API_BASE + path + (f"?{query}" if query else "")
    headers = {
        "KEY": api_key,
        "Timestamp": ts,
        "SIGN": sign,
        "Content-Type": "application/json",
        "User-Agent": "nanobot-quant/0.1",
    }
    req = urllib.request.Request(
        url,
        data=body.encode() if body else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "null"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {}
        return e.code, payload


def fetch_spot_balances(api_key: str, api_secret: str) -> dict[str, dict]:
    """GET /spot/accounts -> {CURRENCY: {"available": float, "locked": float}}.

    Sub-account keys return that sub-account's own balances.
    """
    code, data = signed_request(
        "GET", "/api/v4/spot/accounts", api_key=api_key, api_secret=api_secret
    )
    if code != 200:
        raise RuntimeError(f"GET /spot/accounts failed: HTTP {code} {data}")
    out: dict[str, dict] = {}
    for d in data or []:
        cur = d.get("currency", "")
        out[cur] = {
            "available": float(d.get("available") or 0),
            "locked": float(d.get("locked") or 0),
        }
    return out


def load_slot_map(credentials: Optional[dict] = None) -> dict[str, str]:
    """Return slot → sub-account-name mapping (persisted in gate.json slot_map).

    Defaults to 1..5 → gate_bot1..5 when the field is absent, so existing
    deployments without slot_map keep working. Values are validated against
    the configured sub-accounts; unknown names are dropped.
    """
    creds = credentials if credentials is not None else (load_gate_credentials() or {})
    if not creds:
        return {}
    slot_map = creds.get("slot_map") or {}
    subs = set((creds.get("sub_accounts") or {}).keys())
    out: dict[str, str] = {}
    for i in range(1, 6):
        configured = slot_map.get(str(i))
        if configured and subs and configured not in subs:
            configured = None  # slot references an unconfigured sub → fall back to default
        out[str(i)] = configured or f"gate_bot{i}"
    return out


def sub_account_transfer(
    amount: str,
    target_sub: str,
    currency: str = "USDT",
    credentials: Optional[dict] = None,
    timeout: int = 15,
) -> dict:
    """Transfer funds from the main account to a sub-account (in-house, instant).

    POST /api/v4/wallet/sub_account_transfers, signed with the main key.
    ``target_sub`` accepts a sub-account name ("gate_bot1") or uid; it is
    resolved to the sub-account uid for the API call.
    """
    creds = credentials or load_gate_credentials()
    if not creds:
        raise FileNotFoundError("gate.json not found")
    main = creds.get("main") or {}
    if not main.get("api_key") or not main.get("api_secret"):
        raise RuntimeError("主账号 Key 未配置（/config/credentials/gate 录入）")
    sa = get_api_credentials(creds, target_sub)
    uid = str(sa.get("uid") or "").strip()
    if not uid:
        raise RuntimeError(f"子账号 {target_sub} 未配置 UID（/config/credentials/gate 录入）")
    # Official SDK path (gate-api): transfer with the main key; direction
    # deposit = main → sub. Signed REST was dropped — SDK covers this API.
    from .gate_sdk import transfer_to_sub  # avoid import cycle

    return transfer_to_sub(
        api_key=main["api_key"],
        api_secret=main["api_secret"],
        currency=currency,
        sub_uid=uid,
        amount=str(amount),
        direction="deposit",
    )


def fetch_all_balances(credentials: Optional[dict] = None) -> dict:
    """Balances for the main account and every sub-account.

    The **main key** (with the '子账号' permission) queries all sub-account
    balances via ``GET /wallet/sub_account_balances`` — no sub-account keys
    are required for the account page (only UIDs for name matching).

    Returns ``{"main": {CURRENCY: {available, locked}},
               "sub_accounts": [{"uid", "balances": {CURRENCY: {available, locked}}}, ...]}``.
    A failed query surfaces as ``{"__error": "..."}`` instead of aborting the
    whole page (fail-open for display, fail-closed never guessed).
    """
    from .gate_sdk import sub_account_balances as _list_sub_accounts  # avoid import cycle

    creds = credentials or load_gate_credentials()
    if not creds:
        return {"__error": "gate.json not found"}
    main = creds.get("main") or {}
    out: dict = {"main": {}, "sub_accounts": []}

    if not main.get("api_key") or not main.get("api_secret"):
        return {
            "main": {"__error": "主账号 Key 未配置（/config/credentials/gate 录入）"},
            "sub_accounts": {"__error": "主账号 Key 未配置"},
        }

    try:
        out["main"] = fetch_spot_balances(main["api_key"], main["api_secret"])
    except RuntimeError as exc:
        out["main"] = {"__error": str(exc)}

    try:
        rows = _list_sub_accounts(main["api_key"], main["api_secret"])
        subs: list[dict] = []
        for r in rows or []:
            bal: dict[str, dict] = {}
            for cur, amt in (r.get("available") or {}).items():
                bal[cur] = {"available": float(amt or 0), "locked": 0.0}
            for cur, amt in (r.get("locking") or r.get("locked") or {}).items():
                entry = bal.setdefault(cur, {"available": 0.0, "locked": 0.0})
                entry["locked"] = float(amt or 0)
            subs.append({"uid": str(r.get("uid") or ""), "balances": bal})
        out["sub_accounts"] = subs
    except RuntimeError as exc:
        out["sub_accounts"] = {"__error": str(exc)}
    return out
