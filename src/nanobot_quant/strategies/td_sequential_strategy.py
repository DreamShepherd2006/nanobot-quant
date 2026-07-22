"""TD Sequential lumibot Strategy — backtest-ready trading rules.

Usage::

    from datetime import datetime
    from lumibot.backtesting import YahooDataBacktesting
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    result = TdSequentialStrategy.run_backtest(
        YahooDataBacktesting,
        datetime(2024, 1, 1),
        datetime(2025, 1, 1),
        parameters={"symbol": "AAPL", "quantity": 10},
    )
"""

from __future__ import annotations

from lumibot.strategies.strategy import Strategy

from nanobot_quant.strategies.td_sequential import calculate


class TdSequentialStrategy(Strategy):
    """A lumibot strategy that uses TD Sequential signals for trading.

    Trading rules (daily bars):
    1. LONG entry: setup_buy >= 9 AND score > 0 AND no position
    2. LONG exit:  setup_sell >= 9 OR cd_sell >= 13

    Parameters are passed via the ``parameters`` dict in ``run_backtest()``.
    """

    parameters = {
        "symbol": "AAPL",
        "quantity": 10,
        "max_position_pct": 0.20,   # max % of portfolio in one position
        "max_drawdown_pct": 0.15,   # skip new entries when drawdown > 15%
    }

    # ── lifecycle hooks ───────────────────────────────────────────

    def initialize(
        self,
        symbol: str | None = None,
        quantity: int | None = None,
        max_position_pct: float | None = None,
        max_drawdown_pct: float | None = None,
    ):
        """Called once before the backtest starts (lumibot lifecycle)."""
        self.symbol = symbol or self.parameters.get("symbol", "AAPL")
        self.quantity = quantity or self.parameters.get("quantity", 10)
        self._max_position_pct = max_position_pct or self.parameters.get("max_position_pct", 0.20)
        self._max_drawdown_pct = max_drawdown_pct or self.parameters.get("max_drawdown_pct", 0.15)
        self._bars_consumed = 0  # count of bars processed
        self._min_history = 50  # minimum bars TD Seq needs for meaningful signal
        self._peak_portfolio = None  # track peak for drawdown calc
        self.sleeptime = "1D"  # run once per trading day

    def on_trading_iteration(self):
        """Called for each bar (trading day) during the backtest.

        Fetches all available historical bars up to the current bar,
        calls ``calculate()`` for the latest TD Sequential signal,
        then creates buy/sell orders based on the rules above.
        """
        # ── 1. Fetch historical data ──
        try:
            bars = self.get_historical_prices(
                self.symbol,
                length=self._bars_consumed + self._min_history,
                timestep="day",
            )
        except Exception:
            self._bars_consumed += 1
            return

        if bars is None or bars.df.empty:
            self._bars_consumed += 1
            return

        df = bars.df.copy()
        self._bars_consumed += 1

        # ── 2. Ensure OHLCV columns ──
        col_map = {
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
        for src, dst in col_map.items():
            if src in df.columns and dst not in df.columns:
                df.rename(columns={src: dst}, inplace=True)

        required = {"Open", "High", "Low", "Close", "Volume"}
        if not required.issubset(set(df.columns)):
            self.logger.warning(f"Missing columns: {set(df.columns)}")
            return

        # ── 3. Run TD Sequential ──
        if len(df) < self._min_history:
            return

        signal = calculate(df)

        # ── 4. Evaluate signals ──
        setup_buy = signal.get("setup_buy", 0) or 0
        setup_sell = signal.get("setup_sell", 0) or 0
        cd_buy = signal.get("cd_buy", 0) or 0
        cd_sell = signal.get("cd_sell", 0) or 0
        score = signal.get("score", 0) or 0
        price = signal.get("price", 0) or 0

        has_position = self.get_position(self.symbol) is not None

        # ── Update peak portfolio for drawdown tracking ──
        pv = self.portfolio_value
        if self._peak_portfolio is None or pv > self._peak_portfolio:
            self._peak_portfolio = pv

        # ── BUY signal: setup_buy >= 9, positive score, no position ──
        if setup_buy >= 9 and score > 0 and not has_position:
            if not self._can_trade(price):
                return
            order = self.create_order(self.symbol, self.quantity, "buy")
            self.submit_order(order)
            self.logger.info(
                f"TD LONG  | price={price:.2f} setup_buy={setup_buy} "
                f"score={score:.1f} peak={self._peak_portfolio:.0f}"
            )

        # ── SELL signal: setup_sell >= 9 OR cd_sell >= 13 ──
        elif (setup_sell >= 9 or cd_sell >= 13) and has_position:
            order = self.create_order(self.symbol, self.quantity, "sell")
            self.submit_order(order)
            self.logger.info(
                f"TD EXIT  | price={price:.2f} setup_sell={setup_sell} "
                f"cd_sell={cd_sell}"
            )

    # ── Risk & Portfolio Helpers ──────────────────────────────────

    def _can_trade(self, price: float) -> bool:
        """Risk guard: returns True if both position sizing and drawdown pass."""
        pv = self.portfolio_value

        # 1. Position sizing: new position <= max_position_pct% of portfolio
        position_value = self.quantity * price
        if position_value > pv * self._max_position_pct:
            self.logger.info(
                f"TD BLOCK (size) | pos=${position_value:.0f} > "
                f"{self._max_position_pct*100:.0f}% of pv=${pv:.0f}"
            )
            return False

        # 2. Max drawdown: skip entries when underwater by too much
        peak = self._peak_portfolio or pv
        dd = (peak - pv) / peak if peak > 0 else 0
        if dd > self._max_drawdown_pct:
            self.logger.info(
                f"TD BLOCK (dd)   | drawdown={dd*100:.1f}% > "
                f"{self._max_drawdown_pct*100:.0f}% peak={peak:.0f}"
            )
            return False

        return True
