"""Tests for nanobot_quant.batches — 批次状态机 / FIFO/LIFO / 独立止损止盈 / 持久化."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from nanobot_quant.batches import BatchManager, EXIT_ORDERS


@pytest.fixture
def tmp_path_batches(tmp_path):
    return tmp_path / "batches.json"


@pytest.fixture
def bm(tmp_path_batches):
    return BatchManager(
        symbol="SPCXB",
        account_ids=["acc-1", "acc-2", "acc-3"],
        path=tmp_path_batches,
    )


# ── 初始化 ──────────────────────────────────────────────────────────

def test_init_creates_available_slots(bm):
    assert len(bm.slots) == 3
    assert all(s["status"] == "available" for s in bm.slots)
    assert bm.slots[0]["account_id"] == "acc-1"
    assert bm.slots[2]["slot"] == 3


def test_next_buy_slot_rotates_in_order(bm):
    assert bm.next_buy_slot()["slot"] == 1
    bm.open_lot(qty=5, entry_price=100.0)
    assert bm.next_buy_slot()["slot"] == 2
    bm.open_lot(qty=5, entry_price=101.0)
    assert bm.next_buy_slot()["slot"] == 3
    bm.open_lot(qty=5, entry_price=102.0)
    assert bm.next_buy_slot() is None  # 全部 open


# ── 真分账 v1.1：td_start_slot 起点偏移 / 跳 slot 扫描 ────────────────

def test_next_buy_slot_start_offset(bm):
    """起点偏移：start_slot=2 → 先返回 slot 2（完整循环 + 起点偏移）。"""
    assert bm.next_buy_slot(start_slot=2)["slot"] == 2
    bm.open_lot(qty=5, entry_price=100.0, slot=2)
    # slot 2 open → 从 2 起循环：2(open) → 3 → 1
    assert bm.next_buy_slot(start_slot=2)["slot"] == 3
    bm.open_lot(qty=5, entry_price=100.0, slot=3)
    assert bm.next_buy_slot(start_slot=2)["slot"] == 1


def test_next_buy_slot_start_beyond_n(bm):
    """起点超界（> N）截断到 N，不循环回绕。"""
    assert bm.next_buy_slot(start_slot=99)["slot"] == 3
    assert bm.next_buy_slot(start_slot=0)["slot"] == 1


def test_scan_buy_slots_full_cycle_from_start(bm):
    """scan_buy_slots(3) 顺序：3 → 1 → 2（完整循环，仅 available）。"""
    bm.open_lot(qty=5, entry_price=100.0, slot=2)
    slots = bm.scan_buy_slots(start_slot=3)
    assert [s["slot"] for s in slots] == [3, 1]


def test_scan_buy_slots_all_open_empty(bm):
    for i in range(1, 4):
        bm.open_lot(qty=5, entry_price=100.0, slot=i)
    assert bm.scan_buy_slots(start_slot=1) == []
    assert bm.scan_buy_slots(start_slot=2) == []


def test_scan_buy_slots_empty_manager():
    bm0 = BatchManager(symbol="X", account_ids=[], path=None)
    assert bm0.scan_buy_slots(1) == []
    assert bm0.next_buy_slot(1) is None


# ── 状态机 ──────────────────────────────────────────────────────────

def test_open_lot_records_lot(bm):
    slot = bm.open_lot(qty=5, entry_price=137.2, entry_time="t1")
    assert slot is not None and slot["status"] == "open"
    assert slot["lot"]["qty"] == 5.0
    assert slot["lot"]["entry_price"] == 137.2
    assert slot["lot"]["entry_time"] == "t1"


def test_open_lot_no_slots_left(bm):
    for _ in range(3):
        bm.open_lot(qty=1, entry_price=100.0)
    assert bm.open_lot(qty=1, entry_price=100.0) is None


def test_open_lot_specific_slot_taken(bm):
    bm.open_lot(qty=1, entry_price=100.0, slot=2)
    # slot 2 已被占用，再指定 slot 2 失败
    assert bm.open_lot(qty=1, entry_price=100.0, slot=2) is None
    # 默认分配跳过已占用
    assert bm.open_lot(qty=1, entry_price=100.0)["slot"] == 1


def test_close_lot_recycles_slot(bm):
    s1 = bm.open_lot(qty=5, entry_price=100.0)
    lot = bm.close_lot(s1["slot"])
    assert lot["qty"] == 5.0
    assert bm.slots[0]["status"] == "available"
    assert bm.slots[0]["lot"] is None
    # 回收后 slot 1 再次可用于 BUY
    assert bm.next_buy_slot()["slot"] == 1


def test_close_lot_not_open(bm):
    assert bm.close_lot(1) is None
    bm.open_lot(qty=1, entry_price=100.0, slot=1)
    bm.close_lot(1)
    assert bm.close_lot(1) is None  # 已回收


# ── FIFO / LIFO ─────────────────────────────────────────────────────

def _fill_three(bm):
    """slot1 entry t1, slot2 entry t2, slot3 entry t3（时间递增）。"""
    bm.open_lot(qty=1, entry_price=100.0, entry_time="t1")   # slot 1
    bm.open_lot(qty=1, entry_price=101.0, entry_time="t2")   # slot 2
    bm.open_lot(qty=1, entry_price=102.0, entry_time="t3")   # slot 3


def test_fifo_picks_earliest(bm):
    _fill_three(bm)
    s = bm.pick_exit_slot("fifo")
    assert s["slot"] == 1
    assert s["lot"]["entry_time"] == "t1"


def test_lifo_picks_latest(bm):
    _fill_three(bm)
    s = bm.pick_exit_slot("lifo")
    assert s["slot"] == 3
    assert s["lot"]["entry_time"] == "t3"


def test_pick_exit_no_open(bm):
    assert bm.pick_exit_slot("fifo") is None
    assert bm.pick_exit_slot("lifo") is None


def test_fifo_after_close(bm):
    _fill_three(bm)
    bm.close_lot(1)
    assert bm.pick_exit_slot("fifo")["slot"] == 2


# ── 独立止损 / 止盈 ─────────────────────────────────────────────────

def test_stop_loss_independent(bm):
    """每批独立：只有浮亏 ≥ 10% 的批次命中。"""
    bm.open_lot(qty=1, entry_price=100.0, entry_time="t1")  # 现价 95 → -5%
    bm.open_lot(qty=1, entry_price=110.0, entry_time="t2")  # 现价 95 → -13.6% 命中
    hits = bm.check_exit(price=95.0, stop_loss_pct=0.10)
    assert len(hits) == 1
    assert hits[0]["slot"] == 2
    assert "stop_loss" in hits[0]["_exit_reason"]


def test_take_profit_disabled_by_default(bm):
    bm.open_lot(qty=1, entry_price=100.0, entry_time="t1")
    hits = bm.check_exit(price=105.0, stop_loss_pct=0.10, take_profit_pct=0.0)
    assert hits == []  # take_profit_pct=0 → 关闭


def test_take_profit_hit(bm):
    bm.open_lot(qty=1, entry_price=100.0, entry_time="t1")
    bm.open_lot(qty=1, entry_price=90.0, entry_time="t2")   # 现价 95 → +5.6%
    hits = bm.check_exit(price=95.0, stop_loss_pct=0.10, take_profit_pct=0.05)
    assert len(hits) == 1
    assert hits[0]["slot"] == 2
    assert "take_profit" in hits[0]["_exit_reason"]


def test_exit_sort_respects_order(bm):
    """止损+止盈同时命中多批时，返回顺序按 exit_order。"""
    bm.open_lot(qty=1, entry_price=120.0, entry_time="t1")  # 现价 100 → -16.7% 止损
    bm.open_lot(qty=1, entry_price=80.0, entry_time="t2")   # 现价 100 → +25% 止盈
    hits = bm.check_exit(price=100.0, stop_loss_pct=0.10,
                         take_profit_pct=0.05, order="fifo")
    assert [h["slot"] for h in hits] == [1, 2]
    hits = bm.check_exit(price=100.0, stop_loss_pct=0.10,
                         take_profit_pct=0.05, order="lifo")
    assert [h["slot"] for h in hits] == [2, 1]


# ── 持久化 ──────────────────────────────────────────────────────────

def test_save_load_roundtrip(bm, tmp_path_batches):
    bm.open_lot(qty=5, entry_price=137.2, entry_time="t1")
    bm.save()

    loaded = BatchManager.load(tmp_path_batches)
    assert loaded is not None
    assert loaded.symbol == "SPCXB"
    assert len(loaded.slots) == 3
    assert loaded.slots[0]["status"] == "open"
    assert loaded.slots[0]["lot"]["qty"] == 5.0
    assert loaded.slots[1]["status"] == "available"


def test_load_missing_returns_none(tmp_path):
    assert BatchManager.load(tmp_path / "nope.json") is None


def test_load_corrupt_returns_none(tmp_path_batches):
    tmp_path_batches.write_text("{not json", encoding="utf-8")
    assert BatchManager.load(tmp_path_batches) is None


def test_reload_preserves_open_state(tmp_path_batches):
    bm = BatchManager(symbol="SOL", account_ids=["a", "b"], path=tmp_path_batches)
    bm.open_lot(qty=10, entry_price=76.0, entry_time="t1")
    bm.save()
    loaded = BatchManager.load(tmp_path_batches)
    assert loaded.pick_exit_slot("fifo")["lot"]["qty"] == 10.0


# ── 展示 ────────────────────────────────────────────────────────────

def test_summarize_shape(bm):
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")
    rows = bm.summarize(price=110.0)
    assert len(rows) == 3
    assert rows[0]["status"] == "open"
    assert rows[0]["qty"] == 5.0
    assert rows[0]["pnl_pct"] == pytest.approx(0.10)
    assert rows[1]["pnl_pct"] is None
"""Tests for nanobot_quant.batches — 批次状态机 / FIFO/LIFO / 独立止损止盈 / 持久化."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from nanobot_quant.batches import (
    BatchManager,
    EXIT_ORDERS,
    batches_path,
    migrate_legacy_batches,
)
# ── per-symbol 台账文件（标的池隔离，2026-08-10 定案）──────────────

