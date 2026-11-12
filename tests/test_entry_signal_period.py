"""台账「入场信号」记**触发周期自身**的计数（2026-11-12 C140）。

背景（实盘核对）：多周期入场（``entry_periods=1,2,3,4,5,8``）下，策略决策
用的是各周期独立判定（方案 A），但写台账的 ``entry_reason`` 用的是跨周期
聚合值 ``max``：SOL slot2 台账写「入场信号 32 / 入场周期 2m」，而 2m 当时只有
15、32 其实是 1m 的计数 —— **数字真实但归属周期错**。

本文件的契约：
  ① 记触发周期自身的 setup 计数，不是 max
  ② cd 通路同理（记触发周期自身的 countdown）
  ③ 下单快照 ``order.custom_params['entry_signal']`` 优先（pending 确认在
     后续轮也不会读到漂移值）
  ④ 拿不到周期读数时退回聚合值（单周期 / 旧路径零回归）
  ⑤ 任何异常都不得影响下单（纯记录）
"""

from __future__ import annotations

import types

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _mk(**kw) -> TdSequentialStrategy:
    """最轻量实例：``_entry_signal_of`` 只读这几个状态字段。"""
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s._cur_entry_period = None
    s._entry_periods = [1, 2, 3]
    s._last_multi = {}
    s._td_params = {"entry_setup": 9, "entry_countdown": 13}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _order(custom_params=None):
    return type(
        "Order",
        (),
        {"identifier": "o1", "custom_params": custom_params, "error": None},
    )()


# ── ① setup 通路 ──────────────────────────────────────────────────────────


def test_records_trigger_period_count_not_max():
    # 3m 的 30 是 max，但真正触发的是 2m（12）——台账必须记 12
    s = _mk(
        _cur_entry_period=2,
        _last_multi={1: (0, 0), 2: (12, 0), 3: (30, 0)},
    )
    assert s._entry_reason_of(30, 0, 9, 13) == "setup_buy=12"


def test_period_and_counts_returned():
    s = _mk(
        _cur_entry_period=8,
        _last_multi={1: (0, 0), 8: (11, 2)},
    )
    assert s._entry_signal_of() == (8, 11, 2)


# ── ② cd 通路 ────────────────────────────────────────────────────────────


def test_cd_path_uses_trigger_period_countdown():
    # setup 全线未达 9，只有 3m 的 countdown 到 13
    s = _mk(
        _cur_entry_period=3,
        _last_multi={1: (0, 0), 3: (3, 13)},
    )
    assert s._entry_reason_of(3, 13, 9, 13) == "cd_buy=13"


def test_cd_path_ignores_other_periods_setup_max():
    # 5m setup 是 8（max）但未达阈值；2m 的 cd 到 13 触发 → 记 cd_buy=13
    s = _mk(
        _cur_entry_period=2,
        _last_multi={2: (0, 13), 5: (8, 0)},
    )
    assert s._entry_reason_of(8, 13, 9, 13) == "cd_buy=13"


# ── ③ 下单快照优先（pending 场景） ───────────────────────────────────────


def test_order_snapshot_wins_over_drifted_state():
    # 后续轮 _last_multi 已漂移成 0，但仍要记下单那一刻的 11
    s = _mk(_cur_entry_period=2, _last_multi={2: (0, 0)})
    order = _order({"entry_signal": {"period": 2, "setup_buy": 11, "cd_buy": 0}})
    assert s._entry_reason_of(99, 0, 9, 13, order) == "setup_buy=11"
    assert s._entry_signal_of(order) == (2, 11, 0)


def test_cex_submit_snapshots_trigger_period_readings():
    s = _mk(
        _cur_entry_period=2,
        _last_multi={1: (0, 0), 2: (12, 1), 3: (30, 0)},
    )
    order = _order()
    s.create_order = lambda *a, **k: order
    s._cex_slot_broker = lambda slot: types.SimpleNamespace(
        submit_order=lambda o: o
    )
    req = types.SimpleNamespace(
        asset=object(), quantity=1.0, action="buy", reason="TD LONG setup_buy=30"
    )
    out = s._cex_submit({}, req)
    assert out.custom_params["entry_period"] == 2
    assert out.custom_params["entry_signal"] == {
        "period": 2,
        "setup_buy": 12,
        "cd_buy": 1,
    }


# ── ④ 回退（单周期 / 旧路径零回归） ─────────────────────────────────────


def test_single_period_falls_back_to_aggregate():
    s = _mk(_entry_periods=[1], _last_multi={})
    assert s._entry_reason_of(9, 0, 9, 13) == "setup_buy=9"


def test_no_detail_falls_back_to_aggregate():
    s = _mk(_cur_entry_period=None, _entry_periods=[1, 4], _last_multi={})
    assert s._entry_signal_of(fallback=(7, 2)) == (None, 7, 2)
    assert s._entry_reason_of(7, 2, 9, 13) == "cd_buy=2"


def test_empty_last_multi_uses_cur_period_and_fallback():
    s = _mk(_cur_entry_period=4, _last_multi={})
    assert s._entry_signal_of() == (4, None, None)
    assert s._entry_reason_of(10, 0, 9, 13) == "setup_buy=10"


# ── ⑤ 异常安全 ──────────────────────────────────────────────────────────


def test_exception_paths_are_safe():
    s = _mk(_cur_entry_period=2, _last_multi={2: (12, 0)})
    s._entry_period_hit = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    assert s._entry_signal_of() == (2, None, None)
    assert s._entry_reason_of(9, 0, 9, 13) == "setup_buy=9"


def test_garbage_order_custom_params_is_safe():
    s = _mk(_cur_entry_period=2, _last_multi={2: (12, 0)})
    order = types.SimpleNamespace(custom_params="not-a-dict")
    assert s._entry_reason_of(30, 0, 9, 13, order) == "setup_buy=12"
