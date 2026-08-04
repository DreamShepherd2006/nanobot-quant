"""TD Sequential — pure Python implementation of Tom DeMark's indicator.

Provides: Setup (9-count), Countdown (13-count), TDST lines, Bollinger
Bands, trading recommendations, and volume/news scoring.

Usage::

    import yfinance as yf
    from nanobot_quant.strategies.td_sequential import calculate

    df = yf.download("AAPL", start="2025-01-01")
    result = calculate(df)  # Returns summary dict for latest bar
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nanobot_quant.td_params import DEFAULT_TD_PARAMS, load_td_params


def calculate(
    df: pd.DataFrame,
    news_count: int = 0,
    params: dict | None = None,
) -> dict:
    """Run all DeMark calculations and return a summary for the latest bar.

    The input DataFrame must have columns: Open, High, Low, Close, Volume.
    Handles yfinance MultiIndex columns (e.g., ('Close', 'AAPL')) by
    flattening to single-level ('Close').

    ``params``: optional parameter dict (see ``nanobot_quant.td_params``).
    When omitted, the latest persisted ``td_params.json`` is loaded (falls
    back to defaults == the pre-parameterisation hardcoded behaviour).

    Returns a JSON-serializable dict with keys:
    timestamp, price, recommendation, setup_buy, setup_sell,
    cd_buy, cd_sell, tdst_support, tdst_resistance, rvol, score.
    """
    # Flatten MultiIndex columns (yfinance returns ('Close','AAPL') etc.)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.droplevel(1)
    # Normalise column names to title-case (onchainos/some sources use lowercase)
    df = df.rename(columns=lambda c: {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }.get(c, c))
    if params is None:
        params = load_td_params()
    engine = _DeMarkEngine(df, params)
    engine.run_all(news_count)

    last = engine.df.iloc[-1]

    def _scalar(val):
        """Convert numpy/pandas scalar to Python native, NaN → None."""
        try:
            v = float(val)
            return round(v, 2) if not np.isnan(v) else None
        except (ValueError, TypeError):
            return str(val)

    return {
        "timestamp": str(last.name),
        "price": _scalar(last["Close"]),
        "recommendation": str(last["recommendation"]),
        "setup_buy": int(last["buy_setup_count"]),
        "setup_sell": int(last["sell_setup_count"]),
        "cd_buy": int(last["buy_countdown_count"]),
        "cd_sell": int(last["sell_countdown_count"]),
        "tdst_support": _scalar(last["tdst_support"]),
        "tdst_resistance": _scalar(last["tdst_resistance"]),
        "rvol": _scalar(last.get("rvol")),
        "score": _scalar(last.get("combined_score")),
    }


# ──────────────────────────────────────────────────────────────────────
# Engine
# ──────────────────────────────────────────────────────────────────────


class _DeMarkEngine:
    """Pure-Python DeMark Sequential engine (no Rust required).

    Based on ggoni/demark-patterns (MIT), stripped of Rust backend.
    Core algorithm logic preserved exactly.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        params: dict | None = None,
    ) -> None:
        self.df = df.copy()
        p = params or DEFAULT_TD_PARAMS
        self._setup = int(p.get("setup_period", 9))
        self._cd = int(p.get("countdown_period", 13))
        self._cmp = int(p.get("compare_length", 4))
        self._strict = bool(p.get("countdown_strict", True))
        self._recycle = int(p.get("recycle_threshold", 18))
        self._w = {
            "setup": float(p.get("weight_setup", 0.40)),
            "countdown": float(p.get("weight_countdown", 0.30)),
            "tdst": float(p.get("weight_tdst", 0.15)),
            "volume": float(p.get("weight_volume", 0.10)),
            "bb": float(p.get("weight_bb", 0.05)),
        }

    # ── Setup ─────────────────────────────────────────────────────────

    def calculate_setup(self) -> pd.DataFrame:
        """TD Setup (N-count).

        - Buy Setup: close < close[i-compare_length], preceded by price flip.
        - Sell Setup: close > close[i-compare_length], preceded by price flip.
        """
        close = self.df["Close"]

        self.df["buy_setup_count"] = 0
        self.df["sell_setup_count"] = 0

        b_count = 0
        s_count = 0

        for i in range(self._cmp + 1, len(self.df)):
            # Buy Setup
            if close.iloc[i] < close.iloc[i - self._cmp]:
                if b_count == 0:
                    if close.iloc[i - 1] >= close.iloc[i - self._cmp - 1]:
                        b_count = 1
                else:
                    b_count += 1
            else:
                b_count = 0
            self.df.at[self.df.index[i], "buy_setup_count"] = b_count

            # Sell Setup
            if close.iloc[i] > close.iloc[i - self._cmp]:
                if s_count == 0:
                    if close.iloc[i - 1] <= close.iloc[i - self._cmp - 1]:
                        s_count = 1
                else:
                    s_count += 1
            else:
                s_count = 0
            self.df.at[self.df.index[i], "sell_setup_count"] = s_count

        return self.df

    # ── Countdown ─────────────────────────────────────────────────────

    def calculate_countdown(self) -> pd.DataFrame:
        """TD Countdown (N-count).

        - Bar N qualification: Low[N] <= Close[N-5] (buy) or
          High[N] >= Close[N-5] (sell).
        - Recycle after >recycle_threshold consecutive setup-qualifying bars.
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
            if active_buy and i >= self._cmp:
                buy_ext = (
                    buy_ext + 1
                    if close.iloc[i] < close.iloc[i - self._cmp]
                    else 0
                )
                if buy_ext > self._recycle:
                    self.df.at[self.df.index[i], "buy_countdown_recycled"] = True
                    active_buy = False
                    buy_count = 0
                    buy_bar8_close = np.nan
                    buy_ext = 0

            if active_sell and i >= self._cmp:
                sell_ext = (
                    sell_ext + 1
                    if close.iloc[i] > close.iloc[i - self._cmp]
                    else 0
                )
                if sell_ext > self._recycle:
                    self.df.at[self.df.index[i], "sell_countdown_recycled"] = True
                    active_sell = False
                    sell_count = 0
                    sell_bar8_close = np.nan
                    sell_ext = 0

            # ── Start countdown on Setup N ──
            if not active_buy and self.df.iloc[i]["buy_setup_count"] == self._setup:
                active_buy = True
                buy_count = 0
                buy_ext = 0
            if not active_sell and self.df.iloc[i]["sell_setup_count"] == self._setup:
                active_sell = True
                sell_count = 0
                sell_ext = 0

            # ── Buy countdown ──
            if active_buy and i >= 2:
                ref = low if self._strict else high
                if close.iloc[i] <= ref.iloc[i - 2]:
                    if buy_count < self._cd - 1:
                        buy_count += 1
                        if buy_count == self._cd - 5:
                            buy_bar8_close = close.iloc[i]
                        self.df.at[self.df.index[i], "buy_countdown_count"] = buy_count
                    else:  # checking final bar N
                        if ref.iloc[i] <= buy_bar8_close:
                            buy_count = self._cd
                            self.df.at[self.df.index[i], "buy_countdown_count"] = self._cd
                            active_buy = False
                            buy_count = 0

            # ── Sell countdown ──
            if active_sell and i >= 2:
                ref = high if self._strict else low
                if close.iloc[i] >= ref.iloc[i - 2]:
                    if sell_count < self._cd - 1:
                        sell_count += 1
                        if sell_count == self._cd - 5:
                            sell_bar8_close = close.iloc[i]
                        self.df.at[self.df.index[i], "sell_countdown_count"] = sell_count
                    else:
                        if ref.iloc[i] >= sell_bar8_close:
                            sell_count = self._cd
                            self.df.at[self.df.index[i], "sell_countdown_count"] = self._cd
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
            if self.df.iloc[i]["buy_setup_count"] == self._setup:
                last_resistance = pending_resistance
            if self.df.iloc[i]["sell_setup_count"] == self._setup:
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

            b_9 = self.df.iloc[i]["buy_setup_count"] == self._setup
            s_9 = self.df.iloc[i]["sell_setup_count"] == self._setup
            b_13 = self.df.iloc[i]["buy_countdown_count"] == self._cd
            s_13 = self.df.iloc[i]["sell_countdown_count"] == self._cd

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
        """5-factor composite score computed for every bar.

        Per-factor maxima: Setup 40 | Countdown 30 | TDST 15 | Volume 10 | BB 5
        (raw scores, before weights). The combined score is
        ``Σ raw_i × weight_i`` — with default weights the scale is
        0–28.75, NOT 0–100 (historical docstring was misleading).
        """
        if self.df.empty:
            return 0.0

        # ── Volume (0–10) ────────────────────────────────────────────────
        if "Volume" not in self.df.columns:
            self.df["vol_sma20"] = np.nan
            self.df["rvol"] = 0.0
            self.df["volume_score"] = 5.0  # neutral when missing
        else:
            self.df["vol_sma20"] = self.df["Volume"].rolling(
                window=20, min_periods=1
            ).mean()
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

        # ── Setup direction (0–40) ──────────────────────────────────────
        self.df["setup_score"] = np.where(
            self.df["buy_setup_count"] > 0,
            np.minimum(40.0, self.df["buy_setup_count"] / self._setup * 40.0),
            0.0,
        )

        # ── Countdown proximity (0–30) ───────────────────────────────────
        self.df["countdown_score"] = np.where(
            self.df["buy_countdown_count"] > 0,
            np.minimum(30.0, self.df["buy_countdown_count"] / self._cd * 30.0),
            0.0,
        )

        # ── TDST support above price (0–15) ─────────────────────────────
        self.df["tdst_score"] = 0.0
        has_tsup = ~self.df["tdst_support"].isna() & (self.df["tdst_support"] > 0)
        self.df.loc[
            has_tsup & (self.df["Close"] > self.df["tdst_support"]), "tdst_score"
        ] = 15.0

        # ── Bollinger regime (0–5) — oversold = bullish ──────────────────
        self.df["bb_score"] = 0.0
        has_bl = ~self.df["bb_lower"].isna() & (self.df["bb_lower"] > 0)
        self.df.loc[
            has_bl & (self.df["Close"] < self.df["bb_lower"]), "bb_score"
        ] = 5.0

        # ── Composite (weighted) ─────────────────────────────────────────
        self.df["combined_score"] = (
            self.df["volume_score"] * self._w["volume"]
            + self.df["setup_score"] * self._w["setup"]
            + self.df["countdown_score"] * self._w["countdown"]
            + self.df["tdst_score"] * self._w["tdst"]
            + self.df["bb_score"] * self._w["bb"]
        )

        # ── News bump (only for latest bar; news_count is per-call) ──────
        self.df["news_score"] = 0.0
        if news_count > 0:
            last_idx = self.df.index[-1]
            ns = min(10.0, 0.5 * news_count)
            self.df.at[last_idx, "news_score"] = ns
            old = self.df.at[last_idx, "combined_score"]
            self.df.at[last_idx, "combined_score"] = old * 0.9 + ns * 0.1

        last_idx = self.df.index[-1]
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
