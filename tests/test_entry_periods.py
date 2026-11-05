"""入场多周期 OR（2026-11-05 Step 3a/3b）。

覆盖三件事：
1. `entry_periods` 参数解析/校验/默认值（默认 "1" = 零行为变化）
2. 策略层 `_calc_multi`：默认周期等价基础周期；多周期取 OR（max）
3. 回测侧接线：`multi_warmup` 与 `backtest_config` 必须含该参数
   （否则回测恒单周期——与 IV/F1/EMA 闸门同型的「死门」缺陷）
"""
from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.exec_params import (
    DEFAULT_SCENES,
    PARAM_META,
    SCENE_ONLY_FIELDS,
    parse_entry_periods,
    validate_exec_param,
)


# ── 1. 参数层 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", [1]),
        ("1,2,3,4,5,8", [1, 2, 3, 4, 5, 8]),
        ("8,1", [1, 8]),
        ("1,1,2", [1, 2]),
        ("1，2", [1, 2]),  # 全角逗号
        (" 1 , 3 ", [1, 3]),
        ("", [1]),
        (None, [1]),
        ("abc", [1]),
        ("9", [1]),  # 不在可选集合
        ("0", [1]),
        ([1, 2], [1, 2]),
        (["1", "8"], [1, 8]),
    ],
)
def test_parse_entry_periods(raw, expected):
    assert parse_entry_periods(raw) == expected


def test_entry_periods_param_meta_and_defaults():
    meta = PARAM_META["entry_periods"]
    assert meta["group"] == "scene"
    assert meta["type"] == "str"
    assert meta["std"] == "1"
    assert "entry_periods" in SCENE_ONLY_FIELDS
    for name in ("high", "mid", "low"):
        assert DEFAULT_SCENES[name]["entry_periods"] == "1", f"{name} 默认必须是 1"


def test_validate_entry_periods():
    assert validate_exec_param("entry_periods", "1") is None
    assert validate_exec_param("entry_periods", "1,2,3,4,5,8") is None
    assert validate_exec_param("entry_periods", "8") is None
    assert validate_exec_param("entry_periods", "") is not None
    assert validate_exec_param("entry_periods", "9") is not None
    assert validate_exec_param("entry_periods", "1,x") is not None
    assert validate_exec_param("entry_periods", "1.5") is not None


# ── 2. 策略层 ────────────────────────────────────────────────────────


def _mk_strategy(periods, min_history=120, calc=None):
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL"
    s.sleeptime = "1m"
    s.parameters = {}
    s._min_history = min_history
    s._entry_periods = list(periods)
    s._last_multi = {}
    s._td_params = {}
    if calc is not None:
        s._calc = calc  # 实例属性优先于方法
    return s


def _df(n, start="2026-01-01"):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    base = pd.Series(range(n), index=idx, dtype=float)
    return pd.DataFrame(
        {
            "Open": base + 0.0,
            "High": base + 0.5,
            "Low": base - 0.5,
            "Close": base + 0.2,
            "Volume": base + 10.0,
        }
    )


def test_resample_aggregates_ohlcv_left_aligned():
    s = _mk_strategy([1])
    df = _df(4)
    out = s._resample_bars(df, 2)
    assert len(out) == 2
    first = out.iloc[0]
    assert first["Open"] == pytest.approx(df["Open"].iloc[0])
    assert first["High"] == pytest.approx(df["High"].iloc[:2].max())
    assert first["Low"] == pytest.approx(df["Low"].iloc[:2].min())
    assert first["Close"] == pytest.approx(df["Close"].iloc[1])
    assert first["Volume"] == pytest.approx(df["Volume"].iloc[:2].sum())


def test_calc_multi_default_period_equals_base():
    """默认 [1] 必须与单周期结果逐字段相同（零行为变化）。"""
    calls = []

    def fake_calc(df, news_count=0):
        calls.append(len(df))
        return {"setup_buy": 9, "setup_sell": 1, "cd_buy": 0, "cd_sell": 0, "price": 1.0}

    s = _mk_strategy([1], calc=fake_calc)
    out = s._calc_multi(_df(300))
    assert out["setup_buy"] == 9
    assert out["setup_sell"] == 1
    assert len(calls) == 1, "默认单周期只能算一次"
    assert s._last_multi == {}, "默认路径不产生多周期诊断"


