"""TdSequentialStrategy 分批模式（批次=子钱包，第一版）测试。

覆盖：
- BUY 占用 available slot 并记录 lot
- 全部 slot open 时不再买入
- SELL 信号按 exit_order 平一个批次（FIFO / LIFO）
- 独立止损 / 止盈逐批平仓（take_profit_pct=0 关闭）
- 平仓后 slot 回收复用
"""

from __future__ import annotations

import logging

import pandas as pd

from nanobot_quant.batches import BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bars_with(closes: list[float]):
    from lumibot.entities import Bars

    df = pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2025-01-01", periods=len(closes), freq="D"),
    )
    return Bars(df, "ONCHAIN", None)


def _oscillate() -> list[float]:
    """41 根交替震荡——不触发 setup_buy/setup_sell。"""
    return [100.0 + (i % 2) * 2 for i in range(41)]


def _buy_closes() -> list[float]:
    return _oscillate() + [100.0 - i for i in range(1, 14)]


def _sell_closes() -> list[float]:
    return _oscillate() + [100.0 + i for i in range(1, 14)]


def _make_batch_strategy(bm: BatchManager, bars, **params) -> TdSequentialStrategy:
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-batch-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = bars
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars

    captured: dict = {}

    def _create_order(asset, quantity, action):
        captured["order"] = (asset, quantity, action)
        return type("Order", (), {"identifier": "mock-id", "quantity": quantity})()

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = bm  # 注入 → 分批模式
    s.initialize()
    s._captured = captured
    return s


def _make_bm(tmp_path, n: int = 3) -> BatchManager:
    return BatchManager(
        symbol="SPCXB",
        account_ids=[f"acc-{i}" for i in range(1, n + 1)],
        path=tmp_path / "batches.json",
    )


# ── BUY：占用 slot ───────────────────────────────────────────────────

def test_batch_buy_occupies_slot(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()
    assert "order" in s._captured
    _, qty, action = s._captured["order"]
    assert action == "buy"
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["slot"] == 1
    assert open_slots[0]["lot"]["qty"] == qty
    assert open_slots[0]["lot"]["entry_price"] > 0


def test_batch_buy_accumulates_multiple_lots(tmp_path):
    """连续两次 BUY 信号 → 两个批次（多仓位累积）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()
    s.on_trading_iteration()
    assert len(bm.open_slots()) == 2
    assert [x["slot"] for x in bm.open_slots()] == [1, 2]


def test_batch_no_buy_when_all_slots_open(tmp_path):
    bm = _make_bm(tmp_path, n=2)
    bm.open_lot(qty=5, entry_price=80.0, entry_time="t1")  # slot 1（浮盈）
    bm.open_lot(qty=5, entry_price=80.0, entry_time="t2")  # slot 2（浮盈）
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()
    assert "order" not in s._captured  # 无 available slot → 不加仓（且浮盈无止盈）


# ── SELL：按 exit_order 平一个批次 ───────────────────────────────────

def test_batch_sell_fifo_picks_earliest(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")  # slot 1（最早）
    bm.open_lot(qty=5, entry_price=101.0, entry_time="t2")  # slot 2
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s.on_trading_iteration()
    assert "order" in s._captured
    _, qty, action = s._captured["order"]
    assert action == "sell"
    assert qty == 5  # 平 slot 1 的 lot.qty
    assert bm.slots[0]["status"] == "available"  # slot 1 已平
    assert bm.slots[1]["status"] == "open"       # slot 2 保留
    assert bm.slots[1]["lot"]["qty"] == 5.0


def test_batch_sell_lifo_picks_latest(tmp_path):
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")
    bm.open_lot(qty=7, entry_price=101.0, entry_time="t2")
    s = _make_batch_strategy(
        bm, _bars_with(_sell_closes()), exit_order="lifo")
    s.on_trading_iteration()
    assert s._captured["order"][1] == 7  # 平 slot 2（最新）
    assert bm.slots[1]["status"] == "available"
    assert bm.slots[0]["status"] == "open"


def test_batch_sell_no_open_slots_no_order(tmp_path):
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s.on_trading_iteration()
    assert "order" not in s._captured  # 无 open 批次 → SELL 信号无目标


# ── 独立止损 / 止盈 ──────────────────────────────────────────────────

def test_batch_stop_loss_independent(tmp_path):
    """slot 1 浮亏 -20%（entry=100, 现价=80）→ 只平 slot 1；slot 2 浮盈保留。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")  # slot 1
    bm.open_lot(qty=5, entry_price=70.0, entry_time="t2")   # slot 2（+14%）
    closes = _oscillate() + [100.0 + (i % 2) * 2 for i in range(12)] + [80.0]
    s = _make_batch_strategy(bm, _bars_with(closes), stop_loss_pct=0.10)
    s.on_trading_iteration()
    assert s._captured["order"][1] == 5
    assert s._captured["order"][2] == "sell"
    assert bm.slots[0]["status"] == "available"  # 止损平掉 slot 1
    assert bm.slots[1]["status"] == "open"       # slot 2 保留


def test_batch_take_profit_disabled_by_default(tmp_path):
    """take_profit_pct=0（默认）→ 浮盈不触发卖出。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=80.0, entry_time="t1")  # 现价 100 → +25%
    closes = _oscillate() + [100.0 + (i % 2) * 2 for i in range(12)] + [100.0]
    s = _make_batch_strategy(bm, _bars_with(closes), stop_loss_pct=0.10)
    s.on_trading_iteration()
    assert "order" not in s._captured  # 无 TD 信号、无止盈、无止损 → HOLD


def test_batch_take_profit_hit(tmp_path):
    """take_profit_pct=0.05 → 浮盈 +25% 批次被平。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=80.0, entry_time="t1")
    closes = _oscillate() + [100.0 + (i % 2) * 2 for i in range(12)] + [100.0]
    s = _make_batch_strategy(
        bm, _bars_with(closes), stop_loss_pct=0.10, take_profit_pct=0.05)
    s.on_trading_iteration()
    assert s._captured["order"][2] == "sell"
    assert bm.slots[0]["status"] == "available"


# ── 回收复用 ─────────────────────────────────────────────────────────

def test_batch_slot_reuse_after_close(tmp_path):
    """平仓后 slot 回收，下一 BUY 信号复用同一 slot。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()  # BUY → slot 1 open
    bm.close_lot(1)           # 模拟平仓 → slot 1 回收
    s._captured.clear()
    s.on_trading_iteration()  # 再 BUY
    assert "order" in s._captured
    assert bm.open_slots()[0]["slot"] == 1
