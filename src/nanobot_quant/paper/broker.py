"""Paper Broker — simulated brokerage with JSON persistence.

Zero external dependencies. Tracks cash, positions, trade history.
All state survives restarts via a JSON file on disk.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot_quant.portfolio.order_schema import OrderRequest, OrderResponse


# ── Data structures ────────────────────────────────────────────────

@dataclass
class Position:
    """A single open position tracked by the broker."""
    ticker: str
    quantity: int
    entry_price: float  # average fill price
    opened_at: str = ""  # ISO timestamp

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price

    def market_value(self, current_price: float) -> float:
        return self.quantity * current_price

    def pnl(self, current_price: float) -> float:
        return self.market_value(current_price) - self.cost_basis


@dataclass
class Trade:
    """A completed (filled) trade record."""
    id: str = ""
    ticker: str = ""
    action: str = ""      # buy / sell
    quantity: int = 0
    price: float = 0.0
    filled_at: str = ""   # ISO
    reason: str = ""


@dataclass
class BrokerState:
    cash: float = 100000.0
    initial_cash: float = 100000.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    _trade_counter: int = 0

    def equity(self, prices: dict[str, float] | None = None) -> float:
        """Total equity = cash + market value of all positions."""
        p = prices or {}
        return self.cash + sum(
            pos.market_value(p.get(tkr, pos.entry_price))
            for tkr, pos in self.positions.items()
        )

    @property
    def total_return_pct(self) -> float:
        return (self.equity() - self.initial_cash) / self.initial_cash * 100


# ── Broker ─────────────────────────────────────────────────────────

class PaperBroker:
    """Simulated broker with JSON file persistence.

    Usage::

        broker = PaperBroker("/data/paper/state.json")
        broker.deposit(100000)
        order = OrderRequest(asset="AAPL", action="buy", quantity=10, ...)
        resp = broker.execute(order, current_price=150.0)
        broker.report(prices={"AAPL": 150.0})

    """

    def __init__(self, state_path: str = "paper_state.json") -> None:
        self._path = Path(state_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    # ── public API ──────────────────────────────────────────────

    def deposit(self, amount: float) -> float:
        """Add cash. Returns new balance."""
        self.state.cash += amount
        self.state.initial_cash += amount
        self._save()
        return self.state.cash

    def execute(self, order: OrderRequest, current_price: float) -> OrderResponse:
        """Simulate fill at current_price. No slippage or commission."""
        if not order.validate():
            return OrderResponse.failure(order.id, "invalid order")

        if order.action == "buy":
            return self._buy(order, current_price)
        if order.action == "sell":
            return self._sell(order, current_price)
        return OrderResponse.failure(order.id, f"unknown action: {order.action}")

    def get_position(self, ticker: str) -> Position | None:
        return self.state.positions.get(ticker.upper())

    def report(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Return a summary dict for Neo / display."""
        p = prices or {}
        positions = [
            {
                "ticker": tkr,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "current_price": p.get(tkr, pos.entry_price),
                "pnl": round(pos.pnl(p.get(tkr, pos.entry_price)), 2),
                "pnl_pct": round(
                    (p.get(tkr, pos.entry_price) - pos.entry_price) / pos.entry_price * 100, 1
                ),
            }
            for tkr, pos in self.state.positions.items()
        ]
        return {
            "cash": round(self.state.cash, 2),
            "equity": round(self.state.equity(p), 2),
            "initial_cash": self.state.initial_cash,
            "total_return_pct": round(self.state.total_return_pct, 2),
            "positions": positions,
            "trade_count": len(self.state.trades),
        }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent trades as dicts."""
        return [asdict(t) for t in self.state.trades[-limit:]]

    def reset(self) -> None:
        """Wipe all state and start fresh."""
        self.state = BrokerState()
        self._save()

    def _save(self) -> None:
        self._path.write_text(json.dumps(asdict(self.state), indent=2, default=str))

    def _load(self) -> BrokerState:
        if not self._path.exists():
            return BrokerState()
        try:
            raw = json.loads(self._path.read_text())
            return BrokerState(
                cash=raw.get("cash", 100000),
                initial_cash=raw.get("initial_cash", 100000),
                positions={
                    k: Position(**v) for k, v in raw.get("positions", {}).items()
                },
                trades=[Trade(**t) for t in raw.get("trades", [])],
                _trade_counter=raw.get("_trade_counter", 0),
            )
        except (json.JSONDecodeError, TypeError):
            return BrokerState()

    # ── internal ────────────────────────────────────────────────

    def _next_id(self) -> str:
        self.state._trade_counter += 1
        return f"T{self.state._trade_counter:04d}"

    def _buy(self, order: OrderRequest, price: float) -> OrderResponse:
        qty = order.quantity or 1
        cost = qty * price
        if cost > self.state.cash:
            return OrderResponse.failure(order.id, f"insufficient cash: need {cost:.2f}, have {self.state.cash:.2f}")

        self.state.cash -= cost
        tkr = order.asset.upper()
        now = datetime.now(timezone.utc).isoformat()

        if tkr in self.state.positions:
            old = self.state.positions[tkr]
            total_qty = old.quantity + qty
            avg = (old.cost_basis + cost) / total_qty
            self.state.positions[tkr] = Position(
                ticker=tkr, quantity=total_qty, entry_price=avg, opened_at=old.opened_at,
            )
        else:
            self.state.positions[tkr] = Position(
                ticker=tkr, quantity=qty, entry_price=price, opened_at=now,
            )

        trade = Trade(id=self._next_id(), ticker=tkr, action="buy",
                      quantity=qty, price=price, filled_at=now, reason=order.reason)
        self.state.trades.append(trade)
        self._save()

        return OrderResponse.ok(order.id, filled_price=price, filled_quantity=qty,
                                asset=order.asset, action="buy", filled_at=now)

    def _sell(self, order: OrderRequest, price: float) -> OrderResponse:
        tkr = order.asset.upper()
        pos = self.state.positions.get(tkr)
        if not pos:
            return OrderResponse.failure(order.id, f"no position for {tkr}")

        qty = order.quantity or pos.quantity
        if qty > pos.quantity:
            qty = pos.quantity  # clamp to available

        proceeds = qty * price
        self.state.cash += proceeds
        now = datetime.now(timezone.utc).isoformat()

        remaining = pos.quantity - qty
        if remaining <= 0:
            del self.state.positions[tkr]
        else:
            self.state.positions[tkr] = Position(
                ticker=tkr, quantity=remaining, entry_price=pos.entry_price,
                opened_at=pos.opened_at,
            )

        trade = Trade(id=self._next_id(), ticker=tkr, action="sell",
                      quantity=qty, price=price, filled_at=now, reason=order.reason)
        self.state.trades.append(trade)
        self._save()

        return OrderResponse.ok(order.id, filled_price=price, filled_quantity=qty,
                                asset=order.asset, action="sell", filled_at=now)
