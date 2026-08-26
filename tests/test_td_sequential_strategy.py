"""TdSequentialStrategy 参数化（P2 B2）测试 — quantity_mode / sleeptime。

覆盖：
- initialize 默认值（fixed / 10 / 1D）与参数覆盖
- sleeptime → lumibot timestep 映射
- BUY 信号下单量：fixed=固定 quantity；value=portfolio_value × max_position_pct
- 风控 gate 使用实际下单量的仓位价值（非默认 quantity）
"""

from __future__ import annotations

import logging
import sys

import pandas as pd

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _buy_signal_closes() -> list[float]:
    """50+ 根 bars：41 根交替震荡（不触发 setup）→ 5 根上升 → 12 根连续下跌。

    连续下跌段保证 setup_buy >= 9 且 score > 0（base 变体累加计数）。
    """
    closes = [100.0 + (i % 2) * 2 for i in range(41)]
    closes += [101.0, 102.0, 103.0, 104.0, 105.0]
    closes += [100.0 - i for i in range(12)]
    return closes


def _make_strategy(**params) -> TdSequentialStrategy:
    from lumibot.entities import Bars

    # 测试 bars 58 根 < 生产默认 120 窗口 → 显式收窄到 50（旧行为），
    # 避免 TD SKIP；min_history 参数化本身由专门测试覆盖。
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0

    closes = _buy_signal_closes()
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2025-01-01", periods=len(closes), freq="D"),
    )
    s._bars = Bars(df, "ONCHAIN", None)
    s.get_position = lambda symbol: None  # 无持仓 → BUY 分支
    s.get_historical_prices = lambda symbol, length, timestep: s._bars

    captured = {}

    def _create_order(asset, quantity, action):
        captured["order"] = (asset, quantity, action)
        return type("Order", (), {"identifier": "mock-id", "quantity": quantity})()

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.initialize()
    s._captured = captured
    return s


# ── initialize 参数化 ────────────────────────────────────────────────────

def test_initialize_defaults():
    s = _make_strategy()
    assert s.sleeptime == "1D"
    assert s._timestep == "day"
    assert s.quantity_mode == "fixed"
    assert s._portfolio.default_quantity == 10


def test_initialize_value_mode():
    s = _make_strategy(quantity_mode="value")
    assert s.quantity_mode == "value"
    # value 模式 → 无固定默认数量 → PortfolioEngine 回退 pv × pct 算法
    assert s._portfolio.default_quantity is None


def test_initialize_sleeptime_mapping():
    """场景 sleeptime → 精确 K 线粒度（S3a：5m/15m/30m 不再笼统成 minute）。"""
    for sleeptime, timestep in [
        ("1m", "minute"), ("5m", "5min"), ("15m", "15min"),
        ("30m", "30min"), ("1H", "hour"), ("4H", "4hour"),
        ("1D", "day"), ("1W", "week"),
    ]:
        s = _make_strategy(sleeptime=sleeptime)
        assert s._timestep == timestep, f"{sleeptime} → {s._timestep}"


def test_initialize_unknown_sleeptime_falls_back_to_day():
    """未知 sleeptime 回退 day（fail-safe，不抛错）。"""
    s = _make_strategy(sleeptime="2m")
    assert s._timestep == "day"


def test_initialize_kwargs_override_parameters():
    s = _make_strategy()
    # initialize 关键字参数优先于 parameters 字典
    s.quantity_mode = "value"  # initialize() 里 `or self.parameters.get` 语义
    s2 = _make_strategy(quantity_mode="value", sleeptime="1H")
    assert s2.sleeptime == "1H"
    assert s2._timestep == "hour"
    assert s2._portfolio.default_quantity is None


# ── BUY 下单量 ───────────────────────────────────────────────────────────

def test_buy_fixed_quantity():
    s = _make_strategy()
    s.on_trading_iteration()
    assert s._captured["order"][1] == 10  # fixed → 默认 quantity=10


