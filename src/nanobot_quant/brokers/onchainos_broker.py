"""Lumibot Broker that executes trades via onchainos DEX aggregator.

Implements all 13 abstract methods of ``lumibot.brokers.Broker``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from nanobot_quant.onchainos_swap import (
    resolve_token_address,
    swap_execute,
    swap_status,
    get_wallet_balance,
    get_token_price,
    WSOL_ADDR,
)
from lumibot.brokers import Broker

logger = logging.getLogger("nanobot_quant.brokers.onchainos")


class OnchainOSBroker(Broker):
    """Lumibot Broker that routes orders through onchainos DEX aggregator.

    Parameters:
        tokens_json: User-configured token mappings from tokens.json.
            Each entry: ``{"symbol": "...", "address": "...", "chain": "solana"}``.
        slippage: Default slippage tolerance (0.01 = 1%).
        sol_buffer_pct: Extra SOL buffer on buy orders to absorb price movement
            between quote and execution (default 0.05 = 5%).
    """

    def __init__(
        self,
        tokens_json: list[dict] | None = None,
        slippage: str = "0.01",
        sol_buffer_pct: float = 0.05,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []
        self._slippage = slippage
        self._sol_buffer_pct = sol_buffer_pct
        self._tracked: dict[str, dict] = {}  # tx_hash → order meta

    # ═══════════════════════════════════════════════════════════════
    #  Order Execution
    # ═══════════════════════════════════════════════════════════════

    def _submit_order(self, order) -> Any:
        """Submit a market order via onchainos swap.

        Flow:
        1. Resolve token addresses (user config → CLI fallback)
        2. Determine from_amount (exact for sells, estimated for buys)
        3. ``onchainos swap execute`` → txHash
        4. Update order with txHash + filled status
        """
        symbol = order.asset.symbol
        side = order.side.lower()
        quantity = int(order.quantity)

        if side not in ("buy", "sell"):
            order.update_status("rejected")
            logger.error("Unsupported side: %s (only buy/sell)", side)
            return order

        # ── token resolution ──────────────────────────────────
        if side == "buy":
            from_symbol, to_symbol = "SOL", symbol
        else:
            from_symbol, to_symbol = symbol, "SOL"

        from_addr = resolve_token_address(from_symbol, self._tokens_json)
        to_addr = resolve_token_address(to_symbol, self._tokens_json)

        if not from_addr or not to_addr:
            order.update_status("rejected")
            logger.error(
                "Cannot resolve addresses: %s→%s", from_symbol, to_symbol
            )
            return order

        # ── amount calculation ─────────────────────────────────
        if side == "sell":
            from_amount = str(quantity)
        else:
            # Buy: estimate SOL needed via market price + buffer
            sol_price = get_token_price(from_addr) or 1.0
            token_price = get_token_price(to_addr) or 0.0
            if token_price <= 0:
                order.update_status("rejected")
                logger.error("Cannot get price for %s", symbol)
                return order
            sol_needed = (quantity * token_price / sol_price) * (1 + self._sol_buffer_pct)
            from_amount = f"{sol_needed:.6f}"

        # ── execute swap ───────────────────────────────────────
        result = swap_execute(from_addr, to_addr, from_amount, self._slippage)
        if not result:
            order.update_status("rejected")
            logger.error(
                "Swap execute failed: %s %s %s@%s",
                side, quantity, symbol, from_amount,
            )
            return order

        tx_hash = result.get("swapTxHash") or result.get("txHash", "")
        status = result.get("status", "unknown")

        if status not in ("success", "submitted", "pending"):
            order.update_status("rejected")
            logger.error("Swap returned status=%s tx=%s", status, tx_hash[:16])
            return order

        # ── fill order ─────────────────────────────────────────
        order.set_identifier(tx_hash)
        order.update_status("filled")

        to_amount = float(result.get("toAmount") or 0)
        fill_price = to_amount / quantity if quantity > 0 else 0

        self._tracked[tx_hash] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(
            "Swap filled: %s %s %s → tx=%s price=%.4f",
            side, quantity, symbol, tx_hash[:16], fill_price,
        )
        return order

    # ═══════════════════════════════════════════════════════════════
    #  Balance & Positions
    # ═══════════════════════════════════════════════════════════════

    def _get_balances_at_broker(self, quote_asset, strategy) -> tuple[float, float, float]:
        """Return (cash, positions_value, total_value) in USD."""
        balances = get_wallet_balance()
        if not balances:
            return (0.0, 0.0, 0.0)

        cash = 0.0
        positions_val = 0.0

        for t in balances:
            val = float(t.get("valueUsd") or 0)
            symb = t.get("symbol", "").upper()
            if symb == "SOL":
                cash = val
            else:
                positions_val += val

        return (cash, positions_val, cash + positions_val)

    def _pull_positions(self, strategy) -> list:
        """Return current positions from onchainos wallet."""
        balances = get_wallet_balance()
        if not balances:
            return []

        from lumibot.entities import Asset, Position

        positions = []
        for t in balances:
            symb = t.get("symbol", "")
            if symb.upper() == "SOL":
                continue
            bal = float(t.get("balance") or 0)
            price = float(t.get("price") or 0)
            if bal <= 0:
                continue
            positions.append(
                Position(
                    strategy=strategy,
                    asset=Asset(symbol=symb, asset_type="crypto"),
                    quantity=bal,
                    current_price=price,
                )
            )
        return positions

    def _pull_position(self, strategy, asset) -> Any:
        """Return position for a specific asset, or None."""
        for pos in self._pull_positions(strategy):
            if pos.asset.symbol.upper() == asset.symbol.upper():
                return pos
        return None

    # ═══════════════════════════════════════════════════════════════
    #  Order Tracking
    # ═══════════════════════════════════════════════════════════════

    def _pull_broker_order(self, identifier: str) -> Any:
        """Look up order status by txHash."""
        from lumibot.entities import Asset, Order

        info = self._tracked.get(identifier, {})
        result = swap_status(identifier)

        status = "filled"
        if result:
            raw_status = result.get("status", "success")
            if raw_status in ("pending", "submitted"):
                status = "submitted"
            elif raw_status != "success":
                status = "cancelled"

        return Order(
            strategy=None,
            identifier=identifier,
            asset=Asset(symbol=info.get("symbol", ""), asset_type="crypto"),
            quantity=info.get("quantity", 0),
            side=info.get("side", "buy"),
            status=status,
        )

    def _parse_broker_order(
        self, response, strategy_name: str, strategy_object
    ) -> Any:
        """Parse swap result dict into a Lumibot Order."""
        from lumibot.entities import Asset, Order

        tx_hash = response.get("swapTxHash") or response.get("txHash", "")
        return Order(
            strategy=strategy_object,
            identifier=tx_hash,
            asset=Asset(
                symbol=response.get("symbol", ""), asset_type="crypto"
            ),
            quantity=int(response.get("quantity", 0)),
            side=response.get("side", "buy"),
            status="filled",
        )

    def _pull_broker_all_orders(self) -> list:
        """On-chain DEX has no open order book — swaps are atomic."""
        return []

    # ═══════════════════════════════════════════════════════════════
    #  No-ops (chain constraints)
    # ═══════════════════════════════════════════════════════════════

    def cancel_order(self, order) -> None:
        """Solana swaps are atomic — cannot cancel once submitted."""
        logger.debug("cancel_order no-op for onchainos")
        return None

    def _modify_order(
        self,
        order,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        quantity: Optional[float] = None,
    ) -> None:
        """Solana swaps cannot be modified once submitted."""
        logger.debug("_modify_order no-op for onchainos")
        return None

    # ═══════════════════════════════════════════════════════════════
    #  Streaming (not used — strategy uses timer/sleep loop)
    # ═══════════════════════════════════════════════════════════════

    def _get_stream_object(self):
        return None

    def _register_stream_events(self):
        pass

    def _run_stream(self):
        pass

    # ═══════════════════════════════════════════════════════════════
    #  Historical (not applicable)
    # ═══════════════════════════════════════════════════════════════

    def get_historical_account_value(self) -> dict:
        return {}
