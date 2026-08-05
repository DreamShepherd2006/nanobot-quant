"""TD Sequential — 富途 NINE「神奇九转」口径变体（setup-only，忠实复刻富途源码）。

富途 moonscript 源码（indicator 'NINE'）核心逻辑：

    a1 = close() > ref(close(), 4)   # 或 <
    nt = bars_last_count(a1)          # 连续满足 a1 的根数（不满足即归零）
    tj11 = nt == 9                    # 恰好连续 9 根 → 触发一次

本变体忠实复刻该语义：

1. **无翻转确认**：`bars_last_count` 只统计连续满足条件（close vs close[i-4]）
   的根数，不要求 setup 从价格翻转后开始（与原版/同花顺变体的差异）。
2. **9 后继续累加**：nt 超过 9 后继续累加（10、11、12…），不重置（同花顺
   变体 9 后循环回 1）；信号仅在 ``nt == setup_period`` 的那一根触发一次，
   连续单边行情只触发一次，中断归零后新一轮重新计数。
3. **无 countdown / TDST / Bollinger**：富途界面只绘制 1-9 数字，输出列
   恒 0 / NaN；score 简化为 ``setup_count / setup_period``（0–1 尺度，
   setup 完成时 = 1.0）。

注册为独立策略 ``td_sequential_futu``（variant_of="td_sequential"），
作为「民间最简化口径」样本与 DeMark 原版、同花顺口径三向对照研究。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from nanobot_quant.strategies.td_sequential import (
    _DeMarkEngine,
)
from nanobot_quant.strategies.td_sequential import (
    calculate as _base_calculate,
)


class FutuDeMarkEngine(_DeMarkEngine):
    """富途 NINE 口径引擎：无翻转确认、setup 连续累加、信号恰在 setup 完成根触发。

    Inherits the base engine's constructor (setup/countdown/compare/recycle
    params) and Bollinger computation (unused by this variant's signals);
    overrides setup / countdown / TDST / recommendations / scoring.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        params: dict | None = None,
    ) -> None:
        super().__init__(df, params)
        # 富途无评分体系：权重全部置 0（防御——combined_score 完全由
        # setup_count/setup_period 决定，不混入任何其他成分）。
        self._w = {k: 0.0 for k in self._w}

    def calculate_setup(self) -> pd.DataFrame:
        """富途口径 setup：连续满足即数（无翻转确认），9 后继续累加不重置。

        Mirror of ``bars_last_count(close vs close[i-4])``:
        satisfying bar → count += 1 (no upper bound); non-satisfying
        bar (including equality) → count = 0.
        """
        close = self.df["Close"]

        self.df["buy_setup_count"] = 0
        self.df["sell_setup_count"] = 0

        b_count = 0
        s_count = 0

        # 起点 = cmp（富途 a1 从第 cmp+1 根 K 线即可比较，无需翻转条件
        # 因此无 close[i-cmp-1] 越界问题——比原版/同花顺早一根参与计数）。
        for i in range(self._cmp, len(self.df)):
            # Buy Setup: close < close[i-cmp]
            if close.iloc[i] < close.iloc[i - self._cmp]:
                b_count += 1
            else:
                b_count = 0
            self.df.at[self.df.index[i], "buy_setup_count"] = b_count

            # Sell Setup: close > close[i-cmp]
            if close.iloc[i] > close.iloc[i - self._cmp]:
                s_count += 1
            else:
                s_count = 0
            self.df.at[self.df.index[i], "sell_setup_count"] = s_count

        return self.df

    def calculate_countdown(self) -> pd.DataFrame:
        """富途无 countdown — columns stay 0."""
        self.df["buy_countdown_count"] = 0
        self.df["sell_countdown_count"] = 0
        self.df["buy_countdown_recycled"] = False
        self.df["sell_countdown_recycled"] = False
        return self.df

    def calculate_tdst(self) -> pd.DataFrame:
        """富途无 TDST（界面只绘制 1-9 数字）— columns stay NaN."""
        self.df["tdst_support"] = np.nan
        self.df["tdst_resistance"] = np.nan
        return self.df

    def calculate_recommendations(self) -> pd.DataFrame:
        """富途信号：仅当 setup count 恰好等于 setup_period 时触发一次。

        Mirrors ``tj11 = (nt == 9)`` — the signal fires on the exact bar
        where the run reaches ``setup_period``; longer runs (nt > 9) do
        NOT re-fire until the streak resets.
        """
        self.df["recommendation"] = "HOLD"

        for i in range(len(self.df)):
            b_9 = self.df.iloc[i]["buy_setup_count"] == self._setup
            s_9 = self.df.iloc[i]["sell_setup_count"] == self._setup
            if s_9:
                self.df.at[self.df.index[i], "recommendation"] = "SELL (Setup Complete)"
            elif b_9:
                self.df.at[self.df.index[i], "recommendation"] = "BUY (Setup Complete)"

        return self.df

    def calculate_buy_scoring(self, news_count: int = 0) -> float:
        """富途评分：score = max(buy, sell)_setup_count / setup_period（0–1 尺度）。

        Scheme A (用户拍板): a simple normalised setup progress score —
        ``count / setup_period``, equal to 1.0 when a setup completes and
        < 1.0 otherwise. rvol is pinned to 1.0 (neutral) so the
        TickerSignal contract stays intact.
        """
        if self.df.empty:
            return 0.0

        self.df["combined_score"] = (
            np.maximum(self.df["buy_setup_count"], self.df["sell_setup_count"])
            / self._setup
        )
        # rvol 中性（富途无量能概念）
        self.df["rvol"] = 1.0

        last_idx = self.df.index[-1]
        return float(self.df.at[last_idx, "combined_score"])


def calculate(
    df: pd.DataFrame,
    news_count: int = 0,
    params: dict | None = None,
) -> dict:
    """富途 NINE 口径 TD Sequential — same output contract as
    ``td_sequential.calculate`` but setup-only (no flip confirmation,
    no countdown / TDST, score = setup_count / setup_period).

    Shares preprocessing / signal extraction with the base variant
    (``engine_cls`` override); parameters come from the same
    ``td_params.json`` so all variants are directly comparable.
    """
    return _base_calculate(
        df,
        news_count=news_count,
        params=params,
        engine_cls=FutuDeMarkEngine,
    )
