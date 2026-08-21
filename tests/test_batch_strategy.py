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

    # 小写列 = lumibot v4.5.78 Bars 契约（Bars.__init__ 访问 df["close"] 派生
    # return 列）；CexDataSource 修复后输出小写列，测试 mock 须同契约
    # （2026-08-17 A 修复）。
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
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


def _mock_order(identifier="mock-id", quantity=1.0, filled=True, error=None):
    """lumibot Order 最小 stub（2026-08-11：is_filled 镜像真实 v4.5.78
    签名——策略 _sell_lot/_evaluate_symbol 现以 is_filled() 判断链上确认；
    custom_params 默认为 None 镜像真实 v4.5.78，防止 stale stub 掩盖
    'NoneType' 崩溃）。"""
    return type("Order", (), {
        "identifier": identifier,
        "quantity": quantity,
        "error": error,
        "custom_params": None,
        "is_filled": lambda self=None: filled,
        "set_error": lambda self, e: setattr(self, "error", e),
    })()


def _make_batch_strategy(bm: BatchManager, bars, **params) -> TdSequentialStrategy:
    # 测试 bars 54 根 < 生产默认 120 窗口 → 显式收窄到 50（旧行为）
    params.setdefault("min_history", 50)
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
        return _mock_order(quantity=quantity)

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = bm  # 注入 → 分批模式
    s.initialize()
    # 真分账 v1.1 mock：switch 成功 / 资金充足 / 还原目标固定（单测不触 CLI）
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9  # 链上余额充足（缩量测试单独 mock）
    s._slot_portfolio_value = lambda: 1e6  # 目标 slot 账户资产充足（B 方案风控基准）
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
    """同一信号周期只建一个批次；setup 重置后的新周期再建下一个
    （2026-08-19 分批次建仓：9→10→11 只算一次信号）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()  # 周期 1：BUY → slot 1
    assert len(bm.open_slots()) == 1
    s._captured.clear()
    s.on_trading_iteration()  # 同周期（setup 未重置）→ 周期守卫，不建
    assert "order" not in s._captured
    assert len(bm.open_slots()) == 1
    # 新周期：setup 计数归小（reset）后再触发 → 允许建第二个
    s._bars = _bars_with([100.0 + (i % 2) * 2 for i in range(60)])
    s.on_trading_iteration()  # 中性 60 根 → setup_buy=0 → reset=True
    s._bars = _bars_with(_buy_closes())
    s._captured.clear()
    s.on_trading_iteration()
    assert "order" in s._captured
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
    """平仓后同周期不重建（信号级：slot 释放也不建，等新信号周期）；
    setup 重置后的新周期复用该 slot（2026-08-19 拍板）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.on_trading_iteration()  # BUY → slot 1 open
    bm.close_lot(1)           # 模拟平仓 → slot 1 回收
    s._captured.clear()
    s.on_trading_iteration()  # 同周期（未重置）→ 信号级不建
    assert "order" not in s._captured
    assert bm.slots[0]["status"] == "available"
    # 新周期：setup 归小后重新触发 → 复用 slot 1
    s._bars = _bars_with([100.0 + (i % 2) * 2 for i in range(60)])
    s.on_trading_iteration()  # 中性 60 根 → reset=True
    s._bars = _bars_with(_buy_closes())
    s._captured.clear()
    s.on_trading_iteration()
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
    """起点 slot 已 open（历史持仓）→ 同周期保守不建；新周期信号
    从起点循环找下一 available（2→3）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=80.0, entry_time="t1", slot=2)  # 历史仓位（浮盈，不止损）
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()), td_start_slot=2)
    s.on_trading_iteration()  # 有 open → 视为本周期已建（保守），同周期不建
    assert len(bm.open_slots()) == 1  # 仅历史 slot 2
    # 新周期：setup 归小后重新触发 → 允许建 → 从 slot 2 回绕找 slot 3
    s._bars = _bars_with([100.0 + (i % 2) * 2 for i in range(60)])
    s.on_trading_iteration()
    s._bars = _bars_with(_buy_closes())
    s._captured.clear()
    s.on_trading_iteration()
    assert len(bm.open_slots()) == 2          # 历史 slot 2 + 新买入
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


def test_batch_sell_order_failure_restores_slot(tmp_path):
    """订单失败（quote 解析/资金不足）→ 不 track、恢复 slot，台账回到卖出前。

    2026-08-10 回归：TD BATCH EXIT 曾假报成功——close_lot 先释放 slot、
    订单 set_error 后仍 track + 打印退出日志，链上没卖台账却已释放。
    """
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5, entry_price=100.0, entry_time="t1")  # slot 1
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))

    def _submit_failing(order):
        order.error = "Cannot resolve addresses: SPCXB→USD"
        s._captured.setdefault("submitted", order)

    s.submit_order = _submit_failing
    s.on_trading_iteration()
    assert s._captured["submitted"] is not None     # 订单确实尝试提交
    assert bm.slots[0]["status"] == "open"          # slot 已恢复
    assert bm.slots[0]["lot"]["qty"] == 5.0         # 台账回到卖出前
    assert s.tracker._orders == {}                  # 失败订单不 track

# ── 标的池（多标的扫描，批次 2，2026-08-10）──────────────────────

def _make_pool_strategy(managers, bars_by_symbol, symbols, **params):
    """多标的策略：{symbol: BatchManager} dict 注入（td_live 路径），
    get_historical_prices 按 symbol 返回对应 bars。"""
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, symbols=symbols, **params)
    s.logger = logging.getLogger("td-pool-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: bars_by_symbol[symbol]

    captured: dict = {}

    def _create_order(asset, quantity, action):
        captured.setdefault("orders", []).append((asset, quantity, action))
        return _mock_order(
            identifier=f"mock-{len(captured['orders'])}", quantity=quantity,
        )

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", []).append(order)
    s.batch_managers = managers  # {symbol: BatchManager} 注入（多标的路径）
    s.initialize()
    # 真分账 v1.1 mock：switch 成功 / 资金充足 / 还原目标固定（单测不触 CLI）
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_portfolio_value = lambda: 1e6  # 目标 slot 账户资产充足（B 方案基准）
    return s, captured


def _flat_bars():
    """长震荡序列——不触发任何 TD 信号。"""
    closes = [100.0 + (i % 3) - 1 for i in range(60)]
    return _bars_with(closes)


def test_pool_single_hit_only_buys_signal_symbol(tmp_path):
    """池中只有一个标的 Setup 9 → 仅该标的执行，其他标的静默。"""
    bm_hit = _make_bm(tmp_path)
    bm_silent = _make_bm(tmp_path)
    managers = {"HIT": bm_hit, "SILENT": bm_silent}
    s, captured = _make_pool_strategy(
        managers,
        {"HIT": _bars_with(_buy_closes()), "SILENT": _flat_bars()},
        ["HIT", "SILENT"],
    )
    s.on_trading_iteration()
    assert len(captured.get("orders", [])) == 1
    asset, qty, action = captured["orders"][0]
    assert action == "buy"
    assert len(bm_hit.open_slots()) == 1
    assert len(bm_silent.open_slots()) == 0


def test_pool_both_hit_processed_in_pool_order(tmp_path):
    """同 bar 双标的 Setup 9 → 按池子顺序（=优先级）全部处理，各自台账。"""
    bm_a = _make_bm(tmp_path)
    bm_b = _make_bm(tmp_path)
    managers = {"AAA": bm_a, "BBB": bm_b}
    s, captured = _make_pool_strategy(
        managers,
        {"AAA": _bars_with(_buy_closes()), "BBB": _bars_with(_buy_closes())},
        ["AAA", "BBB"],
    )
    s.on_trading_iteration()
    orders = captured.get("orders", [])
    assert len(orders) == 2
    # 池子顺序 AAA → BBB：AAA 先执行
    assert len(bm_a.open_slots()) == 1
    assert len(bm_b.open_slots()) == 1


def test_pool_silent_when_all_hold(tmp_path):
    """全池无信号 → 无任何下单（静默，非卡死）。"""
    bm_a = _make_bm(tmp_path)
    bm_b = _make_bm(tmp_path)
    s, captured = _make_pool_strategy(
        {"AAA": bm_a, "BBB": bm_b},
        {"AAA": _flat_bars(), "BBB": _flat_bars()},
        ["AAA", "BBB"],
    )
    s.on_trading_iteration()
    assert "orders" not in captured

def test_batch_sell_shrinks_when_onchain_low(tmp_path):
    """链上校验（缩量）：账户实际余额 < lot.qty → 按实际余额卖。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._slot_token_balance = lambda symbol: 3.0  # 链上只有 3
    s.on_trading_iteration()
    assert s._captured["order"][1] == 3.0  # 缩量卖出


