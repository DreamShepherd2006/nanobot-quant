"""Tests for OnchainOS kline data adapter (range fetch via CLI)."""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from nanobot_quant import onchainos_data as mod


def _mock_kline_df(n=10, start_ts=1780000000000, bar_s=86400):
    """Build a kline DataFrame with `n` candles starting at start_ts."""
    rows = []
    for i in range(n):
        ts = start_ts + i * bar_s * 1000
        rows.append({"ts": ts, "o": 100 + i, "h": 102 + i, "l": 99 + i,
                     "c": 101 + i, "v": 1000 + i})
    data = {"code": "0", "msg": "", "data": rows}
    return mod.parse_kline_response(json.loads(json.dumps(data)))


def test_fetch_kline_range_single_call_no_before(monkeypatch):
    """范围 ≤ MAX_LIMIT 时：单次调用、不传 --before、本地裁剪。"""
    calls = []

    def fake_run_cli(args):
        calls.append(args)
        assert "--before" not in args
        return {"ok": True, "data": []}  # unused; df is mocked below

    df = _mock_kline_df(n=10, start_ts=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000))
    monkeypatch.setattr(mod, "_run_cli", fake_run_cli)
    monkeypatch.setattr(mod, "parse_kline_response", lambda data: df)

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 10, tzinfo=timezone.utc)
    out = mod.fetch_kline_range("solana", "Xaddr", start=start, end=end, bar="1D")

    assert len(calls) == 1
    args = calls[0]
    assert args[:4] == ["market", "kline", "--address", "Xaddr"]
    assert "--limit" in args
    assert "--before" not in args
    assert not out.empty


def test_fetch_kline_range_limited_range_raises(monkeypatch):
    """范围需要 > MAX_LIMIT 根时：明确 ValueError，不发起分页。"""
    calls = []
    monkeypatch.setattr(mod, "_run_cli", lambda args: calls.append(args) or {})
    monkeypatch.setattr(mod, "parse_kline_response", lambda data: pd.DataFrame())

    # 90 天 1H = 2160 根 > 300
    start = datetime(2026, 5, 7, tzinfo=timezone.utc)
    end = datetime(2026, 8, 5, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="单次最多返回 300 根"):
        mod.fetch_kline_range("solana", "Xaddr", start=start, end=end, bar="1H")

    assert calls == []  # 未发起任何 CLI 调用


def test_fetch_kline_range_unsupported_bar(monkeypatch):
    """未知周期 → 明确错误。"""
    with pytest.raises(ValueError, match="不支持的周期"):
        mod.fetch_kline_range("solana", "Xaddr",
                              start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                              end=datetime(2026, 1, 10, tzinfo=timezone.utc),
                              bar="3H")


def test_fetch_kline_range_default_end_now(monkeypatch):
    """end 缺省为当前时间（UTC）。"""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    df = _mock_kline_df(n=10, start_ts=int(start.timestamp() * 1000))
    monkeypatch.setattr(mod, "_run_cli", lambda args: {})
    monkeypatch.setattr(mod, "parse_kline_response", lambda data: df)
    out = mod.fetch_kline_range("solana", "Xaddr", start=start, bar="1D")
    assert not out.empty
    assert len(out) <= 10  # 本地裁剪到 [start, now]