def test_buy_value_sizing():
    s = _make_strategy(quantity_mode="value")
    price = s._captured  # placeholder
    s.on_trading_iteration()
    asset, qty, action = s._captured["order"]
    assert action == "buy"
    # pv=100_000 × max_position_pct=0.20 / 最新收盘价
    closes = _buy_signal_closes()
    last_price = closes[-1]
    expected = max(int(100_000 * 0.20 / last_price), 1)
    assert qty == expected, f"qty={qty} expected={expected} (price={last_price})"


def test_buy_value_sizing_floor_one_blocked_by_risk():
    """极端小净值：floor 1 的仓位价值超出 max_position_pct → risk fail-closed。

    pv=10 → qty=floor(10×0.20/price)=1，但 1 股价值 > pv×20% → 拒绝下单
    （不会以超限仓位成交）。
    """
    s = _make_strategy(quantity_mode="value")
    s.portfolio_value = 10.0
    s.on_trading_iteration()
    assert "order" not in s._captured  # risk 拦截，未产生订单


def test_risk_gate_uses_actual_sized_quantity():
    """value 模式下 risk gate 收到的是实际下单量的仓位价值，而非默认 quantity。"""
    s = _make_strategy(quantity_mode="value")
    calls = []

    class _RiskSpy:
        def __init__(self, inner):
            self._inner = inner

        def can_enter(self, **kw):
            calls.append(kw["position_value"])
            return self._inner.can_enter(**kw)

    s._risk = _RiskSpy(s._risk)
    s.on_trading_iteration()
    closes = _buy_signal_closes()
    last_price = closes[-1]
    expected_qty = max(int(100_000 * 0.20 / last_price), 1)
    assert calls, "risk.can_enter 未被调用"
    assert abs(calls[0] - expected_qty * last_price) < 1e-6
def test_initialize_min_history_default_120():
    """生产默认固定窗口 120 根（方案 B，2026-08-10）。"""
    from nanobot_quant.strategies.td_sequential_strategy import (
        TdSequentialStrategy,
    )

    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters)
    s.logger = logging.getLogger("td-test")
    s.initialize()
    assert s._min_history == 120


def test_initialize_min_history_from_parameters():
    s = _make_strategy(min_history=60)
    assert s._min_history == 60


def test_on_trading_iteration_uses_fixed_window(monkeypatch):
    """固定窗口：get_historical_prices 的 length = min_history（不累积增长）。"""
    s = _make_strategy(min_history=50)
    calls: list[int] = []

    def _record(symbol, length, timestep):
        calls.append(length)
        return s._bars

    s.get_historical_prices = _record
    s.on_trading_iteration()
    s.on_trading_iteration()
    # 两轮 length 恒定 = 50（旧行为是 50、51 递增）
    assert calls == [50, 50]


def test_live_gate_source_no_double_drop(monkeypatch):
    """A 修复第二部分回归：数据源已过滤进行中 bar（gate_cex drops=True，td_live
    注入 parameters）时，live 不多拉 1 根、不再丢——双重丢弃会得到
    119 < min_history 永久 SKIP。"""
    s = _make_strategy(min_history=50)
    s._is_live_broker = True
    s.parameters["drops_in_progress_bars"] = True  # td_live 从 broker.data_source 注入
    calls: list[int] = []

    def _record(symbol, length, timestep):
        calls.append(length)
        return s._bars

    s.get_historical_prices = _record
    monkeypatch.setattr(s, "_calc", lambda df: {"setup_buy": 0, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0, "score": 0, "price": 0})  # 短路信号计算
    s.on_trading_iteration()
    assert calls == [50]  # 不 +1