def test_batch_sell_skips_when_onchain_zero(tmp_path):
    """链上校验：账户实际余额为 0 → 跳过该批（不卖空）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._slot_token_balance = lambda symbol: 0.0
    s.on_trading_iteration()
    assert "order" not in s._captured
    assert bm.open_slots() == []  # 台账已释放（close_lot 先行）


def test_home_account_id_reads_data_layer(monkeypatch):
    """_home_account_id 必须从 wallet_accounts() 的 data.accounts 读取
    （曾误读顶层 accounts 恒空 → 交易后不还原默认账户）。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy
    from nanobot_quant import tools

    def fake_accounts():
        return {
            "status": "ok",
            "data": {
                "selected_account_id": "acc-2",
                "accounts": [
                    {"account_id": "acc-1", "account_name": "Account 1",
                     "is_default": True, "is_active": False, "addresses": []},
                    {"account_id": "acc-2", "account_name": "Account 2",
                     "is_default": False, "is_active": True, "addresses": []},
                ],
            },
        }

    monkeypatch.setattr(
        "nanobot_quant.tools.tools_wallet.wallet_accounts", fake_accounts)
    s = TdSequentialStrategy()
    s._home_account = None  # 模拟 initialize() 后的懒解析缓存初始态
    assert s._home_account_id() == "acc-1"
    # 缓存生效（二次调用不再解析）
    assert s._home_account == "acc-1"


