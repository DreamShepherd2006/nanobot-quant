"""Event Bus — in-process pub/sub for nanobot-quant workflow.

Lightweight, zero-dependency. Uses Python ``queue.Queue`` backend;
designed to swap to NATS later without changing the subscriber interface.

Key events (in pipeline order):

    SignalCreated → SignalRouted → RiskChecked → AllocationDecided →
    OrderSubmitted → ExecutionReport → PositionUpdated

Usage::

    bus = EventBus()

    @bus.on("signal.*")
    def log_signal(event):
        print(f"[{event.type}] {event.ticker}")

    pipeline = AnalysisPipeline(event_bus=bus)
    pipeline.run(["AAPL"])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from nanobot_quant.aggregator import RoutedSignal
from nanobot_quant.portfolio.order_schema import OrderRequest
from nanobot_quant.signal_schema import TickerSignal


# ── Event types ──────────────────────────────────────────────────────

@dataclass
class SignalCreatedEvent:
    """Emitted when a raw TD Sequential signal is computed for a ticker."""

    type: str = field(default="signal.created", init=False)
    ticker: str = ""
    signal: TickerSignal | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SignalRoutedEvent:
    """Emitted after aggregation — one event per routed signal."""

    type: str = field(default="signal.routed", init=False)
    routed: RoutedSignal | None = None
    ticker: str = ""
    recommendation: str = ""
    score: float = 0.0
    conflict: bool = False
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class RiskCheckedEvent:
    """Emitted after risk-engine gates are applied to a ticker."""

    type: str = field(default="risk.checked", init=False)
    ticker: str = ""
    passed: bool = False
    details: Dict[str, str] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class AllocationDecidedEvent:
    """Emitted when position size is calculated."""

    type: str = field(default="allocation.decided", init=False)
    ticker: str = ""
    quantity: int = 0
    position_value: float = 0.0
    portfolio_value: float = 0.0
    allocation_pct: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class OrderSubmittedEvent:
    """Emitted when an order request is generated (not yet executed)."""

    type: str = field(default="order.submitted", init=False)
    order: OrderRequest | None = None
    ticker: str = ""
    action: str = ""
    quantity: int = 0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ExecutionReportEvent:
    """Emitted when a broker/backtest reports order fill status.

    Used by: Lumibot paper broker, OnchainOS tx confirmation (future).
    """

    type: str = field(default="execution.report", init=False)
    order_id: str = ""
    asset: str = ""
    status: str = "pending"   # pending / filled / cancelled / rejected
    filled_quantity: int = 0
    filled_price: float = 0.0
    message: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class PositionUpdatedEvent:
    """Emitted when a position changes (opened, closed, adjusted)."""

    type: str = field(default="position.updated", init=False)
    asset: str = ""
    quantity: int = 0
    avg_price: float = 0.0
    market_value: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# Type alias for any event
Event = (
    SignalCreatedEvent
    | SignalRoutedEvent
    | RiskCheckedEvent
    | AllocationDecidedEvent
    | OrderSubmittedEvent
    | ExecutionReportEvent
    | PositionUpdatedEvent
)


# ── Bus ──────────────────────────────────────────────────────────────

class EventBus:
    """In-process pub/sub event bus.

    Subscribers register with an event type pattern (exact or ``"*"``
    suffix for wildcards).  Publish delivers synchronously to every
    matching handler.

    Thread-safe for publish; subscriptions should be set up before
    the pipeline runs.
    """

    def __init__(self) -> None:
        self._subs: Dict[str, List[Callable[[Any], None]]] = {}

    # ── public API ───────────────────────────────────────────────

    def subscribe(self, event_type: str, handler: Callable[[Any], None]) -> None:
        """Register *handler* for *event_type*.

        ``event_type`` may be an exact string (``"signal.created"``)
        or end with ``"*"`` (``"signal.*"`` matches all signal events).
        """
        self._subs.setdefault(event_type, []).append(handler)

    def on(self, event_type: str) -> Callable:
        """Decorator form of :meth:`subscribe`."""

        def decorator(handler: Callable[[Any], None]) -> Callable[[Any], None]:
            self.subscribe(event_type, handler)
            return handler

        return decorator

    def unsubscribe(self, event_type: str, handler: Callable[[Any], None]) -> None:
        """Remove a previously registered *handler*."""
        subs = self._subs.get(event_type, [])
        if handler in subs:
            subs.remove(handler)

    def publish(self, event: Any) -> None:
        """Deliver *event* to all matching subscribers.

        Matching rules:
        - Exact key match
        - Wildcard: ``"signal.*"`` matches ``"signal.created"``,
          ``"signal.routed"``, etc.
        """
        ev_type: str = getattr(event, "type", "")
        if not ev_type:
            return

        handlers: set[Callable[[Any], None]] = set()

        # exact match
        for h in self._subs.get(ev_type, []):
            handlers.add(h)

        # wildcard matches
        for pattern, h_list in self._subs.items():
            if pattern.endswith(".*") and ev_type.startswith(pattern[:-2]):
                for h in h_list:
                    handlers.add(h)

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                # silently swallow — one subscriber should not break others
                pass

    # ── introspection ────────────────────────────────────────────

    @property
    def subscriber_count(self) -> int:
        """Total number of subscriber entries (not unique handlers)."""
        return sum(len(v) for v in self._subs.values())

    @property
    def topics(self) -> List[str]:
        """Registered topic patterns."""
        return sorted(self._subs.keys())


# ── singleton convenience ───────────────────────────────────────────

_default_bus: EventBus | None = None


def get_default_bus() -> EventBus:
    """Return a process-wide singleton :class:`EventBus`."""
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBus()
    return _default_bus