def test_live_onchainos_source_drops_one(monkeypatch):
    """OnchainOS（DEX）数据源无 drops 契约（含进行中 bar）：live 保持原行为
    多拉 1 根供丢弃（方案 C，2026-08-11）。"""
    s = _make_strategy(min_history=50)
    s._is_live_broker = True
    s.parameters["drops_in_progress_bars"] = False  # 默认（未注入）
    calls: list[int] = []

    def _record(symbol, length, timestep):
        calls.append(length)
        return s._bars

    s.get_historical_prices = _record
    monkeypatch.setattr(s, "_calc", lambda df: {"setup_buy": 0, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0, "score": 0, "price": 0})
    s.on_trading_iteration()
    assert calls == [51]  # +1




# ── 2026-08-11 事件展示修复：symbol 显式传 + tx_hash detail 提取 ──

def _record_strategy(monkeypatch) -> tuple[TdSequentialStrategy, dict]:
    """构造轻量策略 + mock td_live_state，捕获 update_symbol/append_event。"""
    import nanobot_quant.td_live_state as tls
    captured: dict = {}
    monkeypatch.setattr(tls, "update_symbol",
                        lambda sym, data=None, **kw: captured.setdefault("upd", sym))
    monkeypatch.setattr(tls, "append_event",
                        lambda ev: captured.setdefault("ev", ev))
    s = TdSequentialStrategy()
    s.symbol = "RENDER"
    s.parameters = {"live_mode": True}
    s._last_signal = {}
    return s, captured


def test_record_symbol_override(monkeypatch):
    """确认路径显式传 symbol → 事件/状态用该 symbol（非 self.symbol）。

    回归 15:49:01：CRCLX 确认被记成 RENDER（确认跑在主循环、
    self.symbol 是当前迭代标的）。
    """
    s, captured = _record_strategy(monkeypatch)
    s._record("LONG", "note", symbol="CRCLX")
    assert captured["upd"] == "CRCLX"
    assert captured["ev"]["symbol"] == "CRCLX"


def test_record_defaults_to_self_symbol(monkeypatch):
    """未显式传 symbol 时回退 self.symbol（标的循环内记录不受影响）。"""
    s, captured = _record_strategy(monkeypatch)
    s._record("LONG", "note")
    assert captured["upd"] == "RENDER"
    assert captured["ev"]["symbol"] == "RENDER"


def test_confirmed_tx_hash_from_detail():
    """detail 响应 data[0].txHash 非占位 → 直接返回（SELL 场景零额外调用）。"""
    s = TdSequentialStrategy()
    s.symbol = "CRCLX"
    real = "5xNq3aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
    st = {"tx_status": "SUCCESS", "raw": {"data": [{"txHash": real}]}}
    assert s._confirmed_tx_hash({"tx_hash": ""}, st) == real


def test_confirmed_tx_hash_placeholder_returns_empty():
    """占位 UUID（detail 查不到）→ 返回空（事件显示 —，不阻塞确认）。"""
    s = TdSequentialStrategy()
    s.symbol = "CRCLX"
    placeholder = "58a1b2c3d4e5f60718293a4b5c6d7e8f"
    st = {"tx_status": "UNKNOWN", "raw": {"data": []}}
    assert s._confirmed_tx_hash({"tx_hash": placeholder}, st) == ""


def test_confirmed_tx_hash_keeps_real_pending_hash():
    """pending 已是真实 hash 且 detail 无响应 → 保留原值。"""
    s = TdSequentialStrategy()
    s.symbol = "CRCLX"
    real = "5xNq3aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
    assert s._confirmed_tx_hash({"tx_hash": real}, None) == real


def test_is_placeholder_tx_hash():
    """32-hex UUID（Gas Station 占位）识别。"""
    from nanobot_quant.onchainos_cli import is_placeholder_tx_hash
    assert is_placeholder_tx_hash("58a1b2c3d4e5f60718293a4b5c6d7e8f") is True
    assert is_placeholder_tx_hash(
        "5xNq3aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef") is False
    assert is_placeholder_tx_hash("") is False