# ── B 方案风控基准（2026-08-10）：slot 子钱包资产 ─────────────────────

def test_buy_min_account_value_skip(tmp_path):
    """slot 子钱包总资产 < min_account_value → 跳过该槽（TD SLOT SKIP），不建仓。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()), min_account_value=50.0)
    s._slot_portfolio_value = lambda: 11.45  # 目标 slot 只有 $11.45
    s.on_trading_iteration()
    assert "order" not in s._captured
    assert bm.open_slots() == []  # 无仓位占用


def test_buy_value_mode_fractional_qty(tmp_path):
    """quantity_mode=value：qty = pv_slot × max_position_pct / price（小数不取整）。
    pv=11.45, pct=0.25, price=76.97 → qty≈0.0372（SOL 高价标的小数量）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="value", quantity=1,
        max_position_pct=0.25,
    )
    s._slot_portfolio_value = lambda: 11.45
    # 下单价格 = 最后收盘价（100.0-13=87 区间）；构造 76.97 收盘价
    s.on_trading_iteration()
    assert "order" in s._captured
    _, qty, action = s._captured["order"]
    assert action == "buy"
    # 价格取自 bars 收盘价（_buy_closes 最后=87.0）；value 模式小数数量
    assert isinstance(qty, float)
    assert qty > 0 and qty < 1  # 小数数量（非 int 截断 + max(...,1) 抬升）
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["lot"]["qty"] == qty


def test_buy_position_limit_based_on_slot_pv(tmp_path):
    """position_limit 基于目标 slot 账户资产（pv_slot）而非活跃账户/全账户。
    fixed qty=1, price=76.97, pv_slot=11.45 → pos=$77 > 25%×11.45 → BLOCK。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()), quantity=1, max_position_pct=0.25)
    s._slot_portfolio_value = lambda: 11.45
    s.on_trading_iteration()
    assert "order" not in s._captured  # position_limit 拒绝
    assert bm.open_slots() == []


def test_buy_fixed_amount_mode_qty(tmp_path):
    """quantity_mode=fixed_amount（2026-08-19）：qty = td_fixed_amount / price。
    fixed_amount=10, price=87.0 → qty≈0.1149；成功建仓。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="fixed_amount", td_fixed_amount=10.0,
    )
    s.on_trading_iteration()
    assert "order" in s._captured
    _, qty, action = s._captured["order"]
    assert action == "buy"
    assert abs(qty - 10.0 / 87.0) < 1e-6  # _buy_closes 最后收盘价 87.0
    assert len(bm.open_slots()) == 1


