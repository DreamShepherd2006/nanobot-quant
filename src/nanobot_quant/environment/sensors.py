"""环境传感器：X3（价格位置）与 F1（波动率状态）——纯计算，无交易逻辑。

面向 15m 周期设计（经 A/B 实验验证的传感器组合，见 docs/quant-system.md）：
  X3 = (Close - EMA20) / ATR20          —— 价格相对均线的偏离（均值回归风险）
  F1 = ATR20[t] / ATR20[t-12]           —— 3h 波动率变化率（>1 扩张 <1 收缩）

语义：
  X3 高（>1.5 ATR） = 追高区，3h 内被套概率 87-92%（10/10 标的、30/30 窗口验证）
  F1 高（>1.5）     = 波动率扩张，被套风险上升且与 X3 条件独立（X3 桶内 +16~24pp）

输入：OHLCV DataFrame（列名支持大写 Open/High/Low/Close/Volume 或小写，UTC DatetimeIndex）
输出：与输入 index 对齐的 Series/DataFrame（前 n 根因窗口不足为 NaN）
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

# 默认参数（与实验扫描一致，ATR 用 Wilder 平滑 ewm(alpha=1/n)）
EMA_N = 20
ATR_N = 20
F1_LOOKBACK = 12  # 15m × 12 = 3h


def _norm_columns(df: pd.DataFrame) -> pd.DataFrame:
    """列名归一化为大写（支持大小写输入）。"""
    rename = {}
    for lo, up in [("open", "Open"), ("high", "High"), ("low", "Low"),
                   ("close", "Close"), ("volume", "Volume"),
                   ("base_vol", "Volume"), ("quote_vol", "QuoteVolume")]:
        if lo in df.columns and up not in df.columns:
            rename[lo] = up
    return df.rename(columns=rename) if rename else df


def atr_series(df: pd.DataFrame, atr_n: int = ATR_N) -> pd.Series:
    """Wilder ATR（与实验一致：TR 三要素取最大 + ewm alpha=1/n）。"""
    d = _norm_columns(df)
    high, low, close = d["High"], d["Low"], d["Close"]
    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / atr_n, adjust=False).mean()


def compute_x3(df: pd.DataFrame, ema_n: int = EMA_N, atr_n: int = ATR_N) -> pd.Series:
    """X3 = (Close - EMA_n) / ATR_n。正 = 价格在均线上方（追高区）。"""
    d = _norm_columns(df)
    ema = d["Close"].ewm(span=ema_n, adjust=False).mean()
    atr = atr_series(d, atr_n)
    return (d["Close"] - ema) / atr


def compute_f1(df: pd.DataFrame, atr_n: int = ATR_N,
               lookback: int = F1_LOOKBACK) -> pd.Series:
    """F1 = ATR_n[t] / ATR_n[t-lookback]。>1 波动扩张，<1 收缩。"""
    atr = atr_series(_norm_columns(df), atr_n)
    return atr / atr.shift(lookback)


def sensor_frame(df: pd.DataFrame, ema_n: int = EMA_N, atr_n: int = ATR_N,
                 lookback: int = F1_LOOKBACK) -> pd.DataFrame:
    """一屏返回 {X3, F1, ATR}，index 与输入对齐（窗口期 NaN）。"""
    d = _norm_columns(df)
    atr = atr_series(d, atr_n)
    ema = d["Close"].ewm(span=ema_n, adjust=False).mean()
    return pd.DataFrame({
        "X3": (d["Close"] - ema) / atr,
        "F1": atr / atr.shift(lookback),
        "ATR": atr,
    }, index=d.index)


def latest_state(df: pd.DataFrame, ema_n: int = EMA_N, atr_n: int = ATR_N,
                 lookback: int = F1_LOOKBACK) -> dict:
    """最新一根已收盘 bar 的传感器状态（供 1m 交易对齐用）。

    返回 {"x3": float, "f1": float, "atr": float}；窗口不足时返回 None 字段。
    """
    s = sensor_frame(df, ema_n=ema_n, atr_n=atr_n, lookback=lookback)
    row = s.iloc[-1]
    return {
        "x3": None if pd.isna(row["X3"]) else float(row["X3"]),
        "f1": None if pd.isna(row["F1"]) else float(row["F1"]),
        "atr": None if pd.isna(row["ATR"]) else float(row["ATR"]),
    }
