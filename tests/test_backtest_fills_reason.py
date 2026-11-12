"""回测 ``fills_detail.reason`` 记**触发周期自身**的读数（2026-11-12 C141）。

背景：C140 让台账「入场信号」改记触发周期自身的计数，但回测的成交明细
原因串仍用跨周期聚合 ``max`` + ``score``（``TD LONG setup_buy=12 cd_buy=0
score=0.8``），两处口径不一致、回测无法核验 C140。

本文件的契约：
  ① 原因串格式 = ``TD LONG setup_buy=<触发周期计数> cd_buy=<...> period=<倍数>``
  ② 读数取触发周期自身（``_cur_entry_period`` + ``_last_multi``），不是 max
  ③ 拿不到周期读数（单周期 / 旧路径）时用传入的聚合值、``period=1``
  ④ 任何异常退回旧格式，绝不影响下单
"""

from __future__ import annotations

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _mk(**kw) -> TdSequentialStrategy:
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s._cur_entry_period = None
    s._entry_periods = [1, 2, 3]
    s._last_multi = {}
    s._td_params = {"entry_setup": 9, "entry_countdown": 13}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── ① 触发周期读数优先于聚合 max ─────────────────────────────────────────


def test_reason_uses_trigger_period_not_max():
    # 3m 的 30 是 max，真正触发的是 2m（12）
    s = _mk(_cur_entry_period=2, _last_multi={1: (0, 0), 2: (12, 0), 3: (30, 0)})
    assert s._long_reason_of(30, 0) == "TD LONG setup_buy=12 cd_buy=0 period=2"


def test_reason_carries_cd_counts():
    s = _mk(_cur_entry_period=5, _last_multi={1: (0, 0), 5: (3, 13)})
    assert s._long_reason_of(7, 13) == "TD LONG setup_buy=3 cd_buy=13 period=5"


def test_reason_no_longer_contains_score():
    s = _mk(_cur_entry_period=1, _last_multi={1: (9, 0)})
    out = s._long_reason_of(9, 0)
    assert "score" not in out
    assert out == "TD LONG setup_buy=9 cd_buy=0 period=1"


# ── ② 无周期读数时回退聚合值 ─────────────────────────────────────────────


def test_reason_falls_back_to_aggregate_when_single_period():
    s = _mk(_cur_entry_period=None, _last_multi={})
    assert s._long_reason_of(9, 0) == "TD LONG setup_buy=9 cd_buy=0 period=1"


def test_reason_falls_back_when_period_missing_from_multi():
    # 触发周期有值但该周期不在 _last_multi（异常态）→ 用传入的聚合值
    s = _mk(_cur_entry_period=4, _last_multi={1: (9, 0)})
    assert s._long_reason_of(11, 2) == "TD LONG setup_buy=11 cd_buy=2 period=4"


# ── ③ 异常不得影响下单 ───────────────────────────────────────────────────


def test_reason_bad_values_fall_back_to_legacy_format():
    s = _mk(_cur_entry_period=None, _last_multi={})
    assert s._long_reason_of("bad", None) == "TD LONG setup_buy=bad"


def test_reason_never_raises():
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)  # 缺全部状态字段
    out = s._long_reason_of(9, 0)
    assert isinstance(out, str) and out.startswith("TD LONG")
