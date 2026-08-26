"""K 线并发预取（kline_concurrency）测试 — 2026-08-24 并发优化。

覆盖：
- kline_concurrency=1 → _prefetch_all_bars 返回 {}（串行路径，零开销）
- 并发模式返回全部标的预取结果（fetch_len/drop_in_progress 契约）
- 单标的失败包装 (None, ..., exc)，_evaluate_symbol 打印 DATA ERROR 不崩
- 预取 bars 直接进评估（跳过拉取，calls 为空）
- 并发真实发生（mock 拉取带 sleep + 活跃计数）
"""

from __future__ import annotations

import logging
import threading
import time

import pandas as pd

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _buy_signal_closes() -> list[float]:
    """58 根 bars：41 根交替震荡（不触发 setup）→ 5 根上升 → 12 根连续下跌。"""
    closes = [100.0 + (i % 2) * 2 for i in range(41)]
    closes += [101.0, 102.0, 103.0, 104.0, 105.0]
    closes += [100.0 - i for i in range(12)]
    return closes


def _make_strategy(**params) -> TdSequentialStrategy:
    from lumibot.entities import Bars

    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-kline-concurrency")
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

    captured = {}

    def _create_order(asset, quantity, action):
        captured["order"] = (asset, quantity, action)
        # 镜像 lumibot v4.5.78 Order：custom_params 默认 None
        return type("Order", (), {"identifier": "mock-id", "quantity": quantity, "custom_params": None})()

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.initialize()
    s._captured = captured
    return s


def _concurrent_fetch_mock(s, delay: float = 0.15, fail_symbol: str | None = None):
    """mock get_historical_prices：记录调用 + sleep 模拟慢拉取 + 统计最大并发。"""
    calls: list = []
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def fetch(symbol, length, timestep="", **kwargs):
        with lock:
            calls.append((symbol, length, timestep))
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        try:
            if fail_symbol is not None and symbol == fail_symbol:
                raise RuntimeError(f"boom {symbol}")
            time.sleep(delay)
            return s._bars
        finally:
            with lock:
                state["active"] -= 1

    return fetch, calls, lambda: state["max_active"]


# ── 串行路径 ─────────────────────────────────────────────────────────────

def test_prefetch_serial_when_workers_1():
    s = _make_strategy(kline_concurrency=1, symbols=["A", "B", "C"])
    fetch, calls, _ = _concurrent_fetch_mock(s)
    s.get_historical_prices = fetch
    assert s._prefetch_all_bars() == {}
    assert calls == []  # 串行路径不预取（由 _evaluate_symbol 内部拉取）


def test_prefetch_skipped_for_single_symbol():
    s = _make_strategy(kline_concurrency=8, symbols=["A"])
    fetch, calls, _ = _concurrent_fetch_mock(s)
    s.get_historical_prices = fetch
    assert s._prefetch_all_bars() == {}
    assert calls == []


# ── 并发路径 ─────────────────────────────────────────────────────────────

def test_prefetch_concurrent_returns_all():
    s = _make_strategy(kline_concurrency=4, symbols=["A", "B", "C"])
    fetch, calls, max_active = _concurrent_fetch_mock(s)
    s.get_historical_prices = fetch
    out = s._prefetch_all_bars()
    assert set(out) == {"A", "B", "C"}
    for sym in ("A", "B", "C"):
        bars, fetch_len, drop_in_progress, exc = out[sym]
        assert bars is s._bars
        assert fetch_len == 50          # = min_history（drop_in_progress=False）
        assert drop_in_progress is False
        assert exc is None
    assert set(c[0] for c in calls) == {"A", "B", "C"}
    assert max_active() >= 2            # 真实并发（串行时恒为 1）


def test_prefetch_workers_capped_by_symbols():
    s = _make_strategy(kline_concurrency=20, symbols=["A", "B"])
    fetch, _, max_active = _concurrent_fetch_mock(s)
    s.get_historical_prices = fetch
    s._prefetch_all_bars()
    assert max_active() <= 2            # max_workers 不超标的数


def test_prefetch_failure_collected():
    s = _make_strategy(kline_concurrency=4, symbols=["A", "B", "C"])
    fetch, _, _ = _concurrent_fetch_mock(s, fail_symbol="B")
    s.get_historical_prices = fetch
    out = s._prefetch_all_bars()
    assert out["B"][0] is None
    assert isinstance(out["B"][3], RuntimeError)
    assert out["A"][0] is s._bars       # 单标的失败不影响其他


# ── _evaluate_symbol 预取路径 ────────────────────────────────────────────

def test_evaluate_symbol_prefetched_skips_fetch():
    s = _make_strategy(kline_concurrency=4, symbols=["A"])
    fetch, calls, _ = _concurrent_fetch_mock(s)
    s.get_historical_prices = fetch
    s.symbol = "A"
    s._evaluate_symbol((s._bars, 50, False, None))  # 预取路径
    assert calls == []                  # 不再调用 get_historical_prices
    assert "order" in s._captured       # 12 连跌 → setup>=9 → BUY 分支走通


def test_evaluate_symbol_prefetched_error_does_not_crash():
    s = _make_strategy(kline_concurrency=4, symbols=["A"])
    s.symbol = "A"
    exc = RuntimeError("boom A")
    s._evaluate_symbol((None, 50, False, exc))  # 打印 DATA ERROR 后 return，不崩
    assert "order" not in s._captured