def test_logger_proxy_penetration():
    """日志可见性：LazyStrategyLogger → StrategyLoggerAdapter → Logger 三层穿透。
    模拟 lumibot v4.5.78 的 logger 链（无 .handlers 的 proxy，仅 .logger 属性委托），
    策略 __init__ 的穿透循环必须拿到底层 logging.Logger（回归：直接访问
    .handlers 曾抛 AttributeError 'StrategyLoggerAdapter' object has no attribute 'handlers'）。"""

    class _FakeProxy:
        def __init__(self, inner):
            self.logger = inner

    base = logging.getLogger("td_test_logger_penetration")
    proxy = _FakeProxy(_FakeProxy(base))  # 两层代理（LazyStrategyLogger → Adapter）
    _lg = proxy
    for _ in range(3):
        if isinstance(_lg, logging.Logger):
            break
        _lg = getattr(_lg, "logger", _lg)
    assert _lg is base

    # 兜底：穿透失败时退化为按类名取 logger（不崩溃）
    _lg2 = object()
    if not isinstance(_lg2, logging.Logger):
        _lg2 = logging.getLogger("TdSequentialStrategy")
    assert isinstance(_lg2, logging.Logger)


def test_strategy_logger_binds_stderr():
    """策略 initialize 后底层 logger 挂 stderr handler（TD 循环日志 gatekeeper 可见）。
    回归：initialize 直接访问 self.logger.handlers 曾抛 AttributeError——
    lumibot 的 logger 是 LazyStrategyLogger proxy（无 .handlers），须穿透
    .logger 链拿底层 Logger 再配置。"""
    s = _make_strategy()
    s.initialize()  # 日志配置在 lumibot lifecycle initialize 里执行
    _lg = logging.getLogger("td-test")  # _make_strategy 手动设置的策略 logger
    assert isinstance(_lg, logging.Logger)
    assert _lg.propagate is False
    assert any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
        for h in _lg.handlers
    )
    # 幂等：重复 initialize 不重复加 handler
    n = len(_lg.handlers)
    s.initialize()
    assert len(_lg.handlers) == n


# ── 信号周期门控（2026-08-19 分批次建仓语义）────────────────────────
# 一个信号周期（setup 计数未重置）内同一标的只建一次仓：
# - setup=9 建仓后 10/11/12 累加（单调不减）→ 同周期，跳过（TD BATCH WAIT）
# - 计数变小（12→8 / 9→1）→ 新周期，允许再建
# - slot 平仓释放后同周期也不建（信号级）

def _neutral_closes(n: int = 60) -> list[float]:
    """交替震荡：setup 计数归小/不触发。"""
    return [100.0 + (i % 2) * 2 for i in range(n)]


def _swap_bars(s, closes: list[float]) -> None:
    from lumibot.entities import Bars

    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2025-01-01", periods=len(closes), freq="D"),
    )
    s._bars = Bars(df, "ONCHAIN", None)


def test_cycle_gate_same_period_skips_second_buy():
    """setup>=9 建仓后同周期（未重置）不再建仓。"""
    s = _make_strategy()
    s._evaluate_symbol()
    assert "order" in s._captured, "首次信号应建仓"
    assert s._cycle_state["AAPL"]["bought"] is True
    assert s._cycle_state["AAPL"]["prev_setup"] >= 9
    s._captured.clear()
    s._evaluate_symbol()  # 同一 bars → setup_buy 仍触发且未重置
    assert "order" not in s._captured, "同周期不得再建仓"
    assert s._cycle_state["AAPL"]["reset"] is False


def test_cycle_gate_new_period_after_reset():
    """setup 计数变小（重置）后重新触发 → 允许再建。"""
    s = _make_strategy()
    s._evaluate_symbol()
    assert "order" in s._captured
    first_setup = s._cycle_state["AAPL"]["prev_setup"]
    assert first_setup >= 9
    # 中性 bars：setup 计数归小 → 触发 reset
    _swap_bars(s, _neutral_closes())
    s._captured.clear()
    s._evaluate_symbol()
    assert "order" not in s._captured
    assert s._cycle_state["AAPL"]["reset"] is True, "计数变小应标记新周期"
    # 重新触发（setup 再数到 >= entry_setup）→ 新周期允许建仓
    _swap_bars(s, _buy_signal_closes())
    s._captured.clear()
    s._evaluate_symbol()
    assert "order" in s._captured, "重置后新周期应允许再建仓"
    assert s._cycle_state["AAPL"]["reset"] is False