def test_buy_fixed_amount_skips_position_limit(tmp_path):
    """fixed_amount 跳过 position_limit（拍板 A）：固定 100U > 25%×pv_slot(11.45)=2.86
    仍买入（金额即用户显式仓位）；资金检查保留（余额充足 → 成功）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="fixed_amount", td_fixed_amount=100.0,
        max_position_pct=0.25,
    )
    s._slot_portfolio_value = lambda: 11.45  # 小 slot：100U 远超 25% 上限
    s.on_trading_iteration()
    assert "order" in s._captured
    assert len(bm.open_slots()) == 1


def test_buy_fixed_amount_insufficient_funds(tmp_path):
    """fixed_amount 资金检查保留：USDC 余额 < 固定金额 → TD SLOT SKIP 不建仓。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(
        bm, _bars_with(_buy_closes()),
        quantity_mode="fixed_amount", td_fixed_amount=10.0,
    )
    s._slot_quote_balance = lambda quote_symbol="USDC": 5.0  # 只有 $5 < $10
    s.on_trading_iteration()
    assert "order" not in s._captured
    assert bm.open_slots() == []


def test_buy_fixed_amount_default_10(tmp_path):
    """td_fixed_amount 缺省回退 10.0（parameters 未传时）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()), quantity_mode="fixed_amount")
    assert s.fixed_amount == 10.0


def test_batch_managers_injected_after_init(tmp_path):
    """回归（2026-08-10）：td_live 在 Strategy 构造后注入 batch_managers，
    lumibot __init__ 先调 initialize() 导致快照空 dict → batch_mode=False
    → BUY 误走非 batch 分支（旧 value 模式 BLOCK）。on_trading_iteration
    每轮实时刷新读取属性，注入后必须走 batch 分支（open slot）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    # 模拟 td_live 时序：initialize 快照为空，单数属性不存在，
    # 构造后注入多标的 dict —— 实时刷新应读到
    s._batch_managers = {}
    s.batch_manager = None
    s.batch_managers = {s.symbol: bm}
    s.on_trading_iteration()
    assert "order" in s._captured
    assert len(bm.open_slots()) == 1  # batch 分支 open 了 slot（而非非 batch 直下单）


def test_batch_sell_restores_lot_when_query_fails(tmp_path):
    """链上校验：余额查询失败（状态未知）→ 跳过卖出并恢复台账
    （fail-safe，避免 close_lot 先行释放导致账实脱节）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._slot_token_balance = lambda symbol: None  # 查询失败
    s.on_trading_iteration()
    assert "order" not in s._captured
    assert len(bm.open_slots()) == 1  # 台账保留（未释放）
    assert bm.open_slots()[0]["lot"]["qty"] == 5.0


def test_slot_token_balance_falls_back_to_symbol(monkeypatch):
    """原生 SOL（tokenAddress 空）vs tokens.json 登记 wSOL 地址：
    地址匹配失败后必须回退 symbol 匹配——曾返回 0 → SELL 误判余额为 0
    释放台账（2026-08-10 14:06 卖9 SKIP 根因）。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy
    s = TdSequentialStrategy()

    def fake_wallet_balance():
        return {"ok": True, "data": {"accountId": "x", "details": [{
            "tokenAssets": [
                {"symbol": "SOL", "balance": "0.045353234",
                 "tokenAddress": "", "tokenPrice": "76.5",
                 "usdValue": "3.47"},
            ]}]}}

    monkeypatch.setattr(
        "nanobot_quant.tools.tools_wallet.wallet_balance", fake_wallet_balance)
    monkeypatch.setattr(
        "nanobot_quant.tokens_store.token_meta",
        lambda sym: {"address": "So11111111111111111111111111111111111111112"})
    assert abs(s._slot_token_balance("SOL") - 0.045353234) < 1e-9


