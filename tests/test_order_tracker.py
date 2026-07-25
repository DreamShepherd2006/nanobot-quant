"""Unit tests for OrderTracker (no lumibot dependency needed)."""

import pytest
from nanobot_quant.order_tracker import OrderTracker, TrackedOrder, TradeRecord


class TestOrderTracker:
    """Core tracking and trade-pairing logic."""

    def test_track_single_order(self):
        tracker = OrderTracker()
        t = tracker.track(
            order_id="ord-1", symbol="AAPL", action="buy",
            quantity=10, tag="signal:aap1",
            reason="TD LONG setup=9",
        )
        assert t.order_id == "ord-1"
        assert t.symbol == "AAPL"
        assert t.action == "buy"
        assert t.quantity == 10
        assert t.status == "submitted"
        assert t.tag == "signal:aap1"
        assert tracker.order_count == 1

    def test_track_idempotent(self):
        """Re-tracking the same order_id updates fields."""
        tracker = OrderTracker()
        tracker.track(order_id="ord-1", symbol="AAPL", action="buy", quantity=10)
        tracker.track(order_id="ord-1", symbol="AAPL", action="buy", quantity=10,
                      reason="updated reason")
        assert tracker.order_count == 1
        assert tracker.get_order("ord-1").reason == "updated reason"

    def test_on_fill_updates_status(self):
        tracker = OrderTracker()
        tracker.track(order_id="ord-1", symbol="AAPL", action="buy", quantity=10)
        t = tracker.on_fill("ord-1", filled_quantity=5, filled_price=150.0)
        assert t.status == "partial_fill"
        assert t.filled_quantity == 5

        t = tracker.on_fill("ord-1", filled_quantity=10, filled_price=150.0)
        assert t.status == "fill"
        assert t.filled_quantity == 10
        assert t.avg_fill_price == 150.0

    def test_on_fill_nonexistent(self):
        tracker = OrderTracker()
        t = tracker.on_fill("ghost", filled_quantity=1, filled_price=100.0)
        assert t is None

    def test_on_cancel(self):
        tracker = OrderTracker()
        tracker.track(order_id="ord-1", symbol="AAPL", action="buy", quantity=10)
        t = tracker.on_cancel("ord-1")
        assert t.status == "canceled"

    def test_get_orders_filtered(self):
        tracker = OrderTracker()
        tracker.track(order_id="ord-1", symbol="AAPL", action="buy", quantity=10)
        tracker.track(order_id="ord-2", symbol="AAPL", action="sell", quantity=10)
        tracker.on_fill("ord-1", filled_quantity=10, filled_price=150.0)
        tracker.on_fill("ord-2", filled_quantity=10, filled_price=160.0)

        fills = tracker.get_orders(status="fill")
        assert len(fills) == 2
        assert all(o.status == "fill" for o in fills)

        # ord-1 should still be "fill" not overwritten by subsequent operations
        assert tracker.get_order("ord-1").status == "fill"

    def test_get_trades_simple_buy_sell(self):
        """One buy, one sell → one trade."""
        tracker = OrderTracker()
        tracker.track(
            order_id="buy-1", symbol="AAPL", action="buy", quantity=10,
            reason="TD LONG", tag="signal:aap1",
        )
        tracker.on_fill("buy-1", filled_quantity=10, filled_price=150.0)

        tracker.track(
            order_id="sell-1", symbol="AAPL", action="sell", quantity=10,
            reason="TD EXIT", tag="signal:aap2",
        )
        tracker.on_fill("sell-1", filled_quantity=10, filled_price=160.0)

        trades = tracker.get_trades()
        assert len(trades) == 1
        t = trades[0]
        assert t.symbol == "AAPL"
        assert t.entry_price == 150.0
        assert t.exit_price == 160.0
        assert t.quantity == 10
        assert t.pnl == 100.0
        assert t.pnl_pct == pytest.approx(6.67, abs=0.1)
        assert t.entry_reason == "TD LONG"
        assert t.exit_reason == "TD EXIT"

    def test_get_trades_no_completed(self):
        """Only a buy, no sell → no trades."""
        tracker = OrderTracker()
        tracker.track(order_id="buy-1", symbol="AAPL", action="buy", quantity=10)
        tracker.on_fill("buy-1", filled_quantity=10, filled_price=150.0)

        trades = tracker.get_trades()
        assert len(trades) == 0

    def test_get_trades_sell_before_buy(self):
        """Chronological: buy-first must be before sell in time."""
        tracker = OrderTracker()
        # Sell fills first chronologically (out of order in registry)
        tracker.track(order_id="sell-1", symbol="AAPL", action="sell", quantity=10)
        tracker.on_fill("sell-1", filled_quantity=10, filled_price=160.0)

        tracker.track(order_id="buy-1", symbol="AAPL", action="buy", quantity=10)
        tracker.on_fill("buy-1", filled_quantity=10, filled_price=150.0)

        # FIFO: sell arrives first, no matching buy → no trade
        trades = tracker.get_trades()
        assert len(trades) == 0

    def test_to_summary(self):
        tracker = OrderTracker()
        tracker.track(order_id="buy-1", symbol="AAPL", action="buy", quantity=10)
        tracker.on_fill("buy-1", filled_quantity=10, filled_price=150.0)
        tracker.track(order_id="sell-1", symbol="AAPL", action="sell", quantity=10)
        tracker.on_fill("sell-1", filled_quantity=10, filled_price=155.0)

        s = tracker.to_summary()
        assert "1 trades" in s
        assert "1W / 0L" in s
        assert "WinRate=100%" in s

    def test_empty_summary(self):
        tracker = OrderTracker()
        assert "no completed trades" in tracker.to_summary()
