"""SKIP 统计标签拆分（2026-11-04）。

拆前 `batch_wait` 一个键同时统计两类拦截：
  ① 同标的 setup/countdown **周期门控**（同一信号周期已建仓，等重置）
  ② 跨标的 **共振组波锁**（同一共振组已满 ``wave_max_buys`` 笔）
⇒ 波锁关掉（``wave_max_buys=0``）后 ``batch_wait`` 仍在涨，读不出是周期门控
还是波锁没关干净，波锁的边际影响无法评估（2026-11-02 回测读数即此问题）。

拆后：``cycle_wait`` = ①，``wave_wait`` = ②；``batch_wait`` 仅作历史回测记录兼容。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ── 夹具 ────────────────────────────────────────────────────────────────


def _bars(closes: list[float]):
    from lumibot.entities import Bars

    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.002 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="min"),
    )
    return Bars(df, "GATE", None)


def _bars_buy9():
    """41 根上涨 + 12 根下跌 → 末根买9。"""
    closes = [100.0 + i * 10 for i in range(41)]
    closes += [500.0 - i for i in range(1, 13)]
    return _bars(closes)


class _FakeBM:
    def __init__(self, slots=None, costs=None):
        self._slots = list(slots or [])
        self._costs = list(costs or [])

    def open_slots(self):
        return [
            {"slot": i + 1, "status": "open", "lot": {"qty": 1.0, "entry_price": c}}
            for i, c in enumerate(self._costs)
        ]

    def any_open(self):
        return bool(self._costs)

    def check_exit(self, *a, **k):
        return []

    def pick_exit_slot(self, *a, **k):
        return None

    def scan_buy_slots(self, start=1):
        return self._slots


def _mock_order(quantity=1.0):
    return type("Order", (), {
        "identifier": "mock-id",
        "quantity": quantity,
        "error": None,
        "status": "unsubmitted",
        "custom_params": None,
        "is_filled": lambda self=None: False,
        "set_error": lambda self, e: setattr(self, "error", e),
    })()


def _make_strategy(bars, bm=None, **params) -> TdSequentialStrategy:
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-skip-split-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = bars
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars
    captured: dict = {}
    s.create_order = lambda asset, quantity, action: (
        captured.update(order=(asset, quantity, action)) or _mock_order(quantity)
    )
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = bm
    s.initialize()
    # initialize() 会用默认标的（AAPL）覆盖 symbol，必须在它之后设
    s.symbol = "SOL"
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9
    s._home_account = "acc-home"
    s._buy_on_slot = lambda *a, **k: (_mock_order(), 1.0)
    s._captured = captured
    return s


def _activate(s, scene_params: dict) -> None:
    s.broker = object()
    s._activate_scene("high", {"params": scene_params})


# ── ① 周期门控 → cycle_wait ─────────────────────────────────────────────


def test_cycle_gate_counts_cycle_wait():
    """同周期已建仓（bought 且未 reset）→ cycle_wait，不再是 batch_wait。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(slots=[{"slot": 1}]))
    _activate(s, {})
    s._cycle_state = {
        # prev_setup == 当前 setup（单调不减）⇒ reset=False，才算同周期
        ("SOL", 1): {"bought": True, "prev_setup": 9, "reset": False, "cd_triggered": False}
    }
    s._evaluate_symbol()
    assert s._skip_counts.get("cycle_wait", 0) == 1
    assert s._skip_counts.get("wave_wait", 0) == 0
    assert s._skip_counts.get("batch_wait", 0) == 0


# ── ② 波锁 → wave_wait ──────────────────────────────────────────────────


def _wave_full_state(s, *, count: int = 1):
    """造一个与当前 bar 相交、已建 count 笔的共振组。

    时间必须用真实 bar 的最后一根时间（``_evaluate_symbol`` 会用
    ``df.index[-1]`` 覆盖 ``_cur_bar_ts``），否则区间不相交、组不命中。
    """
    ts = s._bars.df.index[-1]
    s._cur_bar_ts = ts
    s._cycle_span = {("high", "SOL"): {"start": ts, "end": ts}}
    s._wave_groups = {
        "high": [{"start": ts, "end": ts, "count": count, "symbols": {"OTHER"}}]
    }
    s._denied_cycle = {}
    s._cycle_state = {
        ("SOL", 1): {"bought": False, "prev_setup": 0, "reset": False, "cd_triggered": False}
    }


def test_wave_group_full_counts_wave_wait():
    """共振组已满 wave_max_buys=1 → wave_wait，不是 cycle_wait。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(slots=[{"slot": 1}]))
    _activate(s, {"wave_max_buys": 1})
    _wave_full_state(s, count=1)
    s._evaluate_symbol()
    assert s._skip_counts.get("wave_wait", 0) == 1
    assert s._skip_counts.get("cycle_wait", 0) == 0
    assert s._skip_counts.get("batch_wait", 0) == 0


def test_denied_cycle_counts_wave_wait():
    """本周期已被共振组额度拦过（denied）→ 后续尝试同样记 wave_wait。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(slots=[{"slot": 1}]))
    _activate(s, {"wave_max_buys": 1})
    _wave_full_state(s, count=1)
    s._denied_cycle = {("high", "SOL"): True}
    s._evaluate_symbol()
    assert s._skip_counts.get("wave_wait", 0) == 1
    assert s._skip_counts.get("cycle_wait", 0) == 0


def test_wave_lock_off_no_wave_wait():
    """wave_max_buys=0（关闭）→ 组再满也不产生 wave_wait。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(slots=[{"slot": 1}]))
    _activate(s, {"wave_max_buys": 0})
    _wave_full_state(s, count=5)
    s._evaluate_symbol()
    assert s._skip_counts.get("wave_wait", 0) == 0
    assert s._skip_counts.get("batch_wait", 0) == 0


# ── 展示层映射同源 ─────────────────────────────────────────────────────


def test_labels_registered_both_surfaces():
    """页面 SKIP_LABELS 与 markdown 映射都要有三个键（两份表必须同步）。"""
    from nanobot_quant.backtest_markdown import _SKIP_LABELS

    root = Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"
    page = (root / "backtest_page.html").read_text(encoding="utf-8")
    for key in ("cycle_wait", "wave_wait", "batch_wait"):
        assert key in _SKIP_LABELS, f"markdown 缺 {key}"
        assert f"{key}:" in page, f"页面缺 {key}"
    assert "旧合并口径" in _SKIP_LABELS["batch_wait"]