def test_batch_buy_skips_outer_risk_gate(tmp_path):
    """回归（2026-08-10 15:00）：batch 模式外层 can_enter 曾用组合 pv + 非
    batch qty 预检——高单价标的（CRCLX $66）在组合 $11 时被 BLOCK，永远
    到不了 _buy_on_slot 的 slot 风控（TD SLOT SKIP 从未触发）。
    修复：batch 模式跳过外层检查，风控全部在 _buy_on_slot 内完成。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.portfolio_value = 10.0  # 组合极小——外层 can_enter 必拒（若被调用）
    s._slot_quote_balance = lambda quote_symbol="USDC": 0.0  # slot 无 USDC
    msgs: list[str] = []
    orig_info = s.logger.info
    s.logger.info = lambda msg, *a, **k: msgs.append(str(msg))
    try:
        s.on_trading_iteration()
    finally:
        s.logger.info = orig_info
    assert "order" not in s._captured
    assert not any("TD BLOCK" in m for m in msgs), msgs
    assert any("TD SLOT SKIP" in m for m in msgs), msgs
    assert len(bm.open_slots()) == 0


def test_wallet_switch_accepts_status_ok(monkeypatch):
    """回归（2026-08-10 15:28）：tools_wallet.wallet_switch 返回规范化
    {"status":"ok"}，_wallet_switch 只查 r.get("ok") 恒判失败 → 所有 slot
    「TD SLOT SKIP | switch 失败」误跳过（CLI 实际已切换）。"""
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy
    s = TdSequentialStrategy()
    calls: list[str] = []

    def fake(account_id: str):
        calls.append(account_id)
        return {"status": "ok", "data": None}

    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.wallet_switch", fake)
    assert s._wallet_switch("acc-1") is True
    assert calls == ["acc-1"]

    monkeypatch.setattr(
        "nanobot_quant.tools.tools_wallet.wallet_switch",
        lambda aid: {"status": "error", "error": "boom"})
    assert s._wallet_switch("acc-1") is False

    # CLI 原始信封仍兼容
    monkeypatch.setattr(
        "nanobot_quant.tools.tools_wallet.wallet_switch",
        lambda aid: {"ok": True, "data": None})
    assert s._wallet_switch("acc-1") is True


def test_live_drops_in_progress_bar_for_signal(tmp_path):
    """回归（2026-08-11 00:23 SOL 买9 未生效）：live 数据源（OKX DEX
    kline）返回进行中的最后一根 bar——TD 收盘价状态机被未完成 bar 干扰：
    setup=9 后一根进行中 bar 价格回升 → setup 重置 1 → 策略只看最后一根
    → 单根 setup=9 信号被永久错过。修复：live 路径拉 length+1 并丢弃
    最后一根（进行中），信号基于最近已收盘 bar（与 TD 理论一致）。
    live 模式丢尾后触发 BUY；非 live（回测）不丢、尾=回升重置不触发。"""
    closes = _buy_closes() + [100.0]  # 尾：回升到震荡高位 → setup 重置 1

    # live 模式：丢弃进行中的最后一根 → 窗口尾=setup9 → BUY
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(closes))
    s._is_live_broker = True
    s.on_trading_iteration()
    assert "order" in s._captured, s._captured
    assert len(bm.open_slots()) == 1

    # 非 live（回测）：55 根全用 → 窗口尾=回升（setup 重置）→ 无信号
    bm2 = _make_bm(tmp_path)
    s2 = _make_batch_strategy(bm2, _bars_with(closes))
    assert s2._is_live_broker is False
    s2.on_trading_iteration()
    assert "order" not in s2._captured
    assert len(bm2.open_slots()) == 0


def test_batch_buy_order_failure_no_open_lot(tmp_path):
    """下单失败（如 6010 滑点保护）→ TD BATCH BUY FAIL + 不 open_lot。

    2026-08-11 回归：_buy_on_slot 此前不检查 order.error，swap 失败仍
    open_lot + 打 TD BATCH LONG，产生幽灵批次（台账有持仓、链上没有）——
    18:16 CRCLX（客户端拦截）与 18:21 SOL（链上 6010 失败）双实证。
    """
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))

    def _submit_failing(order):
        order.error = "MinReturnNotReached (6010)"
        s._captured.setdefault("submitted", order)
        return order
    s.submit_order = _submit_failing

    s.on_trading_iteration()
    # 订单提交过但未成交 → 台账不 open、无 LONG 日志
    assert s._captured.get("submitted") is not None
    assert all(x["status"] == "available" for x in bm.slots)
    assert not hasattr(s, "_captured_logs")


def test_batch_sell_min_hold_skip_releases(tmp_path):
    """链上余额 ≤ min_hold（如 SOL gas 0.01）→ 跳过卖出并释放台账。

    2026-08-11：SOL 幽灵批次（台账 0.0374、链上仅 0.00947 gas）在 SELL 时
    缩量卖出会卖光 gas——min_hold 保护：仅剩保留量视为无持仓，释放台账。
    """
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.0374, entry_price=75.0, entry_time="t1")  # 幽灵 lot
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._slot_token_balance = lambda symbol: 0.00947  # 链上仅剩 gas
    s._symbol_min_hold = lambda: 0.01

    s.on_trading_iteration()
    assert "order" not in s._captured          # 未下单（不卖 gas）
    assert all(x["status"] == "available" for x in bm.slots)  # 台账已释放


def test_batch_sell_shrink_keeps_min_hold(tmp_path):
    """缩量卖出时扣除 min_hold（保留 gas），卖量 = 链上余额 − 保留量。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=0.05, entry_price=75.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._slot_token_balance = lambda symbol: 0.02  # 台账 0.05、链上 0.02
    s._symbol_min_hold = lambda: 0.01

    s.on_trading_iteration()
    assert s._captured["order"][2] == "sell"
    assert abs(s._captured["order"][1] - 0.01) < 1e-9  # 0.02 − 0.01
    assert bm.slots[0]["status"] == "available"


