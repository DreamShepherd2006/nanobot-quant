"""Official gate-api SDK wrapper — the single Gate access layer.

Step 1 of the Gate SDK migration: replace ad-hoc signed REST calls with the
official ``gate_api`` SDK (pinned ``gate-api==7.2.123``) for order lifecycle,
main→sub transfers and sub-account balances. The one SDK blind spot
(``GET /spot/accounts`` — querying *own* spot balance with one's own key) has
no SDK method; it stays on the minimal signed call
(``gate_credentials.fetch_spot_balances``) and is re-exported here as
``spot_accounts`` so call sites import from one module.

Design rules:
- Plain dicts in, plain dicts out — SDK model objects never leak to callers.
- ``ApiException`` is converted to ``RuntimeError`` keeping the Gate
  label/message (e.g. ``"HTTP 400 INVALID_CURRENCY_PAIR"``).
- Market orders never send ``time_in_force`` — Gate rejects ``gtc`` for
  market orders (HTTP 400 ``TimeInForce gtc is not support for market order``).
"""

from __future__ import annotations

from typing import Any, Optional

from gate_api import (  # type: ignore[import-not-found]
    ApiClient,
    ApiException,
    Configuration,
    Order,
    SpotApi,
    SubAccountBalance,
    SubAccountTransfer,
    WalletApi,
)

from .gate_credentials import fetch_spot_balances

__all__ = [
    "create_order",
    "get_order",
    "cancel_order",
    "get_currency_pair",
    "spot_accounts",
    "transfer_to_sub",
    "sub_account_balances",
    "make_spot_api",
    "make_wallet_api",
]


def _api_client(api_key: str, api_secret: str) -> ApiClient:
    if not api_key or not api_secret:
        raise RuntimeError("Gate API Key 未配置（/config/credentials/gate 录入）")
    return ApiClient(Configuration(key=api_key, secret=api_secret))


def make_spot_api(api_key: str, api_secret: str) -> SpotApi:
    """Build a SpotApi bound to the given credentials (own or sub-account key)."""
    return SpotApi(_api_client(api_key, api_secret))


def make_wallet_api(api_key: str, api_secret: str) -> WalletApi:
    """Build a WalletApi bound to the given credentials (main key for transfers)."""
    return WalletApi(_api_client(api_key, api_secret))


def _call(label: str, fn) -> Any:
    try:
        return fn()
    except ApiException as exc:
        body = getattr(exc, "body", None)
        detail = ""
        if body:
            if isinstance(body, dict):
                detail = f" {body.get('label', '')} {body.get('message', '')}".rstrip()
            elif isinstance(body, (bytes, bytearray)):
                try:
                    detail = f" {body.decode('utf-8', 'replace')}"
                except Exception:
                    detail = ""
            else:
                detail = f" {body}"
        msg = f"{label} failed: HTTP {exc.status} {exc.reason or ''}{detail}".strip()
        raise RuntimeError(msg) from exc


def create_order(
    api_key: str,
    api_secret: str,
    currency_pair: str,
    side: str,
    amount: str,
    order_type: str = "market",
    price: Optional[str] = None,
    text: Optional[str] = None,
) -> dict:
    """Place a spot order (market by default, mirroring signed-REST semantics).

    Market: ``amount`` is the quote-currency amount for buy, base-currency
    amount for sell. Limit: ``price`` is required, ``amount`` is base units.
    Returns the created order as a dict (``id``, ``status``, ...).
    """
    if not currency_pair or not side or not amount:
        raise RuntimeError("create_order 缺少必要参数（currency_pair/side/amount）")
    if order_type == "limit" and not price:
        raise RuntimeError("限价单必须提供 price")
    order = Order(
        currency_pair=currency_pair,
        side=side,
        amount=amount,
        type=order_type,
        price=price if order_type == "limit" else None,
        text=text,
        # time_in_force left None: market rejects gtc; limit falls back to API
        # default (gtc). Same behaviour as the previous signed-REST calls.
    )
    api = make_spot_api(api_key, api_secret)
    return _call("create_order", lambda: api.create_order(order)).to_dict()


def get_order(api_key: str, api_secret: str, order_id: str, currency_pair: str) -> dict:
    """Fetch a spot order (``status``/``left``/``filled_amount``/``avg_deal_price``/``finish_as``)."""
    api = make_spot_api(api_key, api_secret)
    return _call(
        "get_order", lambda: api.get_order(order_id, currency_pair)
    ).to_dict()


def cancel_order(api_key: str, api_secret: str, order_id: str, currency_pair: str) -> dict:
    """Cancel a spot order; returns the cancelled order as a dict."""
    api = make_spot_api(api_key, api_secret)
    return _call(
        "cancel_order", lambda: api.cancel_order(order_id, currency_pair)
    ).to_dict()


def get_currency_pair(api_key: str, api_secret: str, currency_pair: str) -> dict:
    """Fetch currency-pair metadata (amount_precision/precision/min_quote_amount/trade_status)."""
    api = make_spot_api(api_key, api_secret)
    return _call(
        "get_currency_pair", lambda: api.get_currency_pair(currency_pair)
    ).to_dict()


def spot_accounts(api_key: str, api_secret: str) -> dict[str, dict]:
    """Own spot balances -> {CURRENCY: {"available": float, "locked": float}}.

    SDK blind spot (no ``SpotApi`` method for ``GET /spot/accounts``) — kept
    on the minimal signed call; re-exported here for call-site uniformity.
    """
    return fetch_spot_balances(api_key, api_secret)


def transfer_to_sub(
    api_key: str,
    api_secret: str,
    currency: str,
    sub_uid: str,
    amount: str,
    direction: str = "to",
) -> dict:
    """Transfer between main and sub accounts (in-house, instant).

    ``direction``: ``to`` = main → sub (default), ``from`` = sub → main
    (Gate v4 enum — NOT deposit/withdraw, which the API rejects with
    INVALID_PARAM_VALUE). ``sub_uid`` is the numeric sub-account UID.
    Returns the transfer as a dict.
    """
    if not currency or not sub_uid or not amount:
        raise RuntimeError("transfer_to_sub 缺少必要参数（currency/sub_uid/amount）")
    transfer = SubAccountTransfer(
        currency=currency,
        sub_account=sub_uid,
        amount=amount,
        direction=direction,
    )
    api = make_wallet_api(api_key, api_secret)
    return _call(
        "transfer_to_sub", lambda: api.transfer_with_sub_account(transfer)
    ).to_dict()


def sub_account_balances(
    api_key: str, api_secret: str, sub_uid: Optional[str] = None
) -> list[dict]:
    """Balances of sub-account(s), queried with the *main* key.

    Without ``sub_uid`` returns **every** sub-account (list form of
    ``GET /wallet/sub_account_balances``) — the account page uses this so no
    sub-account keys are required (main key with the '子账号' permission is
    enough). With ``sub_uid`` filters to that sub-account.
    Each row is ``{"uid": ..., "available": {...}, "locked": {...}}``.
    """
    api = make_wallet_api(api_key, api_secret)
    rows = _call(
        "sub_account_balances",
        lambda: api.list_sub_account_balances(sub_uid=sub_uid),
    )
    return [r.to_dict() for r in (rows or []) if isinstance(r, SubAccountBalance)]
