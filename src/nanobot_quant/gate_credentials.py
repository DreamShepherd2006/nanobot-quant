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
    """Load gate.json; return None when missing/unreadable."""
    for p in _credential_paths():
        try:
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "main" in data:
                return data
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
    return creds.get("main") or {}


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
    """Gate spot pair for a symbol: CRCLX -> CRCLXUSDT (tokens.json gate_symbol wins)."""
    sym = str(symbol).upper().strip()
    tokens_json = tokens_json if tokens_json is not None else load_tokens_json()
    for e in tokens_json:
        if isinstance(e, dict) and str(e.get("symbol", "")).upper() == sym:
            gs = str(e.get("gate_symbol") or e.get("symbol") or "").upper().strip()
            if gs:
                sym = gs
            break
    sym = sym.replace("-", "")
    return sym if sym.endswith("USDT") else f"{sym}USDT"


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
