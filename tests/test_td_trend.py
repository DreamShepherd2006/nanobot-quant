"""TD 趋势状态判断单测（td_trend.py，2026-08-31）。

验证（修正版状态机）：
- 严格涨/跌序列 → 对应趋势态（下跌 >=9 累加仍是跌势禁买，不是反转窗口）
- 横盘 → 弹簧；setup 归零瞬间窗口保护（不误判弹簧/不误开闸）
- 数据不足 → unknown；compute_trend_state 集成字段
"""

import numpy as np
import pandas as pd

from nanobot_quant.strategies.td_sequential import _DeMarkEngine
from nanobot_quant.strategies.td_trend import (
    DOWNTREND,
    RANGING,
    UNKNOWN,
    UPTREND,
    UPTREND_EXHAUSTED,
    classify_trend_state,
    compute_trend_state,
)
from nanobot_quant.td_params import DEFAULT_TD_PARAMS


def _df(close: np.ndarray) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "Open": close - 0.3,
        "High": close + 0.5,
        "Low": close - 0.5,
        "Close": close,
        "Volume": np.full(len(close), 100.0),
    })


def _setups(df: pd.DataFrame) -> tuple:
    out = _DeMarkEngine(df, dict(DEFAULT_TD_PARAMS)).run_all()
    return out["buy_setup_count"], out["sell_setup_count"]


def test_strict_uptrend_is_uptrend_exhausted():
    """严格上涨 25 根：setup_sell 累加 >=9 → 涨势末端（禁高9 追高）。"""
    close = np.concatenate([np.full(5, 100.0), np.arange(101, 126, 1.0)])  # 30 根
    sb, ss = _setups(_df(close))
    state = classify_trend_state(sb, ss)
    assert ss.iloc[-1] >= 9
    assert state == UPTREND_EXHAUSTED


def test_short_uptrend_is_uptrend():
    """上涨 5-8 根：setup_sell 1~8 → 涨势（允许）。"""
    close = np.concatenate([np.full(22, 100.0), np.arange(101, 109, 1.0)])  # 30 根
    sb, ss = _setups(_df(close))
    state = classify_trend_state(sb, ss)
    assert 5 <= ss.iloc[-1] <= 8, f"setup_sell={ss.iloc[-1]} 应为中继区间"
    assert state == UPTREND, f"应为涨势，实际 {state}"


def test_strict_downtrend_is_downtrend():
    """严格下跌 25 根：setup_buy 累加 >=9 仍是跌势（禁买），不是反转窗口。"""
    close = np.concatenate([np.full(5, 100.0), np.arange(99, 74, -1.0)])  # 30 根
    sb, ss = _setups(_df(close))
    state = classify_trend_state(sb, ss)
    assert sb.iloc[-1] >= 9, f"setup_buy={sb.iloc[-1]} 应累加过 9"
    assert state == DOWNTREND, f"setup >=9 累加应为跌势禁买，实际 {state}"


def test_weak_downtrend_is_ranging():
    """刚启动的下跌（setup_buy 1~4）：未确立（<5）→ 弹簧保守参数兜底，不禁买。"""
    close = np.concatenate([np.full(26, 100.0), np.arange(99, 95, -1.0)])  # 30 根
    sb, ss = _setups(_df(close))
    assert 1 <= sb.iloc[-1] <= 4, f"setup_buy={sb.iloc[-1]}"
    assert classify_trend_state(sb, ss) == RANGING


def test_ranging_is_ranging():
    """横盘小波动：setup 计数走不远，判定弹簧。"""
    rng = np.random.default_rng(42)
    close = 100.0 + rng.normal(0, 0.3, 60)
    sb, ss = _setups(_df(close))
    state = classify_trend_state(sb, ss)
    assert state == RANGING, f"横盘应弹簧，实际 {state}"


def test_setup_reset_keeps_downtrend_gate_closed():
    """下跌 setup 归零后 1 根：窗口保护——仍按跌势禁买（防 1 根反弹误开闸）。"""
    # 5 根平盘 + 25 根下跌（setup_buy 累加过 9）→ 最后一根反弹归零
    close = np.concatenate([np.full(5, 100.0), np.arange(99, 74, -1.0), np.array([80.0])])
    sb, ss = _setups(_df(close))
    assert int(sb.iloc[-1]) == 0, f"setup 应已归零，实际 {sb.iloc[-1]}"
    state = classify_trend_state(sb, ss)
    assert state == DOWNTREND, \
        f"归零 1 根应延续跌势禁买（窗口保护），实际 {state}"


def test_long_ranging_after_setup_reset():
    """归零后长时间无方向（>8 根窗口外）→ 弹簧。"""
    # 5 平盘 + 20 下跌（setup 过 9）→ 反弹归零 + 20 根横盘（窗口外无方向）
    down = np.arange(99, 79, -1.0)          # 99..80，20 根
    rng = np.random.default_rng(7)
    flat = 100.0 + rng.normal(0, 0.3, 20)
    close = np.concatenate([np.full(5, 100.0), down, np.full(1, 100.0), flat])
    sb, ss = _setups(_df(close))
    state = classify_trend_state(sb, ss)
    assert state == RANGING, f"窗口外无方向应弹簧，实际 {state}"


def test_insufficient_bars_is_unknown():
    """K 线不足 MIN_BARS → unknown。"""
    close = np.arange(100, 120, 1.0)[:20]
    sb, ss = _setups(_df(close))
    assert classify_trend_state(sb, ss) == UNKNOWN


def test_compute_trend_state_integration():
    """compute_trend_state 返回完整字段。"""
    close = np.concatenate([np.full(5, 100.0), np.arange(99, 74, -1.0)])  # 30 根
    r = compute_trend_state(_df(close))
    assert r["state"] == DOWNTREND
    assert r["label"] == "跌势"
    assert r["setup_buy"] >= 9
    assert r["bars"] == len(close)
    assert r["ts"]
    assert r["peak_buy"] >= r["setup_buy"]
