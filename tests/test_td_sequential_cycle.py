"""TD Sequential — 同花顺九转口径变体（setup 1-9 循环）测试。"""

from __future__ import annotations

import pandas as pd
from nanobot_quant.strategies.td_sequential import calculate as td_calculate
from nanobot_quant.strategies.td_sequential_cycle import calculate as cycle_calculate


def _falling_df(n_drop: int = 9) -> pd.DataFrame:
    """Rising 5 bars then a sustained downtrend — guarantees a Buy Setup."""
    closes = list(range(100, 105)) + list(range(100, 100 - n_drop, -1))
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def test_cycle_recycles_after_nine():
    # 12 consecutive falling bars → 1-9 loop: count = 3 on the last bar
    r = cycle_calculate(_falling_df(12))
    assert r["setup_buy"] == 3


def test_base_variant_keeps_accumulating():
    # base variant (production) keeps counting: 12 falling bars → 12
    r = td_calculate(_falling_df(12))
    assert r["setup_buy"] == 12


def test_cycle_completes_at_nine():
    # exactly 9 falling bars → setup 9 on the last bar
    r = cycle_calculate(_falling_df(9))
    assert r["setup_buy"] == 9


def test_cycle_fires_nine_multiple_times_in_long_trend():
    from nanobot_quant.strategies.td_sequential_cycle import CycleDeMarkEngine
    from nanobot_quant.td_params import DEFAULT_TD_PARAMS

    df = _falling_df(30)
    engine = CycleDeMarkEngine(df, dict(DEFAULT_TD_PARAMS))
    out = engine.run_all()
    nines = int((out["buy_setup_count"] == 9).sum())
    assert nines == 3  # bars 9, 18, 27


def test_cycle_output_contract():
    r = cycle_calculate(_falling_df(12))
    for key in ("timestamp", "price", "recommendation", "setup_buy", "setup_sell",
                "cd_buy", "cd_sell", "tdst_support", "tdst_resistance", "rvol", "score"):
        assert key in r
