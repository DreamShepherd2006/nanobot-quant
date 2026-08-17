"""TdSequentialStrategy 参数化（P2 B2）测试 — quantity_mode / sleeptime。

覆盖：
- initialize 默认值（fixed / 10 / 1D）与参数覆盖
- sleeptime → lumibot timestep 映射
- BUY 信号下单量：fixed=固定 quantity；value=portfolio_value × max_position_pct
- 风控 gate 使用实际下单量的仓位价值（非默认 quantity）
"""

from __future__ import annotations

import logging

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
    for sleeptime, timestep in [
        ("1m", "minute"), ("5m", "minute"), ("15m", "minute"),
        ("1H", "hour"), ("1D", "day"), ("1W", "week"),
    ]:
        s = _make_strategy(sleeptime=sleeptime)
        assert s._timestep == timestep, f"{sleeptime} → {s._timestep}"


def test_initialize_unknown_sleeptime_falls_back_to_day():
    s = _make_strategy(sleeptime="4H")
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
