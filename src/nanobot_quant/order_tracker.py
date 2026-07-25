"""Order Tracker — Signal → lumibot Order lifecycle tracking.

Lumibot already manages orders via ``get_tracked_orders()``, lifecycle hooks
(``on_new_order``, ``on_filled_order``, etc.) and the CSV trades log.  This
module adds the missing link: associating each lumibot order with the Signal
that triggered it, and providing a clean query / trade-pairing interface.

Usage (integrated in :class:`TdSequentialStrategy`)::

    self.tracker = OrderTracker()
    ...
    order = self._portfolio.submit_order(req)
    self.tracker.track(order.identifier, symbol=..., action=..., ...)

At end of backtest::

    trades = tracker.get_trades()
    for t in trades:
        print(f"{t.symbol} BUY@{t.entry_price} SELL@{t.exit_price} PnL={t.pnl:.2f}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Tracked Order ────────────────────────────────────────────────────

@dataclass
class TrackedOrder:
    """A lumibot order enriched with our Signal context."""

    order_id: str
    symbol: str
    action: str                     # "buy" or "sell"
    quantity: int
    status: str                     # lumibot Order.OrderStatus value
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    tag: str = ""                   # our signal_id or summary
    signal: Dict[str, Any] = field(default_factory=dict)   # TickerSignal snapshot
    created_at: str = ""
    filled_at: str = ""
    reason: str = ""                # why the order was placed


# ── Trade Record (buy→sell pair) ────────────────────────────────────

@dataclass
class TradeRecord:
    """A completed round-trip trade (buy → sell) with P&L."""

    symbol: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    entry_reason: str = ""
    exit_reason: str = ""
    buy_order_id: str = ""
    sell_order_id: str = ""


# ── Order Tracker ───────────────────────────────────────────────────

class OrderTracker:
    """Track lumibot orders lifecycle and pair buys with sells.

    Attach to a lumibot Strategy.  Override the strategy's lifecycle hooks
    to call ``on_fill()`` and ``on_cancel()``, then call ``get_trades()``
    at the end to extract completed round-trips.
    """

    def __init__(self) -> None:
        self._orders: Dict[str, TrackedOrder] = {}   # order_id → tracked

    # ── write path ──────────────────────────────────────────────

    def track(
        self,
        order_id: str,
        symbol: str,
        action: str,
        quantity: int,
        status: str = "submitted",
        tag: str = "",
        signal: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ) -> TrackedOrder:
        """Register a new order for tracking.

        Called immediately after ``strategy.submit_order()``.
        If the order is already being tracked (e.g. from lifecycle hooks),
        updates the existing entry.
        """
        now = datetime.now(timezone.utc).isoformat()
        if order_id in self._orders:
            t = self._orders[order_id]
            if signal:
                t.signal.update(signal)
            if reason:
                t.reason = reason
            if tag:
                t.tag = tag
            return t

        t = TrackedOrder(
            order_id=order_id,
            symbol=symbol,
            action=action,
            quantity=quantity,
            status=status,
            tag=tag,
            signal=signal or {},
            created_at=now,
            reason=reason,
        )
        self._orders[order_id] = t
        return t

    def on_fill(
        self, order_id: str, filled_quantity: int, filled_price: float
    ) -> TrackedOrder | None:
        """Update an order when it (partially or fully) fills.

        Called from strategy's ``on_filled_order`` / ``on_partially_filled_order``.
        """
        t = self._orders.get(order_id)
        if not t:
            return None
        t.filled_quantity = max(t.filled_quantity, filled_quantity)
        t.avg_fill_price = filled_price
        if filled_quantity >= t.quantity:
            t.status = "fill"
        else:
            t.status = "partial_fill"
        t.filled_at = datetime.now(timezone.utc).isoformat()
        return t

    def on_cancel(self, order_id: str) -> TrackedOrder | None:
        """Mark an order as cancelled."""
        t = self._orders.get(order_id)
        if t:
            t.status = "canceled"
        return t

    # ── read path ───────────────────────────────────────────────

    def get_order(self, order_id: str) -> Optional[TrackedOrder]:
        return self._orders.get(order_id)

    def get_orders(self, status: str | None = None) -> List[TrackedOrder]:
        """Return tracked orders, optionally filtered by status."""
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status]
        return orders

    def get_trades(self) -> List[TradeRecord]:
        """Pair buy→sell orders into completed trades.

        Simple FIFO matching — assumes at most one open position per symbol
        (matches TdSequentialStrategy behavior).
        """
        # Group fills by symbol in time order
        fills = [
            o for o in self._orders.values()
            if o.status in ("fill", "partial_fill")
        ]
        fills.sort(key=lambda o: o.created_at)

        buys: List[TrackedOrder] = []  # open buy queue (FIFO)
        trades: List[TradeRecord] = []

        for o in fills:
            if o.action == "buy":
                buys.append(o)
            elif o.action == "sell":
                qty_to_match = o.filled_quantity
                while qty_to_match > 0 and buys:
                    buy = buys[0]
                    matched = min(qty_to_match, buy.filled_quantity)
                    if buy.avg_fill_price and buy.avg_fill_price > 0:
                        pnl = (o.avg_fill_price - buy.avg_fill_price) * matched
                        pnl_pct = (o.avg_fill_price / buy.avg_fill_price - 1) * 100
                    else:
                        pnl = 0.0
                        pnl_pct = 0.0
                    trades.append(TradeRecord(
                        symbol=o.symbol,
                        entry_time=buy.created_at,
                        exit_time=o.created_at,
                        entry_price=buy.avg_fill_price,
                        exit_price=o.avg_fill_price,
                        quantity=matched,
                        pnl=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 2),
                        entry_reason=buy.reason,
                        exit_reason=o.reason,
                        buy_order_id=buy.order_id,
                        sell_order_id=o.order_id,
                    ))
                    qty_to_match -= matched
                    remaining = buy.filled_quantity - matched
                    if remaining <= 0:
                        buys.pop(0)
                    else:
                        buy.filled_quantity = remaining

        return trades

    @property
    def order_count(self) -> int:
        return len(self._orders)

    @property
    def trade_count(self) -> int:
        return len(self.get_trades())

    def to_summary(self) -> str:
        """Human-readable tracker summary (for logs / agent review)."""
        trades = self.get_trades()
        if not trades:
            return "📊 OrderTracker: no completed trades."
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in trades)
        parts = [
            f"📊 OrderTracker: {len(trades)} trades",
            f"{len(wins)}W / {len(losses)}L",
            f"PnL={total_pnl:+.2f}",
        ]
        if trades:
            parts.append(f"WinRate={len(wins)/len(trades)*100:.0f}%")
        if wins:
            parts.append(f"AvgW={sum(t.pnl for t in wins)/len(wins):.2f}")
        if losses:
            parts.append(f"AvgL={sum(t.pnl for t in losses)/len(losses):.2f}")
        return " | ".join(parts)