def test_cycle_gate_open_position_on_start_blocks():
    """重启边界：初始有 open 仓位 + 其他 available slot → 视为本周期
    已建仓（保守不追同周期信号），BUY 被周期守卫拦截。"""
    from nanobot_quant.batches import BatchManager

    s = _make_strategy()
    bm = BatchManager("AAPL", ["acc-1", "acc-2"], path="/tmp/test-cycle-open.json")
    bm.open_lot(qty=1.0, entry_price=100.0, slot=1)  # slot1 open
    s.batch_manager = bm
    s._batch_managers = {"AAPL": bm}
    s._captured.clear()
    s._evaluate_symbol()  # signal 触发 + slot2 available → 周期守卫拦截
    assert "order" not in s._captured, "有 open 仓位视为本周期已建仓，跳过 BUY"
    assert s._cycle_state["AAPL"]["bought"] is True


def test_write_positions_state(monkeypatch):
    """持仓摘要写入 LIVE_STATE（2026-08-22 实时监控持仓小节）。

    - open 批次按标的分组写入（场景→标的→行）
    - 价格口径 = ticker（_cex_price_of），浮盈按 ticker 价算
    - 无 open 批次的标的不取价、不写入
    """
    from types import SimpleNamespace

    from nanobot_quant import td_live_state
    from nanobot_quant.batches import BatchManager

    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy()
    s._current_scene = "high"
    bm = BatchManager("CRCLX", ["acc-1", "acc-2"], path="/tmp/test-pos.json")
    bm.open_lot(qty=0.045, entry_price=87.99, slot=2)
    s.batch_managers = {"CRCLX": bm, "SOL": SimpleNamespace(slots=[])}
    s._batch_managers = None
    s._cex_price_of = lambda sym: 88.65
    s._write_positions_state()

    rows = td_live_state.LIVE_STATE["positions"]["high"]["CRCLX"]
    assert len(rows) == 1
    assert rows[0]["slot"] == 2
    assert rows[0]["qty"] == 0.045
    assert rows[0]["price"] == 88.65
    assert abs(rows[0]["pnl_pct"] - (88.65 - 87.99) / 87.99) < 1e-9
    # 无 open 批次的标的（SOL）不写入
    assert "SOL" not in td_live_state.LIVE_STATE["positions"]["high"]


def test_write_positions_state_price_failure_fallback(monkeypatch):
    """ticker 取价失败 → price/pnl 置 None（显示 —），不阻塞。"""
    from nanobot_quant import td_live_state
    from nanobot_quant.batches import BatchManager

    # 隔离 LIVE_STATE，避免污染其他测试（2026-08-22 组合跑暴露）
    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy()
    s._current_scene = "mid"
    bm = BatchManager("SOL", ["acc-1"], path="/tmp/test-pos2.json")
    bm.open_lot(qty=0.0457, entry_price=87.42, slot=1)
    s.batch_managers = {"SOL": bm}
    s._batch_managers = None

    def boom_price(sym):
        raise RuntimeError("ticker down")

    s._cex_price_of = boom_price
    s._write_positions_state()

    rows = td_live_state.LIVE_STATE["positions"]["mid"]["SOL"]
    assert rows[0]["price"] is None
    assert rows[0]["pnl_pct"] is None
    # AAPL 触发建仓 → bought=True
    s._evaluate_symbol()
    assert s._cycle_state["AAPL"]["bought"] is True
    # MSFT 从未评估过 → 首次评估视为新标的（bought=False）
    s.symbol = "MSFT"
    s._evaluate_symbol()
    assert s._cycle_state["MSFT"]["bought"] is True
    assert s._cycle_state["MSFT"]["prev_setup"] >= 9
    # AAPL 状态未被 MSFT 评估污染
    assert s._cycle_state["AAPL"]["bought"] is True
