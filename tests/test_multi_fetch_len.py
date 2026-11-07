"""多周期取数长度：EMA 预热不得被倍数放大（2026-11-07 实盘回归）。

背景：entry_periods=1,2,3,4,5,8 + entry_ema_period=610 时旧公式
max(300, 2137) × 8 = 17096 根，撞数据源单次上限（10,000）导致每轮
``KlineRangeTooLarge``、TD 完全停摆。EMA 门只在基础周期判定，
预热长度应与倍数取 max 而不是相乘。
"""
from __future__ import annotations

import pytest


def _mk(monkeypatch, min_history=300, periods="1", ema_enabled=False, ema_period=610):
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st._min_history = min_history
    st._entry_periods = [int(x) for x in periods.split(",")]
    st._entry_ema_enabled = ema_enabled
    st._entry_ema_period = ema_period
    st._entry_ema_offset_pct = 0.1
    st._is_live_broker = True
    st._timestep = "1m"
    st.symbols = ["SOL"]
    st.symbol = "SOL"
    st.parameters = {"drops_in_progress_bars": True}

    captured = {}

    def fake_get(sym, length, timestep):
        captured["length"] = length
        return None

    monkeypatch.setattr(st, "get_historical_prices", fake_get, raising=False)
    return st, captured


def test_ema_warmup_not_multiplied_by_periods(monkeypatch):
    """EMA610 预热 2137 根 + 8 倍周期 ⇒ 取数 2400（300×8），不是 17096。"""
    st, cap = _mk(monkeypatch, min_history=300, periods="1,2,3,4,5,8", ema_enabled=True, ema_period=610)
    res = st._fetch_bars_worker("SOL")
    assert res is not None
    assert cap["length"] == max(300 * 8, 2137) == 2400


def test_single_period_still_uses_ema_warmup(monkeypatch):
    """单周期时取数长度 = EMA 预热（2137），与旧行为一致。"""
    st, cap = _mk(monkeypatch, min_history=300, periods="1", ema_enabled=True, ema_period=610)
    st._fetch_bars_worker("SOL")
    assert cap["length"] == 2137


def test_ema_off_uses_periods_only(monkeypatch):
    st, cap = _mk(monkeypatch, periods="1,2,3,4,5,8", ema_enabled=False)
    st._fetch_bars_worker("SOL")
    assert cap["length"] == 2400


def test_fetch_len_within_source_cap(monkeypatch):
    """组合场景必须落在数据源单次上限内（10,000 根）。"""
    st, cap = _mk(monkeypatch, min_history=300, periods="1,2,3,4,5,8", ema_enabled=True, ema_period=1597)
    st._fetch_bars_worker("SOL")
    assert cap["length"] <= 10000
