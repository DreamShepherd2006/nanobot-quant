"""EOD 日界强平保护期（2026-11-16，docs/quant-system.md §33.68）。

语义：建仓不足 ``eod_protect_hours`` 小时的批次在日界强平时**跳过**
（只看时间、不看盈亏；与 ``eod_mode`` 叠加生效），其余批次照平；
台账缺 ``entry_time`` / 解析失败 → fail-open 放行并计 ``eod_protect_na``。

必须锁死的关系：
- 默认 ``0.0`` = 关 ⇒ 与接线前逐位一致（零行为变化）
- 只作用于 EOD：最短持有期（拦 TD SELL）语义不变，两者互不影响
- 阈值恰等于保护期时**不**保护（``age_h < hours`` 左闭右开）
- 优先级：标的豁免 > 仓位豁免 > 保护期 > mode 盈亏判断
"""
from __future__ import annotations

import pandas as pd

from nanobot_quant.batches import BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bm(tmp_path, lots):
    """lots: [(slot, entry_price, entry_time)]，全部 open。"""
    accounts = [f"acct{i}" for i in range(1, len(lots) + 1)]
    bm = BatchManager(symbol="SOL/USDT", account_ids=accounts,
                      path=tmp_path / "b.json")
    for slot, px, et in lots:
        bm.open_lot(1.0, px, entry_time=et, slot=slot)
    return bm


def _strat(bm, *, protect=0.0, enabled=True, parity="both", mode="all", hour=0):
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL/USDT"
    s.batch_manager = bm
    s._exit_order = "fifo"
    s._sell_all_max = 20
    s._eod_enabled = enabled
    s._eod_parity = parity
    s._eod_mode = mode
    s._eod_hour_utc = hour
    s._eod_protect_hours = protect
    s._eod_last_day = {}
    s._sold = []
    s._skip_counts = {}
    s._sell_lot = lambda slot, price, signal, reason: s._sold.append(
        (slot["slot"], reason))
    s._record = lambda *a, **k: None
    s._bump_skip = lambda k: s._skip_counts.__setitem__(
        k, s._skip_counts.get(k, 0) + 1)
    return s


def _tick(s, ts, price=101.0):
    s._current_bar_time = pd.Timestamp(ts)
    return s._maybe_eod_close(price, {"price": price})


# ── 默认值 / 零行为变化 ──────────────────────────────────────────

def test_default_off_in_class_parameters():
    """类默认必须 0 = 关（新增能力只增加可选择性，零行为变化）。"""
    assert TdSequentialStrategy.parameters.get("eod_protect_hours") == 0.0


def test_protect_off_closes_all(tmp_path):
    s = _strat(
        _bm(tmp_path, [(1, 100.0, "2026-01-02T23:59:00"),
                       (2, 99.0, "2026-01-02T23:59:30")]),
        protect=0.0,
    )
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True
    assert sorted(s_ for s_, _ in s._sold) == [1, 2]
    assert "eod_protect" not in s._skip_counts


# ── 保护 / 放行 ─────────────────────────────────────────────────

def test_protect_blocks_recent_lot(tmp_path):
    """21 分钟前建仓 + 保护期 1 小时 ⇒ 不平，且计数留痕。"""
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T23:40:00")]), protect=1.0)
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is False
    assert s._sold == []
    assert s._skip_counts.get("eod_protect") == 1


def test_protect_passes_old_lot(tmp_path):
    """2 小时前建仓 + 保护期 1 小时 ⇒ 照平。"""
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T22:00:00")]), protect=1.0)
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True
    assert [s_ for s_, _ in s._sold] == [1]
    assert all(r.startswith("eod_close:2026-01-02") for _, r in s._sold)


def test_protect_boundary_exact_hours_not_protected(tmp_path):
    """阈值左闭右开：恰好 N 小时 ⇒ 视为已过保护期（可平）。"""
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T23:01:00")]), protect=1.0)
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True
    assert [s_ for s_, _ in s._sold] == [1]


def test_protect_partial_mixed(tmp_path):
    """部分保护：新批跳过、老批照平，整体仍算「有成交」。"""
    s = _strat(
        _bm(tmp_path, [(1, 100.0, "2026-01-02T23:55:00"),
                       (2, 100.0, "2026-01-01T10:00:00")]),
        protect=2.0,
    )
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True
    assert [s_ for s_, _ in s._sold] == [2]
    assert s._skip_counts.get("eod_protect") == 1
    # 有成交时不计 eod_hold（模式门根本没轮到保护外的那批以下）
    assert "eod_hold" not in s._skip_counts


