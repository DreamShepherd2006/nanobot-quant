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

from nanobot_quant.portfolio import PortfolioEngine
from nanobot_quant.risk import RiskEngine
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
        "stop_loss_pct": 0.10,      # exit when loss exceeds 10%
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
        self._bars_consumed = 0  # count of bars processed
        self._min_history = 50  # minimum bars TD Seq needs for meaningful signal
        self._peak_portfolio = None  # track peak for drawdown calc
        self.sleeptime = "1D"  # run once per trading day

        # Build RiskEngine from parameters
        self._risk = RiskEngine(
            max_position_pct=max_position_pct
            or self.parameters.get("max_position_pct", 0.20),
            max_drawdown_pct=max_drawdown_pct
            or self.parameters.get("max_drawdown_pct", 0.15),
            stop_loss_pct=self.parameters.get("stop_loss_pct", 0.10),
        )

        # Build PortfolioEngine for position sizing & order construction
        self._portfolio = PortfolioEngine(
            strategy=self,
            max_position_pct=self._risk.max_position_pct,
            default_quantity=self.quantity,
        )

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
            result = self._risk.can_enter(
                position_value=self.quantity * price,
                portfolio_value=pv,
                peak_portfolio=self._peak_portfolio or pv,
            )
            if not result.approved:
                self.logger.info(f"TD BLOCK ({result.check_name}) | {result.reason}")
                return

            req = self._portfolio.build_buy_order(
                self.symbol, price,
                f"TD LONG setup_buy={setup_buy} score={score:.1f}",
            )
            self._portfolio.submit_order(req)
            self.logger.info(
                f"TD LONG  | price={price:.2f} qty={req.quantity} "
                f"setup_buy={setup_buy} score={score:.1f}"
            )

        # ── SELL signal: setup_sell >= 9 OR cd_sell >= 13 OR stop-loss ──
        elif has_position:
            position = self.get_position(self.symbol)
            exit_reason = ""

            # Check TD exit signal
            if setup_sell >= 9:
                exit_reason = f"setup_sell={setup_sell}"
            elif cd_sell >= 13:
                exit_reason = f"cd_sell={cd_sell}"

            # Check stop-loss
            if not exit_reason and position is not None and position.avg_fill_price:
                sl = self._risk.should_exit(price, position.avg_fill_price)
                if sl.approved:
                    exit_reason = f"stop_loss: {sl.reason}"

            if exit_reason:
                req = self._portfolio.build_sell_order(
                    self.symbol, price, exit_reason,
                )
                self._portfolio.submit_order(req)
                self.logger.info(
                    f"TD EXIT  | price={price:.2f} qty={req.quantity} {exit_reason}"
                )


