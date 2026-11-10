"""入场多周期：**每个周期独立**计数与独立门控（2026-11-10 方案 A）。

背景（实盘实证）：旧实现把各周期取 ``max`` 后与**单一** ``prev_setup``
比较单调性，靠「计数变小 = 新周期」判断是否允许再建仓。多周期下这会出错——
「主导周期换主角」时被误判成计数回落：2026-11-10 SOL 4m 数到 12 后归零、
5m 的 10 顶上 ⇒ ``12 → 10`` ⇒ 判为新周期 ⇒ 16 分钟内重复建仓（CRCL 3m 13
→ 5m 12 同型）。

方案 A（用户 2026-11-10 拍板）：状态键改为 ``(symbol, 周期)``，每个周期各自
维护 bought / reset / cd_triggered，**跨周期不比较、不等待**——谁先数到阈值
谁触发一次，该周期自己回落后再次达到阈值才算它自己的新周期。

本文件的用例即该语义的契约：
  ① 某周期达阈值 → 触发一次
  ② 同一周期内计数继续累加 → 不再触发
  ③ **另一周期仍可独立触发**（不等前一周期回落）← 旧实现在此会卡住
  ④ 同周期自身回落后再次达阈值 → 允许再建
  ⑤ 单周期（``entry_periods=1``）行为与改造前完全一致（零回归）
  ⑥ 一轮只建 1 批：多周期同时满足时取最小倍数，另一个留到下一轮
  ⑦ 重启边界：有 open 仓位且该周期已达阈值 → 视为已建仓（保守不追）
"""

from __future__ import annotations

import logging

import pandas as pd


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
    return Bars(df, "OKX", None)


def _bars_flat():
    """横盘：末根 setup 计数无意义，本文件一律注入各周期读数。"""
    return _bars([100.0] * 60)


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

    def open_lot(self, **kw):
        self._costs.append(kw.get("entry_price") or 0.0)

    def save(self):
        return None


def _mock_order(quantity=1.0):
    return type(
        "Order",
        (),
        {
            "identifier": "mock-id",
            "quantity": quantity,
            "error": None,
            "status": "unsubmitted",
            "custom_params": None,
            "is_filled": lambda self=None: True,
            "set_error": lambda self, e: setattr(self, "error", e),
        },
    )()


def _make(per_readings: dict, *, periods=(1, 4), bm=None, **params):
    """构造策略并把各周期读数直接注入（绕过真实 resample/TD 计算）。"""
    params.setdefault("min_history", 50)
    # 本文件只测「周期门控」：关闭共振波锁（实盘 high 场景同为 0=关），
    # 否则同一组第 2 笔会被 wave_max_buys=1 的默认值拦下。
    params.setdefault("wave_max_buys", 0)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-entry-periods-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = _bars_flat()
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars
    captured: dict = {}
    s.create_order = lambda asset, quantity, action: (
        captured.update(order=(asset, quantity, action)) or _mock_order(quantity)
    )
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = bm if bm is not None else _FakeBM(slots=[{"slot": 1}])
    s.initialize()
    s.symbol = "SOL"
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9
    s._home_account = "acc-home"
    buy_calls: list = []
    s._buy_on_slot = lambda *a, **k: (buy_calls.append(a) or (_mock_order(), 1.0))
    s._buy_calls = buy_calls
    s._captured = captured

    s.broker = object()
    s._activate_scene("high", {"params": dict(s.parameters)})
    s._entry_periods = list(periods)

    # 可变读数：测试改 s._per_readings 即模拟「下一轮计数变化」
    s._per_readings = dict(per_readings)

    def _fake_calc_multi(df):
        s._last_multi = dict(s._per_readings)
        sig = dict(s._calc(df))
        vals = list(s._per_readings.values())
        sig["setup_buy"] = max((v[0] for v in vals if v), default=0)
        sig["cd_buy"] = max((v[1] for v in vals if v), default=0)
        # score 必须 > score_threshold（默认 0），否则 BUY 分支不进入
        sig["score"] = 5.0
        return sig

    s._calc_multi = _fake_calc_multi
    # 统一判定「本轮是否真的下单」：DEX 路径走 _buy_on_slot、CEX 路径走
    # broker/create_order，两条都算。
    s._did_buy = lambda: bool(captured.get("order") or buy_calls)
    s._reset_buy = lambda: (captured.pop("order", None), buy_calls.clear())
    return s


def _bought_periods(s):
    return sorted(
        m
        for (sym, m), st in (s._cycle_state or {}).items()
        if sym == s.symbol and st.get("bought")
    )


# ── ① 某周期达阈值 → 触发一次 ───────────────────────────────────────────