def test_cycle_gate_per_symbol_independent():
    """每币独立：不同 symbol 各自周期状态，互不影响。"""
    s = _make_strategy()
    s.symbols = ["AAPL", "MSFT"]
    # AAPL 触发建仓 → bought=True
    s._evaluate_symbol()
    assert s._cycle_state["AAPL"]["bought"] is True
    # MSFT 从未评估过 → 首次评估视为新标的（bought=False）
    s.symbol = "MSFT"
    s._evaluate_symbol()
    assert s._cycle_state["MSFT"]["bought"] is True
    assert s._cycle_state["MSFT"]["prev_setup"] >= 9
    # AAPL 状态未被 MSFT 评估污染
    assert s._cycle_state["AAPL"]["bought"] is True

def test_write_account_funds_cex(monkeypatch):
    """CEX 通道写入子账号资金（2026-08-22 资金小表数据源）。

    - channel_family=cex 时按场景 sub_accounts 取 slot→子账号
    - USDT 可用 = available；总资产 = Σ(available+locked)×ticker 价
    - DEX 通道不写（待补）
    """
    from nanobot_quant import td_live_state

    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy(channel_family="cex")
    s._current_scene = "high"
    monkeypatch.setattr(
        "nanobot_quant.exec_params.load_exec_params",
        lambda: {"scenes": {"high": {"sub_accounts": ["gate_bot1", "gate_bot2"]}}},
    )
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_gate_credentials",
        lambda: {
            "sub_accounts": {
                "gate_bot1": {"uid": "59175220"},
                "gate_bot2": {"uid": "59175258"},
            },
        },
    )
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.fetch_all_balances",
        lambda creds: {
            "main": {},
            "sub_accounts": [
                {"uid": "59175220", "balances": {
                    "USDT": {"available": 3.98, "locked": 0.0},
                    "CRCLX": {"available": 0.5, "locked": 0.0},
                }},
                {"uid": "59175258", "balances": {
                    "USDT": {"available": 0.1, "locked": 0.0},
                    "RENDER": {"available": 2.0, "locked": 1.0},
                }},
            ],
        },
    )
    s._cex_price_of = lambda cur: {"CRCLX": 88.0, "RENDER": 1.45}.get(cur, 0.0)
    s._write_account_funds()

    funds = td_live_state.LIVE_STATE["funds"]["high"]
    assert len(funds) == 2
    f1 = funds[0]
    assert f1["slot"] == 1 and f1["account"] == "gate_bot1"
    assert f1["usdt_available"] == 3.98
    assert abs(f1["total_asset"] - (3.98 + 0.5 * 88.0)) < 1e-6
    f2 = funds[1]
    assert f2["usdt_available"] == 0.1
    # RENDER 2.0 available + 1.0 locked = 3.0 × 1.45
    assert abs(f2["total_asset"] - (0.1 + 3.0 * 1.45)) < 1e-6


def test_write_account_funds_dex_skipped(monkeypatch):
    """DEX 通道不写资金（待补，docs/quant-system.md 记录）。"""
    from nanobot_quant import td_live_state

    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy(channel_family="dex")
    s._current_scene = "high"
    monkeypatch.setattr(
        "nanobot_quant.exec_params.load_exec_params",
        lambda: {"scenes": {"high": {"sub_accounts": ["gate_bot1"]}}},
    )
    s._write_account_funds()
    assert "funds" not in td_live_state.LIVE_STATE or "high" not in (
        td_live_state.LIVE_STATE.get("funds") or {}
    )


