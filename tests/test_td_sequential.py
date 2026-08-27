"""TD Sequential engine-level tests (original variant).

countdown 持续值语义（2026-08-27）：不满足 +1 条件的 bar 也返回当前
累积值，供策略周期门控区分「countdown 进行中」与「未启动/已结束」。
旧逻辑非 +1 bar 返回 NaN→0，清位条件（reset and cd_buy==0）会在
countdown 跨 setup 翻转累积期间误触发，cd 13 补买绕过周期门控
（LINK 实证：10:10 setup 9 建仓 + 10:14 cd_buy 13 补买，4 根 bar 内两次）。
"""

import numpy as np
import pandas as pd


def _falling_df(n, rebound_at=None, rebound_close=95.0):
    """前 5 根平盘（触发 setup 翻转确认）→ 严格递减。"""
    tail = list(range(95, 95 - (n - 5), -1))
    close = np.array([100.0] * 5 + tail, dtype=float)[:n]
    if rebound_at is not None:
        close[rebound_at] = rebound_close
    return pd.DataFrame({
        "Open": close - 0.3,
        "High": close + 0.5,
        "Low": close - 0.5,
        "Close": close,
        "Volume": np.full(n, 100.0),
    })


def test_countdown_carry_value_on_non_count_bar():
    """非 +1 bar 返回当前累积值（保持），而非 0/NaN。"""
    from nanobot_quant.strategies.td_sequential import _DeMarkEngine
    from nanobot_quant.td_params import DEFAULT_TD_PARAMS

    df = _falling_df(20, rebound_at=14)  # i=14 反弹（close 95）
    engine = _DeMarkEngine(df, dict(DEFAULT_TD_PARAMS))
    out = engine.run_all()
    c = out["buy_countdown_count"]
    # i=13 setup 到 9 启动 countdown 并满足条件 → cd=1
    assert out["buy_setup_count"].iloc[13] == 9
    assert c.iloc[13] == 1
    # i=14 反弹：95 > low[12]=87.5 → 不满足 +1，但保持累积值 1（旧逻辑 NaN→0）
    assert c.iloc[14] == 1, f"非 +1 bar 应保持累积值，实际 {c.iloc[14]}"
    # i=15 恢复下跌：85 <= low[13]=86.5 → +1
    assert c.iloc[15] == 2


def test_countdown_resets_after_complete():
    """countdown 完成 13 后归 0（active_buy 结束），后续 bar 持续 0。"""
    from nanobot_quant.strategies.td_sequential import _DeMarkEngine
    from nanobot_quant.td_params import DEFAULT_TD_PARAMS

    df = _falling_df(40)  # 严格递减：setup 9 后 countdown 每根 +1，i=25 完成 13
    engine = _DeMarkEngine(df, dict(DEFAULT_TD_PARAMS))
    out = engine.run_all()
    c = out["buy_countdown_count"]
    hit = c[c == 13].index
    assert len(hit) == 1, f"应恰好完成一次 countdown 13，实际 {list(hit)}"
    after = c.iloc[hit[0] + 1:]
    assert (after == 0).all(), f"完成 13 后应持续 0，实际 {list(after.head())}"