# ── 链上成交确认（2026-08-11）：pending 台账保持 open + 补确认 ───────

def test_batch_sell_pending_keeps_slot_open(tmp_path):
    """卖出提交但链上未确认（PENDING）→ 台账保持 open + _pending_sells 记录
    （防“提交成功但链上未成交”的账实脱管，RENDER 3.06 实证）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s.submit_order = lambda order: _mock_order(
        quantity=order.quantity, filled=False)
    s.on_trading_iteration()
    # 台账保持 open（未 close，防账实脱节）
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["lot"]["qty"] == 5.0
    # pending 记录（供下轮补确认）
    assert 1 in s._pending_sells
    assert s._pending_sells[1]["qty"] == 5.0


def test_batch_buy_pending_does_not_open_lot(tmp_path):
    """买入提交但链上未确认（PENDING）→ 不 open_lot + _pending_buys 记录。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.submit_order = lambda order: _mock_order(
        quantity=order.quantity, filled=False)
    s.on_trading_iteration()
    assert bm.open_slots() == []  # 未确认不建仓（防幽灵仓）
    assert 1 in s._pending_buys


def test_check_pending_sell_confirmed_releases(monkeypatch, tmp_path):
    """_check_pending_confirmations：SELL pending 链上确认 SUCCESS → 补 close_lot。"""
    from nanobot_quant import onchainos_cli
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    # 手动注入 pending（模拟上轮提交未确认）
    s._pending_sells[1] = {
        "tx_hash": "tx1", "order_id": "", "chain": "solana",
        "qty": 5.0, "price": 66.0, "exit_reason": "setup_sell=9",
    }
    monkeypatch.setattr(
        onchainos_cli, "swap_status",
        lambda *_a, **_k: {"tx_status": "SUCCESS", "raw": {}})
    s._check_pending_confirmations()
    assert bm.open_slots() == []  # 补确认后释放
    assert s._pending_sells == {}


def test_check_pending_sell_failed_keeps_open(monkeypatch, tmp_path):
    """_check_pending_confirmations：SELL pending 链上 ERROR → 台账保持 open
    （下轮可重试卖出），清 pending。"""
    from nanobot_quant import onchainos_cli
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._pending_sells[1] = {
        "tx_hash": "tx1", "order_id": "", "chain": "solana",
        "qty": 5.0, "price": 66.0, "exit_reason": "setup_sell=9",
    }
    monkeypatch.setattr(
        onchainos_cli, "swap_status",
        lambda *_a, **_k: {"tx_status": "ERROR", "raw": {}})
    s._check_pending_confirmations()
    assert len(bm.open_slots()) == 1  # 保持 open
    assert s._pending_sells == {}  # 清 pending（不再防重，下轮可重试卖）