def test_period_hits_triggers_buy():
    s = _make({1: (2, 0), 4: (9, 0)})
    s._evaluate_symbol()
    assert s._did_buy(), "4m 达阈值应触发建仓"
    assert s._cur_entry_period == 4
    assert _bought_periods(s) == [4], "只标记触发周期"


# ── ② 同一周期累加不再触发 ──────────────────────────────────────────────


def test_same_period_accumulation_blocked():
    s = _make({1: (2, 0), 4: (9, 0)})
    s._evaluate_symbol()
    s._reset_buy()

    s._per_readings = {1: (3, 0), 4: (12, 0)}  # 同一 4m 周期继续累加
    s._evaluate_symbol()
    assert not s._did_buy(), "同周期累加不应再次建仓"
    assert s._skip_counts.get("cycle_wait", 0) == 1


# ── ③ 另一周期独立触发（旧实现会卡住）─────────────────────────────────


def test_other_period_triggers_independently():
    """4m 已建仓、计数仍在累加；5m 自己数到 9 → 应独立触发，不等 4m 回落。

    这是与旧 `max` 聚合语义的核心差异：旧实现里 5m 的 10 被 4m 的 12 压着，
    要等 4m 归零后 `12 → 10` 才「浮出」，还被误判成新周期。
    """
    s = _make({1: (2, 0), 4: (12, 0), 5: (9, 0)}, periods=(1, 4, 5))
    s._evaluate_symbol()
    assert s._did_buy(), "4m/5m 同时满足 → 本轮建 1 批（取最小倍数 4m）"
    assert s._cur_entry_period == 4
    assert _bought_periods(s) == [4]
    s._reset_buy()

    # 4m 继续累加（未回落），5m 从 9 → 10
    s._per_readings = {1: (3, 0), 4: (12, 0), 5: (10, 0)}
    s._evaluate_symbol()
    assert s._did_buy(), "5m 独立周期应触发"
    assert s._cur_entry_period == 5
    assert _bought_periods(s) == [4, 5]


# ── ④ 同周期自身回落后再次达阈值 → 允许再建 ────────────────────────────


def test_period_reset_allows_rebuy():
    s = _make({1: (2, 0), 4: (9, 0)})
    s._evaluate_symbol()
    s._reset_buy()

    s._per_readings = {1: (2, 0), 4: (0, 0)}  # 4m 归零 → 该周期 reset
    s._evaluate_symbol()
    assert not s._did_buy()
    assert s._cycle_state[("SOL", 4)]["reset"] is True

    s._per_readings = {1: (2, 0), 4: (9, 0)}  # 4m 重新数到 9 → 新周期
    s._evaluate_symbol()
    assert s._did_buy(), "同周期回落后再次达阈值应允许再建"


# ── ⑤ 单周期零回归 ──────────────────────────────────────────────────────


def test_single_period_behaviour_unchanged():
    s = _make({1: (9, 0)}, periods=(1,))
    s._evaluate_symbol()
    assert s._did_buy()
    assert s._cur_entry_period == 1

    s._reset_buy()
    s._per_readings = {1: (10, 0)}
    s._evaluate_symbol()
    assert not s._did_buy(), "单周期：累加期不应重复建仓"

    s._per_readings = {1: (0, 0)}
    s._evaluate_symbol()
    s._per_readings = {1: (9, 0)}
    s._evaluate_symbol()
    assert s._did_buy(), "单周期：reset 后应允许再建"


# ── ⑥ 多周期同时满足：一轮只建 1 批，最小的那个先建 ────────────────────


def test_multiple_hits_one_order_per_round():
    s = _make({1: (9, 0), 4: (9, 0)})
    s._evaluate_symbol()
    assert s._did_buy()
    assert s._cur_entry_period == 1, "取最小倍数"
    assert _bought_periods(s) == [1]

    s._reset_buy()
    s._per_readings = {1: (10, 0), 4: (9, 0)}
    s._evaluate_symbol()
    assert s._did_buy(), "4m 尚未建仓，应下一轮触发"
    assert s._cur_entry_period == 4
    assert _bought_periods(s) == [1, 4]


# ── ⑦ 重启边界：有 open 仓位且该周期已达阈值 → 视为已建仓（保守）──────


def test_open_position_blocks_current_hit_period():
    s = _make(
        {1: (2, 0), 4: (12, 0)}, bm=_FakeBM(slots=[{"slot": 1}], costs=[100.0])
    )
    s._evaluate_symbol()
    assert not s._did_buy(), "有 open 仓 + 该周期已达阈值 → 不追"
    assert _bought_periods(s) == [4]
