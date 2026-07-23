"""Paper Runner — periodic market → TD → Risk → Broker execution loop.

Invokable via relay::

    runner = PaperRunner(broker, watchlist=["AAPL", "TSLA"])
    summary = runner.tick()  # one cycle
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from nanobot_quant.paper.broker import PaperBroker
from nanobot_quant.portfolio.order_schema import OrderRequest
from nanobot_quant.risk import RiskEngine
from nanobot_quant.strategies.td_sequential import calculate


class PaperRunner:
    """Runs one execution cycle: fetch prices, evaluate, submit orders.

    Args:
        broker: A :class:`PaperBroker` instance.
        watchlist: Tickers to scan for entry signals.
        max_position_pct: Max % of equity per position.
        stop_loss_pct: Stop-loss threshold.
        period: yfinance period for TD signal calculation.
    """

    def __init__(
        self,
        broker: PaperBroker,
        watchlist: list[str] | None = None,
        max_position_pct: float = 0.20,
        max_drawdown_pct: float = 0.15,
        stop_loss_pct: float = 0.10,
        period: str = "6mo",
    ) -> None:
        self._broker = broker
        self._risk = RiskEngine(
            max_position_pct=max_position_pct,
            max_drawdown_pct=max_drawdown_pct,
            stop_loss_pct=stop_loss_pct,
        )
        self._watchlist = [t.upper() for t in (watchlist or [])]
        self._period = period
        self._max_position_pct = max_position_pct

    # ── public ───────────────────────────────────────────────────

    def tick(self) -> dict[str, Any]:
        """Run one execution cycle. Returns a summary dict."""
        orders: list[dict[str, Any]] = []
        exits: list[dict[str, Any]] = []

        # 1. collect all tickers we care about
        open_tickers = list(self._broker.state.positions.keys())
        all_tickers = sorted(set(open_tickers + self._watchlist))
        if not all_tickers:
            return {"message": "no tickers to scan", "orders": [], "exits": []}

        # 2. fetch latest prices (batch download)
        prices = self._fetch_prices(all_tickers)
        equity = self._broker.state.equity(prices)

        # 3. check exits for open positions
        for tkr in open_tickers:
            exit_order = self._check_exit(tkr, prices.get(tkr, 0))
            if exit_order:
                resp = self._broker.execute(exit_order, prices[tkr])
                exits.append({"ticker": tkr, "action": "sell", "reason": exit_order.reason,
                              "filled": resp.success, "detail": resp.message})

        # 4. check entries for watchlist
        for tkr in self._watchlist:
            if tkr in open_tickers:
                continue  # already have position
            entry_order = self._check_entry(tkr, prices.get(tkr, 0), equity)
            if entry_order:
                resp = self._broker.execute(entry_order, prices[tkr])
                orders.append({"ticker": tkr, "action": "buy", "reason": entry_order.reason,
                               "filled": resp.success, "detail": resp.message})

        # 5. report
        return {
            "date": datetime.now(timezone.utc).isoformat(),
            "prices_snapshot": {t: prices.get(t) for t in all_tickers},
            "orders": orders,
            "exits": exits,
            "portfolio": self._broker.report(prices),
        }

    # ── internal ─────────────────────────────────────────────────

    def _fetch_prices(self, tickers: list[str]) -> dict[str, float]:
        """Batch-fetch latest close prices."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                data = yf.download(
                    tickers, period="5d", auto_adjust=True,
                    progress=False, group_by="ticker",
                )
            except Exception:
                return {}

        prices: dict[str, float] = {}
        if data.empty:
            return prices
        for tkr in tickers:
            try:
                if len(tickers) == 1:
                    close = data["Close"]
                else:
                    close = data[tkr]["Close"] if tkr in data else None
                if close is not None and not close.empty:
                    prices[tkr] = float(close.iloc[-1])
            except (KeyError, IndexError, TypeError):
                pass
        return prices

    def _fetch_td(self, ticker: str) -> dict[str, Any] | None:
        """Run TD Sequential on a single ticker."""
        try:
            df = yf.download(ticker, period=self._period, auto_adjust=True,
                             progress=False)
            if df.empty:
                return None
            return calculate(df)
        except Exception:
            return None

    def _check_exit(self, ticker: str, price: float) -> OrderRequest | None:
        """Check if we should exit an open position."""
        pos = self._broker.get_position(ticker)
        if not pos or price <= 0:
            return None

        # stop-loss
        sl = self._risk.should_exit(current_price=price, entry_price=pos.entry_price)
        if sl.approved:
            return OrderRequest(
                asset=ticker, action="sell", quantity=pos.quantity,
                order_type="market", price=price, reason=sl.reason,
            )

        # TD sell signal
        td = self._fetch_td(ticker)
        if td and td.get("recommendation") == "SELL":
            return OrderRequest(
                asset=ticker, action="sell", quantity=pos.quantity,
                order_type="market", price=price,
                reason=f"TD SELL: cd_sell={td.get('cd_sell')} score={td.get('score')}",
            )

        return None

    def _check_entry(
        self, ticker: str, price: float, equity: float,
    ) -> OrderRequest | None:
        """Check if we should enter a new position."""
        if price <= 0 or equity <= 0:
            return None

        # risk gate: can we enter?
        qty = max(int(equity * self._max_position_pct / price), 1)
        pv = price * qty
        gate = self._risk.can_enter(
            position_value=pv, portfolio_value=equity, peak_portfolio=equity,
        )
        if not gate.approved:
            return None

        # TD signal
        td = self._fetch_td(ticker)
        if not td:
            return None
        rec = td.get("recommendation", "")
        score = td.get("score", 0)

        if rec == "BUY" and score and score > 0:
            return OrderRequest(
                asset=ticker, action="buy", quantity=qty,
                order_type="market", price=price,
                reason=f"TD BUY: setup_buy={td.get('setup_buy')} cd_buy={td.get('cd_buy')} score={score}",
            )

        return None