def test_batches_path_per_symbol():
    # 文件名规则：per-symbol 文件 batches.{symbol}.json；无 symbol → 旧式单文件
    # （根目录不同：/data 存在时无点前缀，home 兜底有点前缀）
    assert str(batches_path("CRCLX")).endswith("batches.CRCLX.json")
    assert str(batches_path("RENDER")).endswith("batches.RENDER.json")
    assert str(batches_path()).endswith("batches.json")


def test_load_per_symbol(monkeypatch, tmp_path):
    d = tmp_path / "legion" / "credentials"
    d.mkdir(parents=True)
    target = d / "batches.SPXCB.json"
    target.write_text(json.dumps({"symbol": "SPXCB", "slots": [{"slot": 1}]}))
    monkeypatch.setattr("nanobot_quant.batches.batches_path",
                        lambda s=None, c=None: d / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json" if s else "batches.json"))
    bm = BatchManager.load(symbol="SPXCB")
    assert bm is not None and bm.symbol == "SPXCB"
    assert BatchManager.load(symbol="OTHER") is None
    # 通道化加载：batches.gate.CRCLX.json 独立读
    gate = d / "batches.gate.CRCLX.json"
    gate.write_text(json.dumps({"symbol": "CRCLX", "slots": [{"slot": 1}]}))
    bg = BatchManager.load(symbol="CRCLX", channel="gate")
    assert bg is not None and bg.channel == "gate"
    assert bg.path == gate


def test_migrate_legacy_batches(monkeypatch, tmp_path):
    d = tmp_path / "legion" / "credentials"
    d.mkdir(parents=True)
    legacy = d / "batches.json"
    legacy.write_text(json.dumps({"symbol": "CRCLX", "slots": [{"slot": 1}]}))
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None: d / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json" if s else "batches.json"),
    )
    migrate_legacy_batches()
    # 旧文件归档到 batches.okx_dex.CRCLX.json（历史 DEX 台账），原路径消失
    assert not legacy.exists()
    assert (d / "batches.okx_dex.CRCLX.json").exists()


def test_migrate_does_not_clobber_existing(monkeypatch, tmp_path):
    d = tmp_path / "legion" / "credentials"
    d.mkdir(parents=True)
    legacy = d / "batches.json"
    legacy.write_text(json.dumps({"symbol": "CRCLX", "slots": [{"slot": 9}]}))
    target = d / "batches.okx_dex.CRCLX.json"
    target.write_text(json.dumps({"symbol": "CRCLX", "slots": [{"slot": 1}]}))
    monkeypatch.setattr(
        "nanobot_quant.batches.batches_path",
        lambda s=None, c=None: d / (f"batches.{c}.{s}.json" if c else f"batches.{s}.json" if s else "batches.json"),
    )
    migrate_legacy_batches()
    # 目标已存在 → 不覆盖，旧文件保留（新文件优先）
    assert legacy.exists()
    raw = json.loads(target.read_text())
    assert raw["slots"][0]["slot"] == 1