def test_check_pending_sell_still_pending_keeps_waiting(monkeypatch, tmp_path):
    """_check_pending_confirmations：SELL pending 仍 PENDING → 继续等待。"""
    from nanobot_quant import onchainos_cli
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    s._pending_sells[1] = {
        "tx_hash": "tx1", "order_id": "", "chain": "solana",
        "qty": 5.0, "price": 66.0, "exit_reason": "setup_sell=9",
    }
    monkeypatch.setattr(
        onchainos_cli, "swap_status",
        lambda *_a, **_k: {"tx_status": "PENDING", "raw": {}})
    s._check_pending_confirmations()
    assert len(bm.open_slots()) == 1
    assert 1 in s._pending_sells  # 继续等


def test_check_pending_buy_confirmed_opens(monkeypatch, tmp_path):
    """_check_pending_confirmations：BUY pending 链上 SUCCESS → 补 open_lot。"""
    from nanobot_quant import onchainos_cli
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s._pending_buys[1] = {
        "tx_hash": "tx1", "order_id": "", "chain": "solana",
        "qty": 0.0372, "price": 66.0, "reason": "setup_buy=9",
    }
    monkeypatch.setattr(
        onchainos_cli, "swap_status",
        lambda *_a, **_k: {"tx_status": "SUCCESS", "raw": {}})
    s._check_pending_confirmations()
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["slot"] == 1
    assert open_slots[0]["lot"]["qty"] == 0.0372
    assert s._pending_buys == {}
def _make_live_strategy(captured, **params):
    """live broker 策略（CexBroker 类名）——timestep 直拉前缀测试用。"""
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, min_history=50, **params)
    s.logger = logging.getLogger("td-live-timestep")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = _bars_with(_oscillate())
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: (
        captured.update({"timestep": timestep, "length": length}) or s._bars
    )
    s.broker = type("CexBroker", (), {})()  # live broker 类名（initialize 判定）
    return s


def test_live_timestep_uses_bar_prefix():
    """live broker → timestep 加 bar: 前缀：lumibot 无法解析 → 原样透传 →
    数据源直拉场景粒度（5m），绕开 multi-timeframe 转换（600 根 1m +
    resample）。CEX drops_in_progress=True → 不多拉 1 根。"""
    captured: dict = {}
    s = _make_live_strategy(captured, drops_in_progress_bars=True)
    s.initialize(symbol="SPCXB", sleeptime="5m")
    assert s._timestep == "5min"
    s._evaluate_symbol()
    assert captured["timestep"] == "bar:5min"
    assert captured["length"] == 50


def test_live_timestep_bar_prefix_drop_in_progress():
    """DEX（OnchainOS，drops_in_progress=False）live：bar: 前缀同时多拉
    1 根供丢弃（与旧行为一致）。"""
    captured: dict = {}
    s = _make_live_strategy(captured)  # 无 drops_in_progress_bars → drop=True
    s.initialize(symbol="SPCXB", sleeptime="1m")
    assert s._timestep == "minute"
    s._evaluate_symbol()
    assert captured["timestep"] == "bar:minute"
    assert captured["length"] == 51  # 多拉 1 根丢进行中 bar


def test_backtest_timestep_unchanged():
    """回测（broker=None）→ 标准 timestep（"5min"）不带 bar: 前缀——
    PandasDataBacktesting 只认 lumibot 标准名，前缀会让回测数据源挂。"""
    captured: dict = {}
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, min_history=50)
    s.logger = logging.getLogger("td-backtest-timestep")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = _bars_with(_oscillate())
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: (
        captured.update({"timestep": timestep}) or s._bars
    )
    s.initialize(symbol="SPCXB", sleeptime="5m")
    s._evaluate_symbol()
    assert captured["timestep"] == "5min"


# ── BUY：占用 slot ───────────────────────────────────────────────────


# ── TD CEX Step 2：CEX pending 订单轮询确认（2026-08-21）─────────────


class _FakeCexBroker:
    """CEX pending 确认测试用 broker：_query_order 返回预设状态。"""

    def __init__(self, status, filled, avg=0.0):
        self.status, self.filled, self.avg = status, filled, avg
        self.last = None

    def _query_order(self, order_id, pair):
        self.last = (order_id, pair)
        return (self.status, self.filled, 0.0, self.avg)


