"""exit_order="min_entry"（2026-11-05，Step 1，纯回测/策略侧新增，默认关）。

语义：High9 触发时，从所有 open 批次中**先筛过盈利门的**、再取**入场价最低**的那一仓。
与 fifo/lifo 的关键差异：后者是「选一个 → 门拦则整轮 SKIP」，被拦的批次不会再被考虑；
min_entry 是「遍历候选 → 跳过被拦的 → 取第一个通过的」，等价于"每轮尽量平掉最便宜的盈利仓"。

默认 fifo 时三条路径行为**逐字节不变**（本文件同时锁定这条回归）。
"""
from __future__ import annotations

import pytest

from nanobot_quant.batches import EXIT_ORDERS, BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bm(tmp_path, lots):
    """lots: [(slot, entry_price, entry_time)]"""
    accounts = [f"acct{i}" for i in range(1, len(lots) + 1)]
    bm = BatchManager(symbol="SOL/USDT", account_ids=accounts,
                      path=tmp_path / "b.json")
    for slot, px, ts in lots:
        bm.open_lot(1.0, px, entry_time=ts, slot=slot)
    return bm


# ── 批次层：排序 ────────────────────────────────────────────────

def test_exit_orders_includes_min_entry():
    assert "min_entry" in EXIT_ORDERS


def test_pick_exit_slot_min_entry_picks_cheapest(tmp_path):
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
        (3, 102.0, "2026-01-03T00:00:00"),
    ])
    assert bm.pick_exit_slot("min_entry")["slot"] == 2


def test_pick_exit_slot_fifo_lifo_unchanged(tmp_path):
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
        (3, 102.0, "2026-01-03T00:00:00"),
    ])
    assert bm.pick_exit_slot("fifo")["slot"] == 1
    assert bm.pick_exit_slot("lifo")["slot"] == 3


def test_pick_all_slots_min_entry_sorted_ascending(tmp_path):
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
        (3, 102.0, "2026-01-03T00:00:00"),
    ])
    assert [s["slot"] for s in bm.pick_all_slots("min_entry")] == [2, 1, 3]


def test_check_exit_min_entry_orders_hits_by_price(tmp_path):
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
        (3, 102.0, "2026-01-03T00:00:00"),
    ])
    hits = bm.check_exit(price=104.0, take_profit_pct=0.01, order="min_entry",
                         fee_rate=0.0)
    assert [h["slot"] for h in hits] == [2, 1, 3]


# ── 策略层：先筛门、再取最低 ────────────────────────────────────

def _strat(min_hold_bars=0, high=0.002, low=0.0, momentum=False):
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL/USDT"
    s._min_hold_bars = min_hold_bars
    s._min_hold_until = {}
    s._sell_only_profit_high = high
    s._sell_only_profit_low = low
    s._momentum_exit = momentum
    s._cd_stall_n = 3
    s._cd_stall_state = {}
    s._current_bar_time = None
    return s


def test_gated_slot_picks_cheapest_profitable(tmp_path):
    """现价 101：100/98 两批盈利（+1%/+3%）、102 浮亏 → 取 98 那批。"""
    s = _strat(high=0.002)
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
        (3, 102.0, "2026-01-03T00:00:00"),
    ])
    slot, hold_skipped, gate_blocked = s._pick_gated_slot(
        bm, 101.0, setup_sell=9, cd_sell=0)
    assert slot is not None and slot["slot"] == 2
    assert hold_skipped == 0 and gate_blocked == 0


def test_gated_slot_skips_blocked_and_tries_next(tmp_path):
    """核心语义差异：最便宜那批被 min_hold 拦 → 继续找下一批（fifo/lifo 会整轮 SKIP）。"""
    import pandas as pd

    s = _strat(min_hold_bars=10)
    s._current_bar_time = pd.Timestamp("2026-01-01 00:05")
    # slot1（最便宜）仍在最短持有期内，slot2 已解禁
    s._min_hold_until = {("SOL/USDT", 1): pd.Timestamp("2026-01-01 00:10")}
    bm = _bm(tmp_path, [
        (1, 98.0, "2026-01-01T00:00:00"),
        (2, 99.5, "2026-01-02T00:00:00"),
        (3, 100.0, "2026-01-03T00:00:00"),
    ])
    slot, hold_skipped, gate_blocked = s._pick_gated_slot(bm, 101.0, setup_sell=9, cd_sell=0)
    assert slot is not None and slot["slot"] == 2
    assert hold_skipped == 1 and gate_blocked == 0


def test_gated_slot_all_unprofitable_returns_none(tmp_path):
    s = _strat(high=0.002)
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 102.0, "2026-01-02T00:00:00"),
    ])
    slot, _, gate_blocked = s._pick_gated_slot(bm, 99.0, setup_sell=9, cd_sell=0)
    assert slot is None and gate_blocked == 2


def test_gated_slot_min_hold_skips_all(tmp_path):
    """全部未满 min_hold → (None, N, 0)，不误记为盈利门拦截。"""
    import pandas as pd

    s = _strat(min_hold_bars=10)
    s._current_bar_time = pd.Timestamp("2026-01-01 00:05")
    until = pd.Timestamp("2026-01-01 00:10")
    s._min_hold_until = {("SOL/USDT", 1): until, ("SOL/USDT", 2): until}
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
    ])
    slot, hold_skipped, gate_blocked = s._pick_gated_slot(
        bm, 101.0, setup_sell=9, cd_sell=0)
    assert slot is None
    assert hold_skipped == 2 and gate_blocked == 0


def test_gated_slot_gate_disabled_sells_cheapest(tmp_path):
    """盈利门关闭（high=0）→ 不过滤，直接取最便宜的一批。"""
    s = _strat(high=0.0)
    bm = _bm(tmp_path, [
        (1, 100.0, "2026-01-01T00:00:00"),
        (2, 98.0, "2026-01-02T00:00:00"),
    ])
    slot, _, _ = s._pick_gated_slot(bm, 90.0, setup_sell=9, cd_sell=0)
    assert slot is not None and slot["slot"] == 2


def test_gated_slot_empty_book(tmp_path):
    s = _strat(high=0.002)
    bm = _bm(tmp_path, [(1, 100.0, "2026-01-01T00:00:00")])
    bm.close_lot(1)
    slot, hold_skipped, gate_blocked = s._pick_gated_slot(
        bm, 101.0, setup_sell=9, cd_sell=0)
    assert slot is None and hold_skipped == 0 and gate_blocked == 0
