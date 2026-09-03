"""OKX v5 signed REST client — private account APIs (options trading line).

Lightweight hand-rolled signing (no official SDK dependency), following the
project convention for OKX/Gate REST adapters (``okx_cex_data.py`` uses plain
REST for public market data; this module adds the private signed layer on top).

Signature rule (OKX v5):
    timestamp = ISO-8601 UTC with millisecond precision + ``Z``
    sign      = base64( HMAC_SHA256(secret, timestamp + method + requestPath + body) )
    requestPath includes the query string (e.g. ``/api/v5/account/positions?instType=OPTION``)
Headers: ``OK-ACCESS-KEY`` / ``OK-ACCESS-SIGN`` / ``OK-ACCESS-TIMESTAMP`` /
         ``OK-ACCESS-PASSPHRASE``

Only **read-only** endpoints are exposed in this batch (account config /
balance / positions / trade-fee). Order placement lands in a later batch and
will reuse :func:`okx_request`.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
from typing import Optional

import requests

from .okx_cex_credentials import get_okx_cex_credentials

_BASE_URL = "https://www.okx.com"
_TIMEOUT = 15

#: instType / instFamily used by the options line (BTC-USD & ETH-USD are the
#: only option families OKX lists; cf. options research notes 2026-09-03).
OPTION_FAMILIES = ("BTC-USD", "ETH-USD")


# ── signing helpers ──────────────────────────────────────────────

def _timestamp() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sign(secret: str, ts: str, method: str, path: str, body: str) -> str:
    msg = ts + method + path + body
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _headers(creds: dict, method: str, path: str, body: str = "") -> dict:
    ts = _timestamp()
    return {
        "OK-ACCESS-KEY": creds["api_key"],
        "OK-ACCESS-SIGN": _sign(creds["secret_key"], ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds["passphrase"],
        "Content-Type": "application/json",
        "User-Agent": "nanobot-quant/0.1",
    }


# ── core request ─────────────────────────────────────────────────

def okx_request(
    method: str,
    path: str,
    creds: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: int = _TIMEOUT,
) -> dict:
    """Signed request to the OKX v5 private API.

    Returns the JSON ``data`` payload when ``code == "0"``; raises
    ``RuntimeError`` on API error codes and on transport/HTTP errors.
    """
    creds = get_okx_cex_credentials(creds)
    body_str = ""
    if body:
        body_str = _json_dumps(body)
    headers = _headers(creds, method, path, body_str)
    url = _BASE_URL + path
    try:
        resp = requests.request(
            method, url, headers=headers, data=body_str or None, timeout=timeout
        )
    except requests.RequestException as exc:  # network errors
        raise RuntimeError(f"OKX request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"OKX HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"OKX non-JSON response: {resp.text[:200]}") from exc
    code = payload.get("code")
    if code != "0":
        raise RuntimeError(f"OKX {code} {payload.get('msg', '')}".strip())
    return payload.get("data")


def _json_dumps(body: dict) -> str:
    import json

    return json.dumps(body)


# ── read-only account endpoints ──────────────────────────────────

def get_account_config(creds: Optional[dict] = None) -> dict:
    """GET /api/v5/account/config — account level / permissions / uid."""
    data = okx_request("GET", "/api/v5/account/config", creds=creds)
    return (data or [{}])[0]


def get_balance(creds: Optional[dict] = None) -> dict:
    """GET /api/v5/account/balance — normalized per-currency balances.

    Returns ``{"total_eq": float, "details": [{"ccy", "cash", "avail", "frozen", "eq"}, ...]}``
    """
    data = okx_request("GET", "/api/v5/account/balance", creds=creds)
    row = (data or [{}])[0]
    details = []
    for d in row.get("details") or []:
        details.append(
            {
                "ccy": d.get("ccy"),
                "cash": _f(d.get("cashBal")),
                "avail": _f(d.get("availBal")),
                "frozen": _f(d.get("frozenBal")),
                "eq": _f(d.get("eq")),
            }
        )
    return {"total_eq": _f(row.get("totalEq")), "details": details}


def get_positions(
    inst_type: str = "OPTION", creds: Optional[dict] = None
) -> list:
    """GET /api/v5/account/positions — current positions (default options)."""
    data = okx_request(
        "GET", f"/api/v5/account/positions?instType={inst_type}", creds=creds
    )
    return data if isinstance(data, list) else []


def get_trade_fee(
    inst_family: str = "BTC-USD", creds: Optional[dict] = None
) -> dict:
    """GET /api/v5/account/trade-fee — maker/taker fee rate for a family."""
    data = okx_request(
        "GET",
        f"/api/v5/account/trade-fee?instType=OPTION&instFamily={inst_family}",
        creds=creds,
    )
    return (data or [{}])[0]


def _f(value) -> float:
    """Coerce OKX numeric string to float (empty/None → 0.0)."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
