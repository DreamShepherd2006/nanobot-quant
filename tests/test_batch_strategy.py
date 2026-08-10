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
    # 真分账 v1.1 mock：switch 成功 / 资金充足 / 还原目标固定（单测不触 CLI）
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._home_account = "acc-home"
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


# ── 真分账 v1.1：资金不足跳 slot / 起点偏移 / switch 还原 ────────────

def test_batch_buy_skips_insufficient_slot(tmp_path):
    """slot 1 资金不足 → 跳过 → slot 2 买入（拍板 1）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    switch_calls: list[str] = []
    s._wallet_switch = lambda account_id: (switch_calls.append(account_id), True)[1]

    def _bal(quote_symbol="USDC"):
        # slot 1（acc-1）资金不足；其余充足
        return 0.0 if switch_calls and switch_calls[-1] == "acc-1" else 1e9
    s._slot_quote_balance = _bal

    s.on_trading_iteration()
    assert s._captured["order"][2] == "buy"
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["slot"] == 2  # 跳过了 slot 1
    assert open_slots[0]["lot"]["qty"] == s._captured["order"][1]
    # switch 序列：acc-1（查资金，不足）→ 还原 acc-home → acc-2（下单）→ 还原 acc-home
    assert switch_calls[0] == "acc-1"
    assert "acc-2" in switch_calls
    assert switch_calls.count("acc-home") == 2  # 每次交易后还原
    assert switch_calls[-1] == "acc-home"


def test_batch_buy_all_slots_poor_skips_buy(tmp_path):
    """全部 slot 资金不足 → 无订单（TD BATCH 跳过，slot 保持 available）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s._slot_quote_balance = lambda quote_symbol="USDC": 0.0
    s.on_trading_iteration()
    assert "order" not in s._captured
    assert all(x["status"] == "available" for x in bm.slots)


def test_batch_buy_start_slot_offset(tmp_path):
    """td_start_slot=2 → 第一次 BUY 落到 slot 2（拍板 3）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()), td_start_slot=2)
    s.on_trading_iteration()
    assert s._captured["order"][2] == "buy"
    assert bm.open_slots()[0]["slot"] == 2


def test_batch_buy_start_slot_wraps_after_open(tmp_path):
    """起点 slot 已 open → 从起点循环找下一 available（2→3→1）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1", slot=2)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()), td_start_slot=2)
    s.on_trading_iteration()
    assert len(bm.open_slots()) == 2          # 原 slot 2 + 新买入
    assert bm.slots[2]["status"] == "open"   # 新买入落到 slot 3


def test_batch_sell_switches_slot_and_restores(tmp_path):
    """SELL 前 switch 到该批次子钱包，交易后还原默认账户。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")  # slot 1
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    switch_calls: list[str] = []
    s._wallet_switch = lambda account_id: (switch_calls.append(account_id), True)[1]
    s.on_trading_iteration()
    assert s._captured["order"][2] == "sell"
    assert switch_calls[0] == "acc-1"      # 平 slot 1 前 switch 到其账户
    assert switch_calls[-1] == "acc-home"  # 交易后还原


def test_batch_sell_float_qty_not_truncated(tmp_path):
    """lot.qty 为小数（0.05）→ 卖出量保留小数（修复 int 截断）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=66.0, entry_time="t1")  # 如 0.05 CRCLX
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s.on_trading_iteration()
    assert s._captured["order"][1] == 0.05
    assert bm.slots[0]["status"] == "available"
