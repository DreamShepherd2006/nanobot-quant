"""Portfolio Engine — position sizing and order construction.

Sits between Risk Engine (gate checks) and lumibot (execution).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nanobot_quant.portfolio.order_schema import OrderRequest

if TYPE_CHECKING:
    from lumibot.strategies.strategy import Strategy


class PortfolioEngine:
    """Position sizing & structured order creation.

    Responsibilities
    ----------------
    * Calculate how many shares to buy (fixed quantity or % of portfolio).
    * Build typed :class:`OrderRequest` objects from signal + risk result.
    * Submit orders to the lumibot strategy and return the broker order.

    Example
    -------
    ::

        pe = PortfolioEngine(strategy, max_position_pct=0.20)
        req = pe.build_buy_order("AAPL", 150.0, "TD buy setup=9")
        pe.submit_order(req)
    """

    def __init__(
        self,
        strategy: Strategy,
        max_position_pct: float = 0.20,
        default_quantity: int | None = None,
    ) -> None:
        self._strategy = strategy
        self.max_position_pct = max_position_pct
        self.default_quantity = default_quantity

    # ── position sizing ──────────────────────────────────────────

    def calculate_quantity(
        self, price: float, quantity: int | None = None
    ) -> int:
        """Return the number of shares to trade.

        Priority: *quantity* arg > *default_quantity* > % of portfolio value.
        """
        if quantity is not None and quantity > 0:
            return quantity
        if self.default_quantity:
            return self.default_quantity
        pv = self.portfolio_value
        return max(int(pv * self.max_position_pct / price), 1)

    # ── order builders ───────────────────────────────────────────

    def build_buy_order(
        self,
        symbol: str,
        price: float,
        reason: str = "",
        quantity: int | None = None,
    ) -> OrderRequest:
        """Build a buy order with position-sizing applied."""
        qty = self.calculate_quantity(price, quantity)
        return OrderRequest(
            asset=symbol, action="buy", quantity=qty,
            price=price, reason=reason,
        )

    def build_sell_order(
        self,
        symbol: str,
        price: float,
        reason: str = "",
        quantity: int | None = None,
    ) -> OrderRequest:
        """Build a sell order. Sells full position unless *quantity* is given."""
        if quantity is not None and quantity > 0:
            qty = quantity
        else:
            pos = self._strategy.get_position(symbol)
            qty = int(pos.quantity) if pos else (self.default_quantity or 1)
        return OrderRequest(
            asset=symbol, action="sell", quantity=qty,
            price=price, reason=reason,
        )

    # ── execution ────────────────────────────────────────────────

    def submit_order(self, request: OrderRequest):
        """Create a lumibot order from the request and submit it.

        Returns the lumibot order object (for logging / tracking).
        """
        order = self._strategy.create_order(
            request.asset, request.quantity, request.action
        )
        self._strategy.submit_order(order)
        return order

    # ── position snapshot ────────────────────────────────────────

    def get_position(self, symbol: str) -> dict | None:
        """Return a position summary dict or ``None`` if no position."""
        pos = self._strategy.get_position(symbol)
        if not pos:
            return None
        return {
            "symbol": symbol,
            "quantity": int(pos.quantity),
            "avg_fill_price": pos.avg_fill_price,
            "unrealized_pnl": getattr(pos, "unrealized_profit_loss", None),
        }

    @property
    def portfolio_value(self) -> float:
        return self._strategy.portfolio_value

    @property
    def cash(self) -> float:
        return self._strategy.cash
