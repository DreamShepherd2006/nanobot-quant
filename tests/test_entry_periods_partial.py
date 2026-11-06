"""多周期信号口径 `entry_periods_partial`（2026-11-06 Step 1）测试。

用户定调：使用 8m 周期的目的是拿到「8m 周期的买9/卖9 信号」，必须等 8m 跑完
才准确 —— TD 计数判据是 ``close[i] vs close[i-cmp]``，未收盘 bar 的 close 是
瞬时值，partial bar 会让同一个 8m 窗口内的计数 9→8→9 反复、提前触发。

- ``False``（默认）：只吃已收盘的倍数 bar → 与研究脚本
  ``close_idx = sub.index + minutes`` 口径一致，且信号序列与重放窗口无关
  （可预计算 → 回测提速）。
- ``True``：保留旧行为（resample 最后一根含未走完的 base bar）。

完整性判据不是「丢最后一行」：最后一根若刚好走完（base bar 落在倍数边界上）
必须保留，否则会白滞后一整根——本文件专门锁这个边界。
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.exec_params import (
    DEFAULT_SCENES,
    PARAM_META,
    SCENE_FIELD_ORDER,
    SCENE_ONLY_FIELDS,
)
from nanobot_quant.strategies.td_sequential_strategy import (
    TdSequentialStrategy,
    as_bool_flag,
)


# ── 夹具 ────────────────────────────────────────────────────────────────


def _mk(partial: bool = False, sleeptime: str = "1m") -> TdSequentialStrategy:
    """绕过 lumibot 构造函数（不建 broker），只补本用例需要的字段。"""
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.parameters = {"sleeptime": sleeptime}
    s.symbol = "SOL"
    s.sleeptime = sleeptime
    s._entry_periods_partial = partial
    return s


def _df(n: int, tz: str | None = "UTC") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01 00:00", periods=n, freq="1min", tz=tz)
    closes = [100.0 + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c + 1 for c in closes],
            "Low": [c - 1 for c in closes],
            "Close": closes,
            "Volume": [10.0] * n,
        },
        index=idx,
    )


# ── 完整性判据 ──────────────────────────────────────────────────────────


def test_partial_false_drops_incomplete_bar():
    """9 根 1m = 1 根完整 8m（00:00）+ 1 根进行中（00:08）→ 只留完整那根。"""
    s = _mk(partial=False)
    out = s._resample_bars(_df(9), 8)
    assert out is not None
    assert len(out) == 1
    assert out.index[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC")


def test_partial_false_keeps_bar_exactly_on_boundary():
    """8 根 1m 恰好走完一个 8m bar（00:00–00:08 收盘）→ 必须保留，不能白丢。"""
    s = _mk(partial=False)
    out = s._resample_bars(_df(8), 8)
    assert out is not None
    assert len(out) == 1
    assert out.index[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC")


def test_partial_false_handles_multiple_complete_bars():
    """17 根 1m → 2 根完整 8m + 1 根进行中。"""
    s = _mk(partial=False)
    out = s._resample_bars(_df(17), 8)
    assert out is not None
    assert [t.strftime("%H:%M") for t in out.index] == ["00:00", "00:08"]


def test_partial_true_keeps_incomplete_bar():
    """旧行为：进行中那根也保留（9 根 → 2 行）。"""
    s = _mk(partial=True)
    out = s._resample_bars(_df(9), 8)
    assert out is not None
    assert len(out) == 2


def test_partial_false_works_on_naive_index():
    """无时区索引同样成立（A股/部分源）。"""
    s = _mk(partial=False)
    out = s._resample_bars(_df(9, tz=None), 4)
    assert out is not None
    assert [t.strftime("%H:%M") for t in out.index] == ["00:00", "00:04"]


def test_mult_1_is_identity():
    """m=1 不聚合（_calc_multi 对 base 周期直接 _calc，不走这里，但语义要成立）。"""
    s = _mk(partial=False)
    out = s._resample_bars(_df(5), 1)
    assert out is not None
    assert len(out) == 5


# ── bool 解析（单一实现，供策略与回测共用） ─────────────────────────────


def test_as_bool_flag_parses_json_strings():
    assert as_bool_flag(False) is False
    assert as_bool_flag(True) is True
    assert as_bool_flag(None, False) is False
    assert as_bool_flag(None, True) is True
    # Python 的 bool("false") 是 True —— 这正是要挡住的静默错误
    assert as_bool_flag("false") is False
    assert as_bool_flag("False") is False
    assert as_bool_flag("0") is False
    assert as_bool_flag("off") is False
    assert as_bool_flag("true") is True
    assert as_bool_flag("1") is True
    assert as_bool_flag(0) is False
    assert as_bool_flag(1) is True


# ── 参数注册（三处：校验 / 展示 / 执行） ────────────────────────────────


def test_param_registered_in_exec_params():
    assert "entry_periods_partial" in SCENE_ONLY_FIELDS
    assert "entry_periods_partial" in SCENE_FIELD_ORDER
    assert "entry_periods_partial" in PARAM_META
    meta = PARAM_META["entry_periods_partial"]
    assert meta["type"] == "bool"
    assert meta["std"] is False


def test_all_default_scenes_partial_false():
    """默认 false = 整根；三个场景无一例外（零行为变化的前提）。"""
    for name in ("high", "mid", "low"):
        assert DEFAULT_SCENES[name]["entry_periods_partial"] is False
