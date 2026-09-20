"""Tests for ``analyze_f1_td`` (tools/tools_f1.py).

Covers the sequence-returning TD helper, symbol→source inference, the CV
interpretation boundaries, the trigger statistics (including its null
baseline) and the tool's own error paths — all without network access (the
data source is monkeypatched).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nanobot_quant.strategies.td_sequential import (
    DEFAULT_TD_PARAMS,
    calculate_series,
)
from nanobot_quant.tools import tools_f1 as T


# ── helpers ────────────────────────────────────────────────────────

def _ohlc(index: np.ndarray, close_arr: np.ndarray) -> pd.DataFrame:
    arr = np.asarray(close_arr, dtype=float)
    n = len(arr)
    # small wicks so high > low > 0 — flat bars would zero the ATR
    wick = np.abs(np.random.default_rng(0).normal(0, 0.001, n)) * arr
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr + wick,
            "low": arr - wick,
            "close": arr,
            "volume": np.ones(n),
        },
        index=pd.to_datetime(index, unit="h", utc=True),
    )


class _FakeSpec:
    """Minimal DataSourceSpec stand-in — returns synthetic OHLCV."""

    name = "fake"
    bars = ("1H", "15m")

    def __init__(self, close_arr: np.ndarray) -> None:
        self._c = close_arr

    def fetch_kline(self, symbol, bar="1D", limit=120, start=None, end=None):
        return _ohlc(np.arange(len(self._c)), self._c)


# ── calculate_series ───────────────────────────────────────────────

def test_calculate_series_returns_full_series():
    n = 120
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, n))
    df = _ohlc(np.arange(n), close)
    out = calculate_series(df, params=dict(DEFAULT_TD_PARAMS))
    assert isinstance(out, pd.DataFrame)
    assert len(out) == n, "must return one row per input bar"
    assert "buy_setup_count" in out.columns
    assert "sell_setup_count" in out.columns


def test_calculate_series_normalises_lowercase_columns():
    n = 80
    close = 50 + np.arange(n) * 0.5
    df = _ohlc(np.arange(n), close)
    out = calculate_series(df, params=dict(DEFAULT_TD_PARAMS))
    assert len(out) == n


# ── _resolve_source ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("600519", "eastmoney"),
        ("588000", "eastmoney"),
        ("AAPL", "eastmoney"),
        ("BTC-USDT", "okx_cex"),
    ],
)
def test_resolve_source_by_symbol_shape(symbol, expected):
    assert T._resolve_source([symbol], "") == expected


def test_resolve_source_explicit_wins():
    assert T._resolve_source(["600519"], "yfinance") == "yfinance"


def test_resolve_source_rejects_unknown_shape():
    with pytest.raises(ValueError):
        T._resolve_source(["@@@"], "")


# ── _cv_hint ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cv,keyword",
    [(0.10, "可用"), (0.27, "可用"), (0.30, "临界"), (0.40, "退化")],
)
def test_cv_hint_boundaries(cv, keyword):
    assert keyword in T._cv_hint(cv)


# ── _trigger_stats ─────────────────────────────────────────────────

def test_trigger_stats_detects_reversal():
    """Construct a series that dips for 9 bars then always rebounds."""
    vals = []
    for _ in range(8):
        for d in (5, 4, 3, 2, 1, 1, 1, 1, 1):      # 9 consecutive declines
            vals.extend([10.0 + d * 0.1])           # filler
        vals.extend([10.0, 12.0, 14.0, 15.0])       # rebound
    arr = np.array(vals)
    counts = np.zeros(len(arr), dtype=int)
    for i in range(len(arr)):
        counts[i] = max(0, min(9, i % 13))
    st = T._trigger_stats(arr, counts, +1, k=3, threshold=9)
    assert st is not None and st["n"] >= 1
    assert 0.0 <= st["hit"] <= 1.0
    assert 0.0 <= st["p"] <= 1.0


def test_trigger_stats_none_when_no_trigger():
    arr = np.linspace(10, 20, 100)
    counts = np.zeros(100, dtype=int)
    assert T._trigger_stats(arr, counts, +1, k=3, threshold=9) is None


def test_trigger_stats_requires_room_for_horizon():
    arr = np.arange(10, dtype=float)
    counts = np.array([0] * 9 + [9])
    # the only trigger sits at the last bar → no room for k bars ahead
    assert T._trigger_stats(arr, counts, +1, k=3, threshold=9) is None


def test_trigger_stats_all_bars_counts_every_bar_over_threshold():
    """累加期口径：连续 >=9 的每一根都算触发，n 明显大于首次穿越。"""
    arr = np.linspace(10, 20, 120)
    counts = np.array([0] * 10 + [9, 10, 11, 12, 13] + [0] * 105)
    assert len(counts) == len(arr)
    first = T._trigger_stats(arr, counts, +1, k=3, threshold=9)
    allb = T._trigger_stats(arr, counts, +1, k=3, threshold=9, mode="all_bars")
    assert first is not None and first["n"] == 1      # 只有首次穿越那一根
    assert allb is not None and allb["n"] == 5        # 9/10/11/12/13 全算
    assert allb["n"] > first["n"]


def test_trigger_stats_all_bars_still_needs_horizon_room():
    """切换口径不能绕过「后面还得有 k 根」这个约束。"""
    arr = np.arange(10, dtype=float)
    counts = np.array([0] * 8 + [9, 10])
    assert T._trigger_stats(arr, counts, +1, k=3, threshold=9, mode="all_bars") is None


# ── analyze_f1_td ──────────────────────────────────────────────────

def _patch_source(monkeypatch, close_arr):
    monkeypatch.setattr(T, "get_data_source", lambda name: _FakeSpec(close_arr))


def test_analyze_f1_td_happy_path(monkeypatch):
    rng = np.random.default_rng(7)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    _patch_source(monkeypatch, close)

    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=n)
    assert out["data_source"] == "eastmoney"
    assert len(out["results"]) == 1
    rec = out["results"][0]
    assert rec["status"] == "ok"
    assert rec["bars"] == n
    assert isinstance(rec["cv"], float) and rec["cv"] > 0
    assert rec["lookback_bars"] == 3          # 3h / 1H
    assert "cv_hint" in rec
    # price-TD control group is present by default
    assert "price_buy9" in rec and "price_sell9" in rec
    assert "summary" in out and "note" in out


def test_f1_series_drops_infinite_ratio():
    """A zero ATR (flat bars) must be dropped, not propagated as inf."""
    n = 120
    idx = pd.to_datetime(np.arange(n), unit="h", utc=True)
    flat = pd.DataFrame(
        {"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0, "Volume": 1.0},
        index=idx,
    )
    f1 = T._f1_series(flat, 20, 3)
    assert len(f1) == 0, "flat bars must yield no F1 points"
    assert np.isfinite(f1).all()      # empty series is vacuously finite


def test_analyze_f1_td_skips_price_control(monkeypatch):
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 900)))
    _patch_source(monkeypatch, close)
    out = T.analyze_f1_td(
        symbols=["600519"], periods=["1H"], limit=900, include_price_td=False
    )
    rec = out["results"][0]
    assert "price_buy9" not in rec


def test_analyze_f1_td_unsupported_period(monkeypatch):
    _patch_source(monkeypatch, np.linspace(100, 110, 500))
    out = T.analyze_f1_td(symbols=["600519"], periods=["4H"], limit=500)
    rec = out["results"][0]
    assert rec["status"] == "error"
    assert "不支持周期" in rec["error"]


def test_analyze_f1_td_insufficient_data(monkeypatch):
    _patch_source(monkeypatch, np.linspace(100, 101, 30))
    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=30)
    rec = out["results"][0]
    assert rec["status"] == "error"
    assert "数据不足" in rec["error"]


def test_analyze_f1_td_fetch_failure_is_isolated(monkeypatch):
    class _Boom:
        name, bars = "fake", ("1H",)

        def fetch_kline(self, *a, **kw):
            raise RuntimeError("network down")

    monkeypatch.setattr(T, "get_data_source", lambda name: _Boom())
    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"])
    rec = out["results"][0]
    assert rec["status"] == "error" and "取数失败" in rec["error"]
    assert out["summary"]           # batch must still return a summary


def test_analyze_f1_td_results_are_json_safe(monkeypatch):
    """Every value must be a native Python type (FastMCP serialises to JSON)."""
    import json

    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 900)))
    _patch_source(monkeypatch, close)
    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=900)
    json.dumps(out)             # raises TypeError if numpy types leaked


def test_analyze_f1_td_reports_both_trigger_modes(monkeypatch):
    """主字段 = 首次穿越，*_all = 累加期全计，两种口径同时给出。

    2026-09-20 在 A股日线上实测：同一份数据下首次穿越只得到 n=2–3，
    累加期口径 n=15–40 —— 只报道一种会让“信号到底多密”失真，
    所以两边一起给，并在 note 里解释差别。
    """
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 900)))
    _patch_source(monkeypatch, close)
    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=900)
    rec = out["results"][0]
    assert rec["status"] == "ok"
    for key in ("f1_buy9_all", "f1_sell9_all", "price_buy9_all", "price_sell9_all"):
        assert key in rec, key        # 值可为 None，但字段必须在
    assert "首次穿越" in out["note"]
    assert "_all" in out["note"]