def test_calc_multi_takes_or_of_setup_and_cd_but_keeps_exit_from_base():
    """setup_buy/cd_buy 取各周期 max；出场字段（setup_sell/cd_sell）仍来自基础周期。"""

    def fake_calc(df, news_count=0):
        n = len(df)
        if n > 200:  # 基础周期（tail min_history=120 后仍可能是 300？用长度区分）
            return {"setup_buy": 3, "setup_sell": 2, "cd_buy": 0, "cd_sell": 4, "price": 9.0}
        return {"setup_buy": 9, "setup_sell": 7, "cd_buy": 5, "cd_sell": 6, "price": 9.0}

    s = _mk_strategy([1, 2], min_history=120, calc=fake_calc)
    out = s._calc_multi(_df(240))
    # 基础周期 = tail(120) → len=120 → 走 fake 的第二分支（setup_buy=9, setup_sell=7）
    assert out["setup_buy"] >= 9
    assert out["cd_buy"] >= 5
    # 出场字段必须来自基础周期（120 根分支），不被 2x 周期覆盖
    assert out["setup_sell"] == 7
    assert out["cd_sell"] == 6
    assert set(s._last_multi) == {1, 2}


def test_calc_multi_marks_insufficient_sub_period():
    def fake_calc(df, news_count=0):
        return {"setup_buy": 1, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0, "price": 1.0}

    # 8x 周期只有 240/8 = 30 根 < 12? 不，30 >= 12；用更短 df 触发 skip
    s = _mk_strategy([1, 8], min_history=120, calc=fake_calc)
    s._calc_multi(_df(60))
    assert s._last_multi[8] is None, "子周期不足时必须显式标 skip 而非静默"


# ── 3. 回测接线（死门防护）─────────────────────────────────────────


def test_driver_injects_entry_periods_and_warmup():
    import inspect

    from nanobot_quant.backtest import driver as drv

    src = inspect.getsource(drv.BacktestDriver.__init__)
    assert "parse_entry_periods" in src
    assert "self.multi_warmup" in src
    assert "(max(self.entry_periods) - 1) * self.min_history" in src
    body = inspect.getsource(drv.BacktestDriver)
    assert "extra_warmup_bars=self.warmup_bars + self.multi_warmup" in body

    # 快照必验：必须落在 ``"backtest_config": {`` 块内——只看 whole-source
    # 包含会漏（本批发生过：写到了策略注入 dict，结果/报告看不到生效值）。
    _start = body.index('"backtest_config": {')
    _end = body.index('"entry_setup"', _start)
    assert '"entry_periods"' in body[_start:_end], (
        "entry_periods 必须写进 backtest_config 快照，否则报告按缺省显示「关」"
    )


def test_markdown_marks_single_non_base_period_as_not_off():
    """只有 [1] 才叫「关」；[8] 是「只吃 8 倍周期」（2026-11-05 实测踩到）。"""
    from nanobot_quant.backtest_markdown import render_markdown

    base = {
        "run_id": "t",
        "scene": "high",
        "symbols": ["SOL"],
        "final_net": 400.0,
        "initial_total": 400.0,
        "capital_stats": {},
        "fills_detail": [],
        "slots": {},
        "skip_counts": {},
    }
    off = render_markdown({**base, "backtest_config": {"entry_periods": "1"}})
    assert "关（只吃基础周期）" in off

    eight = render_markdown({**base, "backtest_config": {"entry_periods": "8"}})
    assert "8 倍（任一周期达阈值即建仓）" in eight
    assert "关（只吃基础周期）" not in eight


def test_multi_diagnostics_are_visible_without_spamming():
    """回测不刷屏（live_mode 守卫），但诊断必须进结果（2026-11-05）。"""
    import inspect

    from nanobot_quant.backtest import driver as drv
    from nanobot_quant.strategies import td_sequential_strategy as tss

    body = inspect.getsource(tss.TdSequentialStrategy._calc_multi)
    # 两处 print（MULTI / MULTI WARN）都必须挂在 live_mode 守卫下
    assert body.count('self.parameters.get("live_mode")') >= 2
    assert "_multi_warn_count" in body
    assert "_multi_bar_counts" in body

    dsrc = inspect.getsource(drv.BacktestDriver)
    assert '"multi_window_warnings"' in dsrc
    assert '"entry_periods": "|".join(' in dsrc


def test_strategy_fetch_len_scales_with_periods():
    """取数长度必须按最大倍数放大，否则长周期永远拿不到足够 bar。"""
    import inspect

    from nanobot_quant.strategies import td_sequential_strategy as tss

    src = inspect.getsource(tss.TdSequentialStrategy)
    assert src.count("_entry_periods") >= 3
    assert "* max(_mult, 1)" in src or "* max(" in src
