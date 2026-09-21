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
        ("600519", "sina"),
        ("588000", "sina"),
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

def test_cv_is_descriptive_only():
    """CV 已不再作可用性判据（2026-09-20 实证推翻），只作序列描述。

    旧行为：CV≤0.27 判「可用」、0.27–0.35「临界」、>0.35「退化」。
    新行为：无论 CV 多大，输出一律是描述 + 显式否定判据用途。
    """
    for cv in (0.10, 0.27, 0.30, 0.40, 0.99):
        s = T._cv_hint(cv)
        assert isinstance(s, str)
        assert "不可用作可用性判据" in s, f"CV={cv} 的输出未声明不作判据: {s}"
        assert f"{cv:.3f}" in s, f"CV={cv} 的数值未出现在描述中: {s}"
    # 不得再出现旧的三档判词
    for cv in (0.10, 0.30, 0.40):
        s = T._cv_hint(cv)
        assert "可用" not in s.replace("不可用作可用性判据", "")
        assert "退化" not in s


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


def test_fetch_with_fallback_switches_source_on_failure(monkeypatch):
    """A 股首选源取数失败 → 自动换兄弟源，并留日志（不静默降级）。"""
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, 400)))
    calls: list[str] = []

    class _DeadSpec:
        bars = ("1H",)

        def fetch_kline(self, *a, **k):
            raise RuntimeError("sina down")

    def fake_get(name):
        calls.append(name)
        return _FakeSpec(close)

    monkeypatch.setattr(T, "get_data_source", fake_get)
    df, used = T._fetch_with_fallback(_DeadSpec(), "sina", "600519", "1H", 400)
    assert used == "eastmoney"
    assert calls == ["eastmoney"]   # 只给兄弟源发请求，不重试已死的源
    assert len(df) > 0


def test_fetch_with_fallback_reraises_when_no_alt(monkeypatch):
    """没有兄弟源的源（okx_cex）失败时必须原样抛出，不吞异常。"""

    class _Boom:
        bars = ("1H",)

        def fetch_kline(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        T._fetch_with_fallback(_Boom(), "okx_cex", "BTC-USDT", "1H", 300)


def test_fetch_with_fallback_reraises_when_alt_lacks_period(monkeypatch):
    """兄弟源不支持该周期时不发第二发请求，直接冒原始异常。"""

    class _Boom:
        bars = ("1H",)

        def fetch_kline(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(T, "get_data_source",
                        lambda name: type("S", (), {"bars": ("1D",)})())
    with pytest.raises(RuntimeError, match="boom"):
        T._fetch_with_fallback(_Boom(), "sina", "600519", "1H", 300)


def test_analyze_f1_td_happy_path(monkeypatch):
    rng = np.random.default_rng(7)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    _patch_source(monkeypatch, close)

    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=n)
    assert out["data_source"] == "sina"
    assert len(out["results"]) == 1
    rec = out["results"][0]
    assert rec["status"] == "ok"
    assert rec["bars"] == n
    assert isinstance(rec["cv"], float) and rec["cv"] > 0
    assert rec["lookback_bars"] == 3          # 3h / 1H
    assert "cv_hint" in rec
    # price-TD control group is present by default
    assert "price_buy9" in rec and "price_sell9" in rec
    # 自反应参考行（口径修正后新增）
    assert "f1_self_buy9" in rec and "f1_self_sell9" in rec
    assert "summary" in out and "note" in out


def test_f1_rows_measure_price_not_the_f1_series(monkeypatch):
    """口径回归（2026-09-21）：F1 行以**价格**为被测量对象。

    修正前 ``f1_buy9`` 传的是 F1 数组，量的是波动率均值回归本身（同义反复，
    必然得到 p≈0 的「显著」）。现要求 F1 行（前 4 次调用）传价格，只有
    自反应参考行（第 5、6 次）才传 F1 序列。
    """
    rng = np.random.default_rng(11)
    n = 900
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    _patch_source(monkeypatch, close)

    seen: list[np.ndarray] = []
    real = T._trigger_stats

    def spy(vals, counts, sign, k, threshold, ntrials=200, seed=42, mode="first_cross"):
        seen.append(np.asarray(vals, dtype=float))
        return real(vals, counts, sign, k, threshold,
                    ntrials=ntrials, seed=seed, mode=mode)

    monkeypatch.setattr(T, "_trigger_stats", spy)
    out = T.analyze_f1_td(symbols=["600519"], periods=["1H"], limit=n)
    assert out["results"][0]["status"] == "ok"
    assert len(seen) >= 6
    # _f1_series 会 dropna（ATR 预热）：F1 行与自反应行都比价格短，
    # 且必须按 F1 的索引对齐（尾部取价，非头部）
    f1_len = len(seen[4])
    assert f1_len < len(close)
    for vals in seen[:4]:                       # F1 行（含累加期）→ 价格
        assert len(vals) == f1_len
        assert np.allclose(vals, close[-f1_len:])
    for vals in seen[4:6]:                      # 自反应参考行 → F1 序列（非价格）
        assert len(vals) == f1_len
        assert not np.allclose(vals, close[-f1_len:])


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
