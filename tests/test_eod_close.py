"""EOD 单双号强平（2026-11-05，Step 2）。

研究口径（docs/quant-system.md §33.58）：单号日 24:00（UTC）持仓不动、双号日 24:00
强平全部仓位。本实现按**标的独立**判定（每个标的自己的 bar 时间跨日才触发自己全平），
避免多标的共用一份状态时后评估的标的被漏掉。

与既有门控的关系（必须锁死）：
- `eod_mode=all` **绕过** `min_hold` 与高9 盈利门（日界强平不受它们约束）
- `eod_mode=profit_only` 只平 `entry_price < price` 的批，其余计 `eod_hold`
- 默认 `eod_enabled=False` ⇒ 整个方法第一行返回，零行为变化

时区口径：日期号取「日界对齐后的 UTC 日期」（`ts - eod_hour_utc 小时`），
换北京时区整轮差约 1.66pp（2026-11-05 实测），故此处显式按 UTC 锁定。
"""
from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.batches import BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bm(tmp_path, lots):
    """lots: [(slot, entry_price)]，全部 open。"""
    accounts = [f"acct{i}" for i in range(1, len(lots) + 1)]
    bm = BatchManager(symbol="SOL/USDT", account_ids=accounts,
                      path=tmp_path / "b.json")
    for slot, px in lots:
        bm.open_lot(1.0, px, entry_time="2026-01-01T00:00:00", slot=slot)
    return bm


def _strat(bm, *, enabled=True, parity="even", mode="all", hour=0):
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL/USDT"
    s.batch_manager = bm
    s._exit_order = "fifo"
    s._sell_all_max = 20
    s._eod_enabled = enabled
    s._eod_parity = parity
    s._eod_mode = mode
    s._eod_hour_utc = hour
    s._eod_last_day = {}
    s._sold = []
    s._skip_counts = {}
    s._sell_lot = lambda slot, price, signal, reason: s._sold.append(
        (slot["slot"], reason))
    s._record = lambda *a, **k: None
    s._bump_skip = lambda k: s._skip_counts.__setitem__(
        k, s._skip_counts.get(k, 0) + 1)
    return s


def _tick(s, ts, price=101.0, signal=None):
    """推进一根 bar 并跑一次 EOD 判定。"""
    s._current_bar_time = pd.Timestamp(ts)
    return s._maybe_eod_close(price, signal or {"price": price})


# ── 奇偶判定 ────────────────────────────────────────────────────

@pytest.mark.parametrize("parity,day,expect", [
    ("even", pd.Timestamp("2026-01-02").date(), True),
    ("even", pd.Timestamp("2026-01-01").date(), False),
    ("odd", pd.Timestamp("2026-01-01").date(), True),
    ("odd", pd.Timestamp("2026-01-02").date(), False),
    ("both", pd.Timestamp("2026-01-02").date(), True),
    ("both", pd.Timestamp("2026-01-01").date(), True),
    ("EVEN", pd.Timestamp("2026-01-02").date(), True),  # 大小写不敏感
])
def test_parity_match(parity, day, expect):
    s = _strat(None, parity=parity)
    assert s._eod_parity_match(day) is expect


# ── 触发边界 ────────────────────────────────────────────────────

def test_disabled_never_fires(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0)]), enabled=False)
    assert _tick(s, "2026-01-01 23:59") is False
    assert _tick(s, "2026-01-02 00:01") is False
    assert s._sold == []


def test_first_bar_only_records_day(tmp_path):
    """首根 bar 没有 previous day ⇒ 只记录、不触发（避免启动即强平）。"""
    s = _strat(_bm(tmp_path, [(1, 100.0)]))
    assert _tick(s, "2026-01-02 23:59") is False
    assert s._sold == []


def test_same_day_no_fire(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0)]))
    _tick(s, "2026-01-02 00:01")   # 首根，记录 prev=None
    assert _tick(s, "2026-01-02 12:00") is False
    assert s._sold == []


