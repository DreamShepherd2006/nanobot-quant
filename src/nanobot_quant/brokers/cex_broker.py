"""Lumibot Broker that executes trades on Gate CEX spot market.

Second execution channel (``execution_channel="cex"``), alongside
OnchainOSBroker (DEX) — docs/quant-system.md §18. Data/execution separation:
signal data comes from OKX CEX (okx_cex_data.py), orders execute on Gate.

The same underlying tokenized asset may use different tickers per exchange
(CRCLX on Gate ↔ XCRCL on OKX); the mapping lives in tokens.json
(``gate_symbol`` / ``okx_symbol`` fields, see gate_credentials).

Implementation notes (2026-08-14):
- Trading endpoints (create/query/cancel order, balances) use signed Gate REST
  requests (gate_credentials.signed_request) — the official gate-api SDK's
  ``CreateOrder`` model does not match POST /spot/orders fields
  (no currency_pair/side/amount), so the SDK is used only for the endpoints
  it wraps correctly (sub-account management / transfers via WalletApi).
- Gate signature requires the full ``/api/v4`` path prefix; signing
  ``/spot/accounts`` yields HTTP 401.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Optional

from lumibot.brokers import Broker

from nanobot_quant.gate_credentials import (
    fetch_spot_balances,
    gate_pair,
    get_api_credentials,
    okx_ticker,
    signed_request,
)
from nanobot_quant.okx_cex_data import fetch_ticker

logger = logging.getLogger("nanobot_quant.brokers.cex")

# Currencies treated as cash — everything else counts as a position.
_CASH_CURRENCIES = frozenset({"USDT", "USDC", "USDG", "USD", "TUSD"})

_SPOT_ORDERS_PATH = "/api/v4/spot/orders"
_SPOT_ACCOUNTS_PATH = "/api/v4/spot/accounts"


class _DummyDataSource:
    """Placeholder so Broker.__init__ does not require a real data source."""

    def __init__(self, *args, **kwargs):
        pass

    def get_chains(self, asset=None, quote=None):
        return {}


class CexBroker(Broker):
    """Lumibot Broker that routes orders through Gate CEX spot market.

    Parameters:
        credentials: gate.json dict (main + sub_accounts); None → auto-load.
        tokens_json: tokens.json entries (symbol/gate_symbol/okx_symbol).
        sub_account: sub-account name ("gate_bot1") or uid; None → main key.
        slippage: price-protection tolerance in percent (1 = 1%). P1 records
            deviation; reject logic lands in P4 (docs/quant-system.md §18.8).
    """

    SOURCE = "gate"

    def __init__(
        self,
        credentials: Optional[dict] = None,
        tokens_json: Optional[list[dict]] = None,
        sub_account: Optional[str] = None,
        slippage: str = "0.01",
        **kwargs,
    ):
        if "data_source" not in kwargs:
            kwargs["data_source"] = _DummyDataSource()
        super().__init__(**kwargs)
        # Crypto is a 24/7 continuous market: keeps the StrategyExecutor loop
        # running for execution_mode="loop".
        self.market = "24/7"
        self._credentials = credentials
        self._tokens_json = tokens_json or []
        self._slippage = float(slippage)
        self._sub_account = sub_account

        creds = get_api_credentials(credentials, sub_account)
        self._api_key = str(creds.get("api_key") or "")
        self._api_secret = str(creds.get("api_secret") or "")
        self._uid = str(creds.get("uid") or "")
        if not self._api_key or not self._api_secret:
            raise ValueError(
                "Gate credentials missing api_key/api_secret"
                f" (gate.json main or sub_account={sub_account!r})"
            )
        self._tracked: dict[str, dict] = {}  # order_id → meta

    # ═══════════════════════════════════════════════════════════
    #  Helpers
    # ═══════════════════════════════════════════════════════════

    # Gate pair metadata cache (amount_precision / min_quote_amount / trade_status)
    _pair_meta_cache: dict[str, tuple[float, dict]] = {}
    _PAIR_META_TTL = 300.0

    def _pair_meta(self, pair: str) -> dict:
        """Gate spot pair metadata; {} on any failure (validation then skipped)."""
        now = time.time()
        cached = self._pair_meta_cache.get(pair)
        if cached and now - cached[0] < self._PAIR_META_TTL:
            return cached[1]
        meta: dict = {}
        try:
            code, data = self._request(
                "GET", f"/api/v4/spot/currency_pairs/{pair}",
            )
            if code == 200 and isinstance(data, dict):
                meta = {
                    "amount_precision": int(data.get("amount_precision") or 0),
                    "quote_precision": int(data.get("precision") or 0),
                    "min_quote_amount": float(data.get("min_quote_amount") or 0),
                    "trade_status": str(data.get("trade_status") or ""),
                }
        except Exception as e:
            print(f"[DIAG] CEX pair meta error {pair}: {e}", file=sys.stderr, flush=True)
        self._pair_meta_cache[pair] = (now, meta)
        return meta

    @staticmethod
    def _format_err(prefix: str, payload: Any = None) -> str:
        """Build a readable error string, keeping platform error codes intact."""
        if not payload:
            return prefix
        if isinstance(payload, dict):
            label = payload.get("label") or payload.get("message") or ""
            if label:
                return f"{prefix}: {label}"
        return f"{prefix}: {payload}"

    def _request(self, method: str, path: str, query: str = "", body: str = ""):
        """Signed Gate request bound to this broker's credentials."""
        code, payload = signed_request(
            method, path, query=query, body=body,
            api_key=self._api_key, api_secret=self._api_secret,
        )
        return code, payload

    def _query_order(self, order_id: str, pair: str) -> tuple[str, float, float, float]:
        """Query order status → (lumibot_status, filled, left, avg_price)."""
        code, data = self._request(
            "GET", f"{_SPOT_ORDERS_PATH}/{order_id}",
            query=f"currency_pair={pair}",
        )
        if code != 200:
            return "submitted", 0.0, 0.0, 0.0
        status = str(data.get("status") or "").lower()
        filled = float(data.get("filled_amount") or 0)
        left = float(data.get("left") or 0)
        avg = float(data.get("avg_deal_price") or 0)
        finish_as = str(data.get("finish_as") or "").lower()

        if status == "closed" or finish_as == "filled":
            return "filled", filled, left, avg
        if status == "cancelled":
            return "cancelled", filled, left, avg
        if finish_as in ("ioc", "expired"):
            # market IOC: remaining quantity expired — treat any fill as done
            return "filled" if filled > 0 else "failed", filled, left, avg
        return "submitted", filled, left, avg

    def _price_of(self, symbol: str) -> float:
        """Current price for order sizing. Gate ticker first (same-exchange,
        closest to fill), OKX CEX as fallback; 0.0 when both fail."""
        pair = gate_pair(symbol, self._tokens_json)
        try:
            code, data = self._request("GET", f"/api/v4/spot/tickers/{pair}")
            if code == 200 and isinstance(data, dict):
                last = float(data.get("last") or 0)
                if last > 0:
                    return last
        except Exception as e:
            print(f"[DIAG] CEX price gate ticker error {pair}: {e}", file=sys.stderr, flush=True)
        try:
            t = fetch_ticker(okx_ticker(symbol, self._tokens_json))
            px = float(t.get("last") or 0)
            if px > 0:
                return px
        except Exception as e:
            print(f"[DIAG] CEX price okx ticker error {symbol}: {e}", file=sys.stderr, flush=True)
        return 0.0

    def _balances(self) -> dict[str, dict]:
        """Available+locked balances keyed by currency (this broker's account)."""
        try:
            return fetch_spot_balances(self._api_key, self._api_secret)
        except Exception as e:
            print(f"[DIAG] CEX broker balances error: {e}", file=sys.stderr, flush=True)
            return {}

    # ═══════════════════════════════════════════════════════════
    #  Order Execution
    # ═══════════════════════════════════════════════════════════

    def _submit_order(self, order) -> Any:
        """Submit a market order on Gate CEX.

        Market order amount is the base-currency quantity (e.g. 0.05 CRCLX).
        Confirmation: POST → query once → filled/pending/failed.
        """
        symbol = order.asset.symbol
        side = str(order.side).lower()
        # Keep decimals: explicit quantity (e.g. 0.05) must not be truncated.
        quantity = float(order.quantity)

        if side not in ("buy", "sell"):
            order.set_error(f"Unsupported side: {side} (only buy/sell)")
            return order
        if quantity <= 0:
            order.set_error(f"Invalid quantity: {quantity} ({symbol})")
            return order

        pair = gate_pair(symbol, self._tokens_json)

        # ── Pair-level pre-flight (fail-closed, explicit errors) ─────
        meta = self._pair_meta(pair)
        if not meta:
            msg = f"Gate {pair} pair metadata unavailable — refusing to place order"
            order.set_error(msg)
            print(f"CEX BROKER DIAG | submit REJECT {side} {quantity} {symbol}@{pair}: {msg}",
                  file=sys.stderr, flush=True)
            return order
        status = meta.get("trade_status", "")
        if status and status != "tradable":
            order.set_error(f"Gate pair {pair} not tradable (trade_status={status})")
            return order
        ap = int(meta.get("amount_precision") or 0)  # base 数量精度（market SELL）
        qp = int(meta.get("quote_precision") or 0)   # quote 金额精度（market BUY）
        min_quote = float(meta.get("min_quote_amount") or 0)

        # Gate market order semantics (官方 spec): buy → amount = quote 金额 (USDT),
        # sell → amount = base 数量 (CRCLX).
        px = self._price_of(symbol)
        if side == "buy":
            if px <= 0:
                msg = f"Gate {pair} cannot place market buy: no price for {symbol}"
                order.set_error(msg)
                print(f"CEX BROKER DIAG | submit REJECT {side} {quantity} {symbol}@{pair}: {msg}",
                      file=sys.stderr, flush=True)
                return order
            if min_quote > 0 and quantity * px < min_quote:
                msg = (
                    f"Gate {pair} below min order amount {min_quote:g} USDT "
                    f"(qty {quantity} x {px:.2f} = ${quantity * px:.2f})"
                )
                order.set_error(msg)
                print(f"CEX BROKER DIAG | submit REJECT {side} {quantity} {symbol}@{pair}: {msg}",
                      file=sys.stderr, flush=True)
                return order
            amount_str = f"{quantity * px:.{qp}f}" if qp > 0 else f"{quantity * px:.8f}"
        else:
            if min_quote > 0 and px > 0 and quantity * px < min_quote:
                msg = (
                    f"Gate {pair} below min order amount {min_quote:g} USDT "
                    f"(qty {quantity} x {px:.2f} = ${quantity * px:.2f})"
                )
                order.set_error(msg)
                print(f"CEX BROKER DIAG | submit REJECT {side} {quantity} {symbol}@{pair}: {msg}",
                      file=sys.stderr, flush=True)
                return order
            amount_str = f"{quantity:.{ap}f}" if ap > 0 else f"{quantity:.8f}"

        client_oid = f"nq{int(time.time())}{os.urandom(3).hex()}"
        body = json.dumps({
            "currency_pair": pair,
            "side": side,
            "type": "market",
            "amount": amount_str,
            "time_in_force": "ioc",
            "text": f"t-{client_oid}",
        }, separators=(",", ":"))

        code, data = self._request("POST", _SPOT_ORDERS_PATH, body=body)
        if code != 200:
            msg = self._format_err(f"Gate create_order failed: {pair} {side} {quantity}", data)
            order.set_error(msg)
            print(f"CEX BROKER DIAG | submit FAIL {side} {quantity} {symbol}@{pair}: {msg}",
                  file=sys.stderr, flush=True)
            return order

        oid = str(data.get("id") or "")
        order.set_identifier(oid)
        order.custom_params = order.custom_params or {}
        order.custom_params["cex"] = {
            "pair": pair,
            "client_order_id": client_oid,
            "sub_account": self._uid,
        }

        # ── status confirmation ──────────────────────────────────
        status, filled, left, avg = self._query_order(oid, pair)
        print(
            f"CEX BROKER DIAG | submit {side} {quantity} {symbol}@{pair} "
            f"oid={oid[:12]} status={status} filled={filled} left={left} avg={avg}",
            file=sys.stderr, flush=True,
        )
        if status == "filled":
            order.set_filled()
            self._tracked[oid] = {
                "symbol": symbol, "pair": pair, "side": side,
                "quantity": quantity, "filled": filled,
                "avg_price": avg, "ts": time.time(),
            }
        elif status in ("cancelled", "failed", "expired"):
            order.set_error(f"Gate order {oid} {status} (left={left})")
        # status == "submitted"/"open": pending — strategy polls _pull_broker_order
        return order

    def cancel_order(self, order) -> None:
        """Cancel an open order (CEX supports cancellation of unfilled remainder)."""
        oid = str(getattr(order, "identifier", "") or "")
        info = self._tracked.get(oid, {})
        pair = info.get("pair", "")
        if not oid or not pair:
            return
        try:
            code, data = self._request(
                "DELETE", f"{_SPOT_ORDERS_PATH}/{oid}", query=f"currency_pair={pair}"
            )
            if code != 200:
                logger.debug("cancel_order %s failed: %s %s", oid, code, data)
        except Exception as e:
            logger.debug("cancel_order %s error: %s", oid, e)

    def _modify_order(self, order, limit_price=None, stop_price=None, quantity=None):
        """No-op: market orders cannot be amended."""
        return

    # ═══════════════════════════════════════════════════════════
    #  Balances & Positions
    # ═══════════════════════════════════════════════════════════

    def _get_balances_at_broker(self, quote_asset, strategy) -> tuple[float, float, float]:
        """Return (cash, positions_value, total) in the quote currency (USDT)."""
        balances = self._balances()
        if not balances:
            return (0.0, 0.0, 0.0)
        quote = quote_asset.symbol if quote_asset else "USDT"
        cash = 0.0
        positions_value = 0.0
        for cur, b in balances.items():
            qty = b["available"] + b["locked"]
            if qty <= 0:
                continue
            if cur in _CASH_CURRENCIES:
                cash += qty
            else:
                price = self._price_of(cur)
                positions_value += qty * price
        return (cash, positions_value, cash + positions_value)

    def _pull_positions(self, strategy) -> list:
        """Positions = non-cash balances; priced via OKX CEX (data side)."""
        balances = self._balances()
        if not balances:
            return []
        from lumibot.entities import Asset, Position
        positions = []
        for cur, b in balances.items():
            qty = b["available"] + b["locked"]
            if qty <= 0 or cur in _CASH_CURRENCIES:
                continue
            pos = Position(
                strategy=strategy,
                asset=Asset(symbol=cur, asset_type="crypto"),
                quantity=qty,
            )
            pos.current_price = self._price_of(cur)
            positions.append(pos)
        return positions

    def _pull_position(self, strategy, asset):
        positions = self._pull_positions(strategy)
        for p in positions:
            if p.asset.symbol == asset.symbol:
                return p
        return None

    # ═══════════════════════════════════════════════════════════
    #  Order Reconciliation
    # ═══════════════════════════════════════════════════════════

    def _pull_broker_order(self, identifier: str) -> Any:
        """Reconstitute a lumibot Order from a Gate order id."""
        info = self._tracked.get(identifier, {})
        pair = info.get("pair", "")
        if not pair:
            return None
        status, filled, left, avg = self._query_order(identifier, pair)
        return self._parse_broker_order(
            {"id": identifier, "status": status, "filled": filled, "left": left,
             "avg_price": avg, "symbol": info.get("symbol", ""),
             "side": info.get("side", "buy"), "quantity": info.get("quantity", 0)},
            strategy_name=None, strategy_object=None,
        )

    def _parse_broker_order(self, response: dict, strategy_name, strategy_object) -> Any:
        from lumibot.entities import Asset, Order
        oid = str(response.get("id") or "")
        status = str(response.get("status") or "submitted")
        symbol = str(response.get("symbol") or "")
        side = str(response.get("side") or "buy")
        quantity = float(response.get("quantity") or 0)
        filled = float(response.get("filled") or 0)
        order = Order(
            strategy=strategy_name or "cex",
            identifier=oid,
            asset=Asset(symbol=symbol, asset_type="crypto") if symbol else None,
            quantity=quantity,
            side=side,
            status=status,
        )
        if status == "filled":
            order.set_filled()
        elif status == "cancelled":
            order.set_canceled()
        return order

    def _pull_broker_all_orders(self) -> list:
        return []

    # ═══════════════════════════════════════════════════════════
    #  Streaming (poll-based, matching OnchainOSBroker pattern)
    # ═══════════════════════════════════════════════════════════

    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        pass

    def _run_stream(self):
        pass

    def get_historical_account_value(self):
        return {}
