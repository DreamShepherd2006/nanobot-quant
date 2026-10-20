"""环境传感器单测：X3/F1 计算正确性（合成数据 + 与实验口径一致）。"""
import numpy as np
import pandas as pd
import pytest

from nanobot_quant.environment.sensors import (
    EMA_FIB,
    EMA_TREND_N,
    atr_series,
    compute_f1,
    compute_x3,
    ema_series,
    ema_trend,
    ema_warmup_bars,
    latest_state,
    sensor_frame,
)


def make_df(close, high=None, low=None, open_=None, volume=None):
    n = len(close)
    high = high if high is not None else np.array(close) * 1.01
    low = low if low is not None else np.array(close) * 0.99
    open_ = open_ if open_ is not None else close
    volume = volume if volume is not None else np.ones(n)
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
    }, index=idx)


def test_x3_zero_when_flat():
    """价格恒定 → X3 = 0（close == EMA）。"""
    df = make_df(np.full(60, 100.0))
    x3 = compute_x3(df)
    assert x3.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_x3_positive_when_above_ema():
    """价格在均线上方 → X3 > 0。"""
    close = np.concatenate([np.full(40, 100.0), np.linspace(100, 120, 20)])
    x3 = compute_x3(make_df(close))
    assert x3.iloc[-1] > 0


def test_x3_negative_when_below_ema():
    close = np.concatenate([np.full(40, 100.0), np.linspace(100, 80, 20)])
    x3 = compute_x3(make_df(close))
    assert x3.iloc[-1] < 0


def test_f1_expansion_when_volatility_surges():
    """前段平静后段剧烈 → 最后 12 根窗口内 F1 显著 > 1。"""
    n = 60
    close = np.full(n, 100.0)
    high = np.full(n, 100.5)
    low = np.full(n, 99.5)
    # 后 12 根剧烈波动
    high[-12:] = 105.0
    low[-12:] = 95.0
    f1 = compute_f1(make_df(close, high=high, low=low))
    assert f1.iloc[-1] > 1.5


def test_f1_near_one_when_volatility_stable():
    n = 60
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    high = close + 0.5
    low = close - 0.5
    f1 = compute_f1(make_df(close, high=high, low=low))
    assert 0.5 < f1.iloc[-1] < 2.0


def test_sensor_frame_columns():
    df = make_df(np.linspace(100, 110, 60))
    s = sensor_frame(df)
    assert list(s.columns) == ["X3", "F1", "ATR"]
    assert s.index.equals(df.index)
    # 窗口期（前 12 根 F1 无值，前 1 根 ATR 无值）
    assert s["F1"].iloc[:11].isna().all()
    assert s["F1"].iloc[-1] is not np.nan


def test_lowercase_columns_supported():
    """小写列输入兼容（实验脚本口径）。"""
    df = pd.DataFrame({
        "open": np.full(60, 100.0), "high": np.full(60, 101.0),
        "low": np.full(60, 99.0), "close": np.full(60, 100.0),
        "base_vol": np.ones(60),
    }, index=pd.date_range("2026-01-01", periods=60, freq="15min"))
    x3_up = compute_x3(df)
    x3_low = compute_x3(df.rename(columns={c: c.capitalize() for c in df.columns}))
    assert np.allclose(x3_up.dropna(), x3_low.dropna())


def test_atr_matches_manual_wilder():
    """ATR 与手算 ewm 一致。"""
    df = make_df(np.linspace(100, 120, 30))
    atr = atr_series(df)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"] - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    expected = tr.ewm(alpha=1 / 20, adjust=False).mean()
    assert np.allclose(atr.dropna(), expected.dropna())


def test_latest_state():
    df = make_df(np.linspace(100, 110, 60))
    st = latest_state(df)
    assert set(st) == {"x3", "f1", "atr"}
    assert st["x3"] is not None and st["f1"] is not None


# ── EMA 趋势变量（C73/C74 研究线，2026-10-20）──────────────────────

def test_ema_series_uses_alpha_2_over_n_plus_1():
    """EMA(n)：首根作初值、α = 2/(n+1)（C73 冻结口径）。"""
    close = pd.Series([100.0, 110.0, 100.0, 110.0])
    out = ema_series(close, n=3)
    assert out.iloc[0] == pytest.approx(100.0)  # adjust=False → 首根作初值
    alpha = 2 / (3 + 1)
    assert out.iloc[1] == pytest.approx(100 + alpha * (110 - 100))


def test_ema_warmup_bars_is_5_tau_with_floor():
    """预热 = 5τ，τ=(n−1)/2；至少 50 根（界面只读展示的就是这个值）。"""
    assert ema_warmup_bars(144) == int(5 * (144 + 1) / 2)
    assert ema_warmup_bars(89) == int(5 * (89 + 1) / 2)
    assert ema_warmup_bars(5) == 50  # 下限
    assert ema_warmup_bars(144) > ema_warmup_bars(89)  # n 越大预热越长


def test_ema_trend_slope_sign_follows_trend():
    """slope_k = (EMA_t − EMA_{t−k}) / (k·ATR)：上涨为正、下跌为负。"""
    n = 200
    up = pd.DataFrame({
        "Close": [100 + i * 0.5 for i in range(n)],
        "High": [100 + i * 0.5 + 0.2 for i in range(n)],
        "Low": [100 + i * 0.5 - 0.2 for i in range(n)],
    })
    down = pd.DataFrame({
        "Close": [200 - i * 0.5 for i in range(n)],
        "High": [200 - i * 0.5 + 0.2 for i in range(n)],
        "Low": [200 - i * 0.5 - 0.2 for i in range(n)],
    })
    assert ema_trend(up, n=20, k=3)["slope"].iloc[-1] > 0
    assert ema_trend(down, n=20, k=3)["slope"].iloc[-1] < 0
    # 前几根 ATR 不足 → NaN（不是 0）；调用方 fail-open 并计数 ema_gate_na
    assert pd.isna(ema_trend(down, n=20, k=3)["slope"].iloc[0])


def test_ema_fib_family_is_candidate_set_only():
    """斐波那契族是**候选集**（界面下拉来源），不代表数学优势。"""
    assert EMA_FIB == (5, 8, 13, 21, 34, 55, 89, 144)
    assert EMA_TREND_N in EMA_FIB
