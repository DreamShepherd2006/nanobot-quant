"""TD Sequential — pure Python implementation of Tom DeMark's indicator.

Provides: Setup (9-count), Countdown (13-count), TDST lines, Bollinger
Bands, trading recommendations, and volume/news scoring.

Usage::

    import yfinance as yf
    from nanobot_quant.strategies.td_sequential import calculate

    df = yf.download("AAPL", start="2025-01-01")
    result = calculate(df)  # Returns DataFrame with all columns
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate(df: pd.DataFrame, news_count: int = 0) -> pd.DataFrame:
    """Run all DeMark calculations on an OHLCV DataFrame.

    The input DataFrame must have columns: Open, High, Low, Close, Volume.
    Returns the DataFrame with additional columns added in-place.
    """
    engine = _DeMarkEngine(df)
    return engine.run_all(news_count)


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────


class _DeMarkEngine:
    """Pure-Python DeMark Sequential engine (no Rust required).

    Based on ggoni/demark-patterns (MIT), stripped of Rust backend.
    Core algorithm logic preserved exactly.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df.copy()

    # ── Setup ─────────────────────────────────────────────────────────

    def calculate_setup(self) -> pd.DataFrame:
        """TD Setup (9-count).

        - Buy Setup: close < close[i-4], preceded by price flip.
        - Sell Setup: close > close[i-4], preceded by price flip.
        """
        close = self.df["Close"]
        low = self.df["Low"]
        high = self.df["High"]

        self.df["buy_setup_count"] = 0
        self.df["sell_setup_count"] = 0

        b_count = 0
        s_count = 0

        for i in range(5, len(self.df)):
            # Buy Setup
            if close.iloc[i] < close.iloc[i - 4]:
                if b_count == 0:
                    if close.iloc[i - 1] >= close.iloc[i - 5]:
                        b_count = 1
                else:
                    b_count += 1
            else:
                b_count = 0
            self.df.at[self.df.index[i], "buy_setup_count"] = b_count

            # Sell Setup
            if close.iloc[i] > close.iloc[i - 4]:
                if s_count == 0:
                    if close.iloc[i - 1] <= close.iloc[i - 5]:
                        s_count = 1
                else:
                    s_count += 1
            else:
                s_count = 0
            self.df.at[self.df.index[i], "sell_setup_count"] = s_count

        return self.df

    # ── Countdown ─────────────────────────────────────────────────────

    def calculate_countdown(self) -> pd.DataFrame:
        """TD Countdown (13-count).

        - Bar 13 qualification: Low[13] <= Close[8] (buy) or
          High[13] >= Close[8] (sell).
        - Recycle after >18 consecutive setup-qualifying bars.
        """
        close = self.df["Close"]
        high = self.df["High"]
        low = self.df["Low"]

        self.df["buy_countdown_count"] = 0
        self.df["sell_countdown_count"] = 0
        self.df["buy_countdown_recycled"] = False
        self.df["sell_countdown_recycled"] = False

        active_buy = False
        buy_count = 0
        buy_bar8_close = np.nan
        buy_ext = 0

        active_sell = False
        sell_count = 0
        sell_bar8_close = np.nan
        sell_ext = 0

        for i in range(len(self.df)):
            # ── Recycle checks ──
            if active_buy and i >= 4:
                buy_ext = buy_ext + 1 if close.iloc[i] < close.iloc[i - 4] else 0
                if buy_ext > 18:
                    self.df.at[self.df.index[i], "buy_countdown_recycled"] = True
                    active_buy = False
                    buy_count = 0
                    buy_bar8_close = np.nan
                    buy_ext = 0

            if active_sell and i >= 4:
                sell_ext = sell_ext + 1 if close.iloc[i] > close.iloc[i - 4] else 0
                if sell_ext > 18:
                    self.df.at[self.df.index[i], "sell_countdown_recycled"] = True
                    active_sell = False
                    sell_count = 0
                    sell_bar8_close = np.nan
                    sell_ext = 0

            # ── Start countdown on Setup 9 ──
            if not active_buy and self.df.iloc[i]["buy_setup_count"] == 9:
                active_buy = True
                buy_count = 0
                buy_ext = 0
            if not active_sell and self.df.iloc[i]["sell_setup_count"] == 9:
                active_sell = True
                sell_count = 0
                sell_ext = 0

            # ── Buy countdown ──
            if active_buy and i >= 2:
                if close.iloc[i] <= low.iloc[i - 2]:
                    if buy_count < 12:
                        buy_count += 1
                        if buy_count == 8:
                            buy_bar8_close = close.iloc[i]
                        self.df.at[self.df.index[i], "buy_countdown_count"] = buy_count
                    else:  # count == 12, checking bar 13
                        if low.iloc[i] <= buy_bar8_close:
                            buy_count = 13
                            self.df.at[self.df.index[i], "buy_countdown_count"] = 13
                            active_buy = False
                            buy_count = 0

            # ── Sell countdown ──
            if active_sell and i >= 2:
                if close.iloc[i] >= high.iloc[i - 2]:
                    if sell_count < 12:
                        sell_count += 1
                        if sell_count == 8:
                            sell_bar8_close = close.iloc[i]
                        self.df.at[self.df.index[i], "sell_countdown_count"] = sell_count
                    else:
                        if high.iloc[i] >= sell_bar8_close:
                            sell_count = 13
                            self.df.at[self.df.index[i], "sell_countdown_count"] = 13
                            active_sell = False
                            sell_count = 0

        return self.df

    # ── TDST ──────────────────────────────────────────────────────────

    def calculate_tdst(self) -> pd.DataFrame:
        """TD Setup Trend lines.

        - Resistance: High of bar 1 of a Buy Setup.
        - Support: Low of bar 1 of a Sell Setup.
        """
        high = self.df["High"]
        low = self.df["Low"]

        self.df["tdst_support"] = np.nan
        self.df["tdst_resistance"] = np.nan

        last_support = np.nan
        last_resistance = np.nan
        pending_support = np.nan
        pending_resistance = np.nan

        for i in range(len(self.df)):
            if self.df.iloc[i]["buy_setup_count"] == 1:
                pending_resistance = high.iloc[i]
            if self.df.iloc[i]["sell_setup_count"] == 1:
                pending_support = low.iloc[i]
            if self.df.iloc[i]["buy_setup_count"] == 9:
                last_resistance = pending_resistance
            if self.df.iloc[i]["sell_setup_count"] == 9:
                last_support = pending_support

            self.df.at[self.df.index[i], "tdst_support"] = last_support
            self.df.at[self.df.index[i], "tdst_resistance"] = last_resistance

        return self.df

    # ── Bollinger Bands ───────────────────────────────────────────────

    def calculate_bollinger_bands(
        self, period: int = 20, std_dev: float = 2.0
    ) -> pd.DataFrame:
        """Bollinger Bands."""
        close = self.df["Close"]
        self.df["bb_middle"] = close.rolling(period).mean()
        rolling_std = close.rolling(period).std()
        self.df["bb_upper"] = self.df["bb_middle"] + std_dev * rolling_std
        self.df["bb_lower"] = self.df["bb_middle"] - std_dev * rolling_std
        return self.df

    # ── Recommendations ───────────────────────────────────────────────

    def calculate_recommendations(self) -> pd.DataFrame:
        """Generate BUY/SELL recommendations.

        Signals based on:
        - Setup 9 / Countdown 13 completion
        - Overbought (price > BB upper) / Oversold (price < BB lower)
        - TDST support / resistance breaks
        """
        self.df["recommendation"] = "HOLD"

        for i in range(len(self.df)):
            close = self.df.iloc[i]["Close"]
            support = self.df.iloc[i]["tdst_support"]
            resist = self.df.iloc[i]["tdst_resistance"]
            bb_upper = self.df.iloc[i]["bb_upper"]
            bb_lower = self.df.iloc[i]["bb_lower"]

            b_9 = self.df.iloc[i]["buy_setup_count"] == 9
            s_9 = self.df.iloc[i]["sell_setup_count"] == 9
            b_13 = self.df.iloc[i]["buy_countdown_count"] == 13
            s_13 = self.df.iloc[i]["sell_countdown_count"] == 13

            rec = "HOLD"

            # ── Sell signals ──
            if s_9 or s_13:
                rec = "SELL (Overbought)" if close > bb_upper else "SELL (Setup Complete)"

            # Support break → sell
            if not np.isnan(support) and i > 0:
                prev_close = self.df.iloc[i - 1]["Close"]
                if close < support <= prev_close:
                    rec = "SELL (Support Break)"

            # ── Buy signals ──
            if b_9 or b_13:
                rec = "BUY (Oversold)" if close < bb_lower else "BUY (Setup Complete)"

            # Resistance break → buy
            if not np.isnan(resist) and i > 0:
                prev_close = self.df.iloc[i - 1]["Close"]
                if close > resist >= prev_close:
                    rec = "BUY (Resistance Break)"

            self.df.at[self.df.index[i], "recommendation"] = rec

        return self.df

    # ── Volume / News scoring ─────────────────────────────────────────

    def calculate_buy_scoring(self, news_count: int = 0) -> float:
        """Volume (RVOL) + news intensity combined score for the latest bar."""
        if self.df.empty:
            return 0.0

        if "Volume" not in self.df.columns:
            self.df["vol_sma20"] = np.nan
            self.df["rvol"] = 0.0
            self.df["volume_score"] = 0.0
            self.df["news_score"] = 0.0
            self.df["combined_score"] = 0.0
            return 0.0

        self.df["vol_sma20"] = self.df["Volume"].rolling(window=20, min_periods=1).mean()
        self.df["rvol"] = np.where(
            self.df["vol_sma20"] > 0,
            self.df["Volume"] / self.df["vol_sma20"],
            0.0,
        )
        self.df["volume_score"] = np.where(
            self.df["rvol"] < 1.0,
            5.0 * self.df["rvol"],
            np.minimum(10.0, 5.0 + 2.5 * (self.df["rvol"] - 1.0)),
        )
        self.df["news_score"] = 0.0
        self.df["combined_score"] = 0.0

        if news_count == 0:
            news_score = 0.0
        elif 1 <= news_count <= 20:
            news_score = 0.5 * news_count
        else:
            news_score = 10.0

        last_idx = self.df.index[-1]
        self.df.at[last_idx, "news_score"] = news_score
        self.df.at[last_idx, "combined_score"] = (
            self.df.at[last_idx, "volume_score"] * 0.6 + news_score * 0.4
        )
        return float(self.df.at[last_idx, "combined_score"])

    # ── Run everything ────────────────────────────────────────────────

    def run_all(self, news_count: int = 0) -> pd.DataFrame:
        """Run all DeMark calculations in correct dependency order."""
        self.calculate_setup()
        self.calculate_countdown()
        self.calculate_tdst()
        self.calculate_bollinger_bands()
        self.calculate_recommendations()
        self.calculate_buy_scoring(news_count)
        return self.df