def test_even_parity_fires_after_even_day(tmp_path):
    """1/1（单号）跨到 1/2 不触发；1/2（双号）跨到 1/3 触发全平。"""
    s = _strat(_bm(tmp_path, [(1, 100.0), (2, 99.0)]))
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is False    # 刚结束 1/1 = 单号
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True     # 刚结束 1/2 = 双号
    assert [s_ for s_, _ in s._sold] == [1, 2]
    assert all(r.startswith("eod_close:2026-01-02") for _, r in s._sold)


def test_odd_parity_fires_after_odd_day(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0)]), parity="odd")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is True      # 刚结束 1/1 = 单号


def test_both_parity_fires_every_day(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0)]), parity="both")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is True


def test_no_fire_when_no_open_lots(tmp_path):
    s = _strat(_bm(tmp_path, []), parity="both")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is False


# ── 模式 ────────────────────────────────────────────────────────

def test_all_mode_closes_losers_too(tmp_path):
    """all 模式绕过盈利门：浮亏批也平（研究口径，默认关的原因）。"""
    s = _strat(_bm(tmp_path, [(1, 100.0), (2, 105.0)]), parity="both")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01", price=101.0) is True
    assert sorted(s_ for s_, _ in s._sold) == [1, 2]


def test_profit_only_skips_losers(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 100.0), (2, 105.0)]),
               parity="both", mode="profit_only")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01", price=101.0) is True
    assert [s_ for s_, _ in s._sold] == [1]          # 只平 100 那批
    assert s._skip_counts.get("eod_hold") is None    # 有成交就不计 hold


def test_profit_only_all_blocked_counts_skip(tmp_path):
    s = _strat(_bm(tmp_path, [(1, 105.0), (2, 110.0)]),
               parity="both", mode="profit_only")
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01", price=101.0) is False
    assert s._sold == []
    assert s._skip_counts.get("eod_hold") == 1


# ── 日界小时偏移 ────────────────────────────────────────────────

def test_hour_offset_shifts_trading_day(tmp_path):
    """eod_hour_utc=8 ⇒ 交易日 = 08:00~次日 07:59；跨 08:00 才换日。"""
    s = _strat(_bm(tmp_path, [(1, 100.0)]), parity="both", hour=8)
    _tick(s, "2026-01-02 07:59")     # 交易日 = 1/1
    assert _tick(s, "2026-01-02 08:01") is True   # 交易日 1/1 → 1/2
    # 同一天内 09:00 → 23:00 不再触发
    assert _tick(s, "2026-01-02 23:00") is False


def test_hour_offset_negative_wrap(tmp_path):
    """边界值（如 23 点）也能正确取日期号，不抛异常。"""
    s = _strat(_bm(tmp_path, [(1, 100.0)]), parity="both", hour=23)
    _tick(s, "2026-01-02 22:59")
    assert _tick(s, "2026-01-02 23:01") is True


# ── 与既有参数交互 ──────────────────────────────────────────────

def test_exit_order_min_entry_respected(tmp_path):
    """EOD 全平也走 exit_order 排序（保持与普通出场一致的可预测性）。"""
    bm = _bm(tmp_path, [(1, 100.0), (2, 98.0), (3, 102.0)])
    s = _strat(bm, parity="both")
    s._exit_order = "min_entry"
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is True
    assert [s_ for s_, _ in s._sold] == [2, 1, 3]   # 入场价升序


def test_default_disabled_in_class_parameters():
    """类默认必须关——开启等于允许亏损平仓，需用户显式拍板。"""
    assert TdSequentialStrategy.parameters.get("eod_enabled", False) is False
    assert TdSequentialStrategy.parameters.get("eod_parity", "even") == "even"
    assert TdSequentialStrategy.parameters.get("eod_mode", "all") == "all"
    assert TdSequentialStrategy.parameters.get("eod_hour_utc", 0) == 0
