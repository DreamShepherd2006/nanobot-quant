"""TD Sequential — 同花顺「九转序列」口径变体（setup 1-9 循环）。

与 ``td_sequential`` 原版的唯一差异：setup 计数达到 ``setup_period``
（默认 9）后，下一根仍满足条件的 bar 从 1 重新计数（DeMark 1-9 循环，
同花顺/通达信「九转序列」的显示口径）。原版继续累加（10、11、12…）。

注册为独立策略 ``td_sequential_cycle``（variant_of="td_sequential"），
供算法校准 / 参数扫描对比研究——不预设哪个口径"正确"，由调测数据决定。
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.strategies.td_sequential import (
    _DeMarkEngine,
)
from nanobot_quant.strategies.td_sequential import (
    calculate as _base_calculate,
)


class CycleDeMarkEngine(_DeMarkEngine):
    """DeMark engine with setup count recycling after ``setup_period``.

    Inherits countdown / TDST / Bollinger / scoring / recommendations
    verbatim; only the setup counter behaviour differs (1-9 loop).
    Note: because setup restarts at 1 every cycle, TDST pending/lock also
    refreshes per cycle (tracking the most recent completed setup), which
    is closer to canonical DeMark than the base variant.
    """

    def calculate_setup(self) -> pd.DataFrame:
        """TD Setup with 1-9 recycling (同花顺「九转序列」口径)."""
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
                elif b_count < self._setup:
                    b_count += 1
                else:
                    b_count = 1  # setup complete → restart cycle (1-9 loop)
            else:
                b_count = 0
            self.df.at[self.df.index[i], "buy_setup_count"] = b_count

            # Sell Setup
            if close.iloc[i] > close.iloc[i - self._cmp]:
                if s_count == 0:
                    if close.iloc[i - 1] <= close.iloc[i - self._cmp - 1]:
                        s_count = 1
                elif s_count < self._setup:
                    s_count += 1
                else:
                    s_count = 1  # setup complete → restart cycle (1-9 loop)
            else:
                s_count = 0
            self.df.at[self.df.index[i], "sell_setup_count"] = s_count

        return self.df


def calculate(
    df: pd.DataFrame,
    news_count: int = 0,
    params: dict | None = None,
) -> dict:
    """同花顺口径 TD Sequential — same output contract as
    ``td_sequential.calculate`` but with setup 1-9 recycling.

    Shares preprocessing / signal extraction with the base variant
    (``engine_cls`` override); parameters come from the same
    ``td_params.json`` so the two variants are directly comparable.
    """
    return _base_calculate(
        df,
        news_count=news_count,
        params=params,
        engine_cls=CycleDeMarkEngine,
    )