def test_write_account_funds_uid_missing(monkeypatch):
    """子账号缺 UID 映射 → 资金行 usdt/total 为 0（fail-safe，不崩）。"""
    from nanobot_quant import td_live_state

    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy(channel_family="cex")
    s._current_scene = "high"
    monkeypatch.setattr(
        "nanobot_quant.exec_params.load_exec_params",
        lambda: {"scenes": {"high": {"sub_accounts": ["gate_bot1", "gate_bot2"]}}},
    )
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.load_gate_credentials",
        lambda: {"sub_accounts": {"gate_bot1": {"uid": "59175220"}}},
    )  # gate_bot2 无 uid 映射
    monkeypatch.setattr(
        "nanobot_quant.gate_credentials.fetch_all_balances",
        lambda creds: {"main": {}, "sub_accounts": [
            {"uid": "59175220", "balances": {"USDT": {"available": 3.98, "locked": 0.0}}},
        ]},
    )
    s._cex_price_of = lambda cur: 0.0
    s._write_account_funds()
    funds = td_live_state.LIVE_STATE["funds"]["high"]
    assert funds[0]["usdt_available"] == 3.98
    assert funds[1]["usdt_available"] == 0.0  # uid 缺失 → 0（不崩）

def test_write_positions_state_display_min_filter(monkeypatch):
    """持仓显示阈值（2026-08-26 用户拍板：显示阈值独立于交易门槛）：
    value < position_display_min_usd → 不写入 LIVE_STATE（dust 不刷屏）；
    ≥ 阈值正常显示；台账本身不受影响。"""
    from nanobot_quant import td_live_state
    from nanobot_quant.batches import BatchManager

    monkeypatch.setattr(td_live_state, "LIVE_STATE", {
        "running": False, "next_iteration": None, "updated_at": "",
        "strategy_variant": "", "symbols": {}, "positions": {},
    })
    s = _make_strategy()
    s._current_scene = "high"
    s.parameters = dict(s.parameters, position_display_min_usd=1.0)
    bm = BatchManager("CRCLX", ["acc-1", "acc-2"], path="/tmp/test-pos3.json")
    bm.open_lot(qty=0.005, entry_price=87.99, slot=1)   # $0.44 < $1 → 不显示
    bm.open_lot(qty=0.045, entry_price=87.99, slot=2)   # $3.99 ≥ $1 → 显示
    s.batch_managers = {"CRCLX": bm}
    s._batch_managers = None
    s._cex_price_of = lambda sym: 88.65
    s._write_positions_state()

    rows = td_live_state.LIVE_STATE["positions"]["high"]["CRCLX"]
    assert [r["slot"] for r in rows] == [2]
    # 台账不受影响——两个批次都在（显示过滤不影响交易）
    assert len(bm.open_slots()) == 2


def test_load_batch_snapshot_display_min_filter(monkeypatch):
    """离线快照显示阈值（TD 未运行时页面回退读台账）：成本价值
    （entry_price × qty）< position_display_min_usd → 不返回（dust 不显示）。"""
    from nanobot_quant import td_table_handlers
    from nanobot_quant.batches import BatchManager

    bm = BatchManager("CRCLX", ["acc-1", "acc-2"], path="/tmp/snap-test.json")
    bm.open_lot(qty=0.005, entry_price=87.99, slot=1)   # $0.44 < $1 → 过滤
    bm.open_lot(qty=0.045, entry_price=87.99, slot=2)   # $3.96 ≥ $1 → 显示
    monkeypatch.setattr(BatchManager, "load", staticmethod(lambda **kw: bm))
    monkeypatch.setattr(
        "nanobot_quant.td_table_handlers.load_exec_params",
        lambda: {"execution_channel": "gate",
                 "position_display_min_usd": 1.0,
                 "scenes": {"high": {"symbols": ["CRCLX"]}}},
    )
    rows = td_table_handlers._load_batch_snapshot("high")
    assert [r["slot"] for r in rows] == [2]