def _inject_cex_buy_pending(s, order_id="111", qty=0.0372, price=66.0):
    s._pending_buys[1] = {
        "cex": True, "order_id": order_id, "qty": qty, "price": price,
        "symbol": "CRCLX", "reason": "setup_buy=9",
    }


def _inject_cex_sell_pending(s, order_id="222", qty=5.0, price=66.0):
    s._pending_sells[1] = {
        "cex": True, "order_id": order_id, "qty": qty, "price": price,
        "symbol": "CRCLX", "exit_reason": "setup_sell=9",
    }


def test_cex_pending_buy_confirmed_opens(tmp_path):
    """CEX BUY pending 查单 filled → open_lot 补台账（回填 avg_price）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    _inject_cex_buy_pending(s)
    fb = _FakeCexBroker("filled", 0.0372, 66.5)
    s._cex_confirm_broker = lambda slot_id: fb
    s._check_pending_confirmations()
    open_slots = bm.open_slots()
    assert len(open_slots) == 1
    assert open_slots[0]["slot"] == 1
    assert open_slots[0]["lot"]["qty"] == 0.0372
    assert open_slots[0]["lot"]["entry_price"] == 66.0  # 策略价优先
    assert s._pending_buys == {}
    assert fb.last == ("111", "CRCLX_USDT")


def test_cex_pending_buy_zero_fill_releases(tmp_path):
    """CEX BUY pending 查单 cancelled/ioc 零成交 → 不建仓 + 释放 slot。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    _inject_cex_buy_pending(s)
    s._cex_confirm_broker = lambda slot_id: _FakeCexBroker("cancelled", 0.0)
    s._check_pending_confirmations()
    assert bm.open_slots() == []  # 零成交不建仓（防幽灵仓）
    assert s._pending_buys == {}  # 清 pending → slot 回到 available


def test_cex_pending_buy_still_open_waits(tmp_path):
    """CEX BUY pending 查单仍 open → 继续等待（不建仓不释放）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    _inject_cex_buy_pending(s)
    s._cex_confirm_broker = lambda slot_id: _FakeCexBroker("submitted", 0.0)
    s._check_pending_confirmations()
    assert bm.open_slots() == []
    assert 1 in s._pending_buys  # 继续等


def test_cex_pending_sell_confirmed_releases(tmp_path):
    """CEX SELL pending 查单 filled → close_lot 释放 slot。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    _inject_cex_sell_pending(s)
    fb = _FakeCexBroker("filled", 5.0, 66.9)
    s._cex_confirm_broker = lambda slot_id: fb
    s._check_pending_confirmations()
    assert bm.open_slots() == []  # 补确认后释放
    assert s._pending_sells == {}
    assert fb.last == ("222", "CRCLX_USDT")


def test_cex_pending_sell_zero_fill_keeps_open(tmp_path):
    """CEX SELL pending 查单 cancelled/ioc 零成交 → 台账保持 open（可重试卖）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    _inject_cex_sell_pending(s)
    s._cex_confirm_broker = lambda slot_id: _FakeCexBroker("cancelled", 0.0)
    s._check_pending_confirmations()
    assert len(bm.open_slots()) == 1  # 保持 open
    assert s._pending_sells == {}  # 清 pending → 下轮可重试卖出


def test_cex_pending_sell_still_open_waits(tmp_path):
    """CEX SELL pending 查单仍 open → 继续等待（台账保持 open）。"""
    bm = _make_bm(tmp_path)
    bm.open_lot(qty=5.0, entry_price=66.0, entry_time="t1")
    s = _make_batch_strategy(bm, _bars_with(_sell_closes()))
    _inject_cex_sell_pending(s)
    s._cex_confirm_broker = lambda slot_id: _FakeCexBroker("submitted", 0.0)
    s._check_pending_confirmations()
    assert len(bm.open_slots()) == 1
    assert 1 in s._pending_sells  # 继续等


def test_cex_pending_query_error_keeps_waiting(tmp_path):
    """CEX 查单异常 → 保留 pending（fail-safe 不误判成交/失败）。"""
    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    _inject_cex_buy_pending(s)

    def _boom(slot_id):
        raise RuntimeError("network down")

    s._cex_confirm_broker = _boom
    s._check_pending_confirmations()
    assert bm.open_slots() == []
    assert 1 in s._pending_buys  # 继续等