def test_protect_all_blocked_counts_skip(tmp_path):
    """全部在保护期 ⇒ 返回 False（本轮无动作）+ 保护计数。"""
    s = _strat(
        _bm(tmp_path, [(1, 100.0, "2026-01-02T23:50:00"),
                       (2, 100.0, "2026-01-02T23:51:00")]),
        protect=3.0,
    )
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is False
    assert s._sold == []
    assert s._skip_counts.get("eod_protect") == 2


# ── 与 mode 的叠加 ──────────────────────────────────────────────

def test_protect_beats_profit_only(tmp_path):
    """保护期优先于 mode 判定：刚买的浮盈批在保护期内也不平。"""
    s = _strat(
        _bm(tmp_path, [(1, 90.0, "2026-01-02T23:55:00")]),
        protect=2.0, mode="profit_only",
    )
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01", price=101.0) is False
    assert s._sold == []
    assert s._skip_counts.get("eod_protect") == 1
    assert "eod_hold" not in s._skip_counts


def test_protect_only_applies_to_eod(tmp_path):
    """保护期不改变类默认的 min_hold 语义（只拦 EOD，不碰 TD SELL 通道）。"""
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T23:59:00")]), protect=5.0)
    # min_hold 仍是类默认值，保护期是独立参数
    assert TdSequentialStrategy.parameters.get("min_hold_bars") == 10
    assert s._eod_protect_hours == 5.0


def test_protect_with_nonzero_eod_hour(tmp_path):
    """日界小时 ≠ 0（如 6:00 UTC）时，保护期仍按真实持有时间判定。

    保护期的基准是台账 entry_time、与日界小时无关；这条用例防「日界对齐
    日期被误当成持有时间」的回归。
    """
    s = _strat(
        _bm(tmp_path, [(1, 100.0, "2026-01-02T05:40:00"),    # 21 分钟
                       (2, 100.0, "2026-01-02T02:00:00")]),   # 约 4 小时
        protect=1.0, hour=6,
    )
    _tick(s, "2026-01-02 05:59")
    assert _tick(s, "2026-01-02 06:01") is True
    assert [x for x, _ in s._sold] == [2]
    assert s._skip_counts.get("eod_protect") == 1


# ── 台账缺 entry_time：fail-open + 计数 ─────────────────────────

def test_protect_na_when_entry_time_missing(tmp_path):
    bm = _bm(tmp_path, [(1, 100.0, "2026-01-02T23:55:00")])
    # 注意：字段**完全缺失**会让 batches.pick_all_slots 的排序 KeyError（既有行为，
    # 与保护期无关）；保护期的 fail-open 覆盖的是「值为 None / 不可解析」。
    bm.slots[0]["lot"]["entry_time"] = None
    s = _strat(bm, protect=6.0)
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True   # fail-open 放行
    assert [s_ for s_, _ in s._sold] == [1]
    assert s._skip_counts.get("eod_protect_na") == 1


def test_protect_na_when_entry_time_unparsable(tmp_path):
    bm = _bm(tmp_path, [(1, 100.0, "2026-01-02T23:55:00")])
    bm.slots[0]["lot"]["entry_time"] = "not-a-timestamp"
    s = _strat(bm, protect=6.0)
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True
    assert s._skip_counts.get("eod_protect_na") == 1


# ── 优先级：豁免 > 保护期 ───────────────────────────────────────

def test_symbol_exempt_precedes_protect(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T23:59:00")]), protect=6.0)
    s._eod_exempt = frozenset({"SOL/USDT"})
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is False
    assert s._skip_counts.get("eod_exempt") == 1
    assert "eod_protect" not in s._skip_counts


def test_slot_exempt_precedes_protect(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0, "2026-01-02T23:59:00")]), protect=6.0)
    s._eod_exempt_slots_all = frozenset({1})
    s._eod_exempt_slots_by_symbol = {}
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is False
    assert s._skip_counts.get("eod_exempt_slots") == 1
    assert "eod_protect" not in s._skip_counts
