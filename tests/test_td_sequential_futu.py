"""TD Sequential — 富途 NINE「神奇九转」口径变体（setup-only）测试。

对照三向口径（原版继续累加 / 同花顺 1-9 循环 / 富途恰好触发一次）：
- 无翻转确认：连续满足即数（原版/同花顺要求 setup 从价格翻转后开始）
- 9 后继续累加但不重复触发：信号恰在 count == setup_period 的那根出现一次
- 无 countdown / TDST；score = setup_count / setup_period（0–1 尺度）
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.strategies.td_sequential import calculate as td_calculate
from nanobot_quant.strategies.td_sequential_cycle import calculate as cycle_calculate
from nanobot_quant.strategies.td_sequential_futu import (
    FutuDeMarkEngine,
    calculate as futu_calculate,
)


def _falling_df(n_drop: int = 9) -> pd.DataFrame:
    """Rising 5 bars then a sustained downtrend — guarantees a Buy Setup."""
    closes = list(range(100, 105)) + list(range(100, 100 - n_drop, -1))
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _sustained_fall_df(n: int = 12) -> pd.DataFrame:
    """Monotonic downtrend from bar 0 — no flip for the base/cycle variants.

    close[i] < close[i-4] holds from the first comparable bar onward and the
    previous bar also satisfies the setup condition, so the base variant's
    flip-confirmation never fires while futu counts immediately.
    """
    closes = list(range(120, 120 - n, -1))
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _rising_df(n_rise: int = 9) -> pd.DataFrame:
    """Falling 5 bars then a sustained uptrend — guarantees a Sell Setup."""
    closes = list(range(100, 95, -1)) + list(range(100, 100 + n_rise))
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


# ── 计数行为 ─────────────────────────────────────────────────────────


def test_futu_accumulates_past_nine():
    # 12 consecutive falling bars → futu keeps counting (12), unlike cycle (3)
    r = futu_calculate(_falling_df(12))
    assert r["setup_buy"] == 12


def test_futu_completes_at_nine():
    # exactly 9 falling bars → setup 9 + BUY + score 1.0
    r = futu_calculate(_falling_df(9))
    assert r["setup_buy"] == 9
    assert r["recommendation"] == "BUY (Setup Complete)"
    assert r["score"] == 1.0


def test_futu_no_flip_confirmation():
    # sustained fall: base variant requires a prior-bar flip to start counting
    # (stays 0), futu counts immediately (close < close[i-4] is satisfied).
    r_futu = futu_calculate(_sustained_fall_df(12))
    r_base = td_calculate(_sustained_fall_df(12))
    assert r_futu["setup_buy"] > 0
    assert r_base["setup_buy"] == 0


def test_futu_equal_close_resets():
    # close[i] == close[i-cmp] is NOT a setup bar → counter resets to 0.
    closes = [100, 99, 98, 97, 96, 95, 98, 94, 93, 92, 91, 90]
    df = pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    engine = FutuDeMarkEngine(df, None)
    out = engine.run_all()
    assert int(out["buy_setup_count"].iloc[6]) == 0   # equal close → reset
    assert int(out["buy_setup_count"].iloc[7]) == 1   # next satisfying bar → 1


def test_futu_sell_side():
    r = futu_calculate(_rising_df(9))
    assert r["setup_sell"] == 9
    assert r["recommendation"] == "SELL (Setup Complete)"


# ── 信号触发频率（与 cycle 的实质差异） ──────────────────────────────


def test_futu_fires_once_per_long_trend():
    df = _falling_df(30)
    engine = FutuDeMarkEngine(df, None)
    out = engine.run_all()
    nines = int((out["buy_setup_count"] == 9).sum())
    buy_signals = int((out["recommendation"] == "BUY (Setup Complete)").sum())
    assert nines == 1          # count hits exactly 9 once, then 10, 11, …
    assert buy_signals == 1    # fires only on the count==9 bar


def test_futu_vs_cycle_fire_counts():
    # Same 30-bar trend: cycle fires 3× (bars 9/18/27), futu fires 1×.
    df = _falling_df(30)
    cyc = CycleEngineCount(df)
    fut = FutuEngineCount(df)
    assert cyc == 3
    assert fut == 1


def CycleEngineCount(df):
    from nanobot_quant.strategies.td_sequential_cycle import CycleDeMarkEngine
    return int((CycleDeMarkEngine(df, None).run_all()["recommendation"] == "BUY (Setup Complete)").sum())


def FutuEngineCount(df):
    return int((FutuDeMarkEngine(df, None).run_all()["recommendation"] == "BUY (Setup Complete)").sum())


# ── 评分 / 输出契约 ──────────────────────────────────────────────────


def test_futu_score_scale():
    # 5 falling bars → score = 5/9 ≈ 0.56 (normalised, not 0–28.75)
    r = futu_calculate(_falling_df(5))
    assert r["setup_buy"] == 5
    assert r["score"] is not None and 0.5 < r["score"] < 0.6


def test_futu_no_countdown_no_tdst():
    r = futu_calculate(_falling_df(12))
    assert r["cd_buy"] == 0
    assert r["cd_sell"] == 0
    assert r["tdst_support"] is None
    assert r["tdst_resistance"] is None


def test_futu_output_contract():
    r = futu_calculate(_falling_df(12))
    for key in ("timestamp", "price", "recommendation", "setup_buy", "setup_sell",
                "cd_buy", "cd_sell", "tdst_support", "tdst_resistance", "rvol", "score"):
        assert key in r


def test_futu_custom_setup_period():
    # setup_period=5 → signal fires at count==5, score 1.0
    r = futu_calculate(_falling_df(5), params={"setup_period": 5})
    assert r["setup_buy"] == 5
    assert r["recommendation"] == "BUY (Setup Complete)"
    assert r["score"] == 1.0
