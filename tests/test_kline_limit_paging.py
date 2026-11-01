"""K 线单次上限兜底：limit 超过数据源单次能力时改走区间分页（C95）。

2026-11-01 实测：OKX `candles` 请求 limit=2137 只回 299 根（端点恒返回最近
~300 根）。EMA 折价门 n=610 需要 2137 根（5(n+1)/2 预热），拿不到就会：
  ① KlineCache `len(df) < length` 恒真 → 每轮全量重建
  ② 策略 `bars=299 < min_history=300` → 每轮 SKIP、一笔不交易
  ③ EMA610 在 299 根上算 → 数值严重失真
故数据源层在 limit 超单次能力时改走 history-candles 反向分页。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.data_sources import gate_cex, okx_cex


def _df(rows: int) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="min", tz="UTC")
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0},
        index=idx,
    )


@pytest.fixture
def _patch_okx(monkeypatch):
    calls: dict[str, object] = {}

    def fake_single(inst, bar="1D", limit=100, session=None):
        calls["single"] = (inst, bar, limit)
        return _df(min(limit, okx_cex._SINGLE_FETCH_MAX))

    def fake_range(inst, start=None, end=None, bar="1D"):
        calls["range"] = (inst, start, end, bar)
        return _df(2500)  # 多给一点，验证 tail(limit) 截断

    monkeypatch.setattr(okx_cex, "_fetch_kline_okx", fake_single)
    monkeypatch.setattr(okx_cex, "_fetch_kline_range_okx", fake_range)
    monkeypatch.setattr(okx_cex, "okx_ticker", lambda symbol, tokens=None: symbol)
    monkeypatch.setattr(okx_cex, "load_tokens_json", lambda: [])
    return calls


@pytest.fixture
def _patch_gate(monkeypatch):
    calls: dict[str, object] = {}

    def fake_single(pair, bar="1D", limit=100):
        calls["single"] = (pair, bar, limit)
        return _df(min(limit, gate_cex._SINGLE_FETCH_MAX))

    def fake_range(pair, start, end, bar="1D"):
        calls["range"] = (pair, start, end, bar)
        return _df(2500)

    monkeypatch.setattr(gate_cex, "fetch_gate_kline", fake_single)
    monkeypatch.setattr(gate_cex, "fetch_gate_kline_range_paged", fake_range)
    monkeypatch.setattr(gate_cex, "gate_pair", lambda symbol, tokens=None: symbol)
    monkeypatch.setattr(gate_cex, "load_tokens_json", lambda: [])
    return calls


def test_okx_large_limit_uses_range_paging(_patch_okx):
    """limit > 299 → 走 history-candles 分页，并 tail(limit) 截到请求根数。"""
    out = okx_cex.fetch_kline("BTC", "1m", 2137)

    assert len(out) == 2137
    assert "range" in _patch_okx
    assert "single" not in _patch_okx
    # 区间必须覆盖「limit + 2」根 1m，且保留时分秒
    _, start, end, bar = _patch_okx["range"]
    assert bar == "1m"
    assert start != end and "T" in start


def test_okx_small_limit_stays_on_single_fetch(_patch_okx):
    """limit ≤ 299 维持原单次路径（零行为变化）。"""
    out = okx_cex.fetch_kline("BTC", "1m", 120)

    assert len(out) == 120
    assert "single" in _patch_okx
    assert "range" not in _patch_okx


def test_okx_explicit_range_wins_over_limit(_patch_okx, monkeypatch):
    """显式 start/end 优先走区间分页（增量路径不受影响）。"""
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    okx_cex.fetch_kline("BTC", "1m", 2137, start=start, end=end)

    assert "range" in _patch_okx
    assert "single" not in _patch_okx


def test_gate_large_limit_uses_range_paging(_patch_gate):
    out = gate_cex.fetch_kline("BTC", "1m", 2137)

    assert len(out) == 2137
    assert "range" in _patch_gate
    assert "single" not in _patch_gate


def test_gate_small_limit_stays_on_single_fetch(_patch_gate):
    out = gate_cex.fetch_kline("BTC", "1m", 120)

    assert len(out) == 120
    assert "single" in _patch_gate
    assert "range" not in _patch_gate


def test_unknown_bar_fails_loud(monkeypatch, _patch_okx):
    """未知周期不得静默回退单次路径（否则又会退化成截断）。"""
    with pytest.raises(ValueError):
        okx_cex.fetch_kline("BTC", "7q", 2137)