# ── 共振错峰（2026-08-26 用户拍板）─────────────────────────────
# 每轮每场景全局只建 1 笔：本轮已有标的建仓（_round_buy_used）→ 其余标的
# BUY 被拦并打 _denied_cycle 标记；denied 标的本 setup 周期内不再尝试
# （等重置），setup 计数变小（reset）时清除标记、新周期恢复建仓资格。

def _resonance_strategy(tmp_name: str):
    from nanobot_quant.batches import BatchManager

    s = _make_strategy()
    bm = BatchManager("AAPL", ["acc-1", "acc-2"], path=f"/tmp/{tmp_name}.json")
    s.batch_manager = bm
    s._batch_managers = {"AAPL": bm}
    s.broker = None  # _activate_scene 会读 self.broker
    s._captured.clear()
    return s


def test_resonance_round_quota_blocks_and_denies():
    """本轮额度已用（其他标的已建）→ 本标的 BUY 被拦 + denied 标记。"""
    s = _resonance_strategy("test-resonance-1")
    s._round_buy_used = True  # 模拟：池内其他标的已建（占用本轮额度）
    s._evaluate_symbol()
    assert "order" not in s._captured, "额度已用 → 不得建仓"
    assert s._denied_cycle.get("AAPL") is True, "被拦标的应打本周期错过标记"


def test_resonance_denied_waits_for_reset_then_allowed(monkeypatch):
    """denied 标的下轮 setup 仍≥9 也不建（等重置）；setup 重置清除标记
    → 新周期重新数到 9 允许建仓（PENDING 入账）。"""
    from types import SimpleNamespace

    from nanobot_quant.batches import BatchManager

    s = _make_strategy()
    bm = BatchManager("AAPL", ["acc-1", "acc-2"], path="/tmp/test-resonance-2.json")
    s.batch_manager = bm
    s._batch_managers = {"AAPL": bm}
    s._pending_buys = {}
    s._is_cex = lambda: True
    # mock 下单：PENDING 订单（is_filled=False）→ executed 置位但不落台账
    mo = SimpleNamespace(is_filled=lambda: False, custom_params=None, identifier="oid-1")
    monkeypatch.setattr(s, "_buy_on_slot", lambda slot, price, reason: (mo, 1.0))
    # 首次：额度已用 → 被拦 + denied（未下单）
    s._round_buy_used = True
    s._evaluate_symbol()
    assert s._denied_cycle.get("AAPL") is True
    assert not s._pending_buys, "额度已用不得下单"
    # 下轮：额度重置（新心跳），但 denied 未清除（setup 仍≥9）→ 仍不建
    s._round_buy_used = False
    s._evaluate_symbol()
    assert not s._pending_buys, "denied 标的本周期不得建仓"
    # 中性 bars：setup 计数变小 → reset → denied 清除
    _swap_bars(s, _neutral_closes())
    s._evaluate_symbol()
    assert s._denied_cycle.get("AAPL") is not True, "setup 重置应清除 denied"
    # 新周期重新数到 9 → 允许建仓（PENDING 入账）
    _swap_bars(s, _buy_signal_closes())
    s._evaluate_symbol()
    assert s._pending_buys, "重置后新周期应允许建仓"


def test_resonance_round_quota_reset_on_scene_activate():
    """场景激活（每轮执行开始）重置本轮额度 → 下一轮其他标的可建。"""
    s = _resonance_strategy("test-resonance-3")
    s._round_buy_used = True
    s._activate_scene("high", {
        "enabled": True, "sleeptime": "1m",
        "params": {"symbols": ["AAPL"], "quantity_mode": "fixed",
                   "td_quantity": 1},
    })
    assert s._round_buy_used is False, "场景激活应重置本轮额度"


def test_resonance_no_quota_without_scene():
    """非场景运行（execute_signal 直调/纸交易，未激活场景）→ 额度机制不
    生效（_round_buy_used 缺省 False），保持旧行为。"""
    s = _make_strategy()
    assert getattr(s, "_round_buy_used", False) is False
    s._evaluate_symbol()
    assert "order" in s._captured, "未激活场景不限制建仓"
