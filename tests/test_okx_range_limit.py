"""C103：单次区间上限按数据源声明 + 取数真因不得被吞。

背景：``kline_paging.MAX_BARS_PER_REQUEST = 10_000`` 是**自家性能护栏**，
不是平台限制。Gate 1m 平台硬限 ≈6.9 天（HTTP 400 深度闸门），但 OKX 1m
实测可回溯 ≥260 天（2026-11-03 实测）；90 天 1m = 129,600 根被护栏一刀切死，
且 ``prefetch`` 把 ``KlineRangeTooLarge`` 吞成一行日志 ⇒ 上层只报
「bars=0 历史数据不足」，真因丢失。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from nanobot_quant import kline_paging
from nanobot_quant.backtest.driver import _insufficient_bars_error
from nanobot_quant.backtest import fetchers
from nanobot_quant.data_sources import get_data_source


def test_okx_declares_max_backfill_days():
    spec = get_data_source("okx_cex")
    assert spec.max_backfill_days == 180


def test_max_bars_for_by_source():
    # OKX：180 天按周期换算（1m 259,200 / 15m 17,280）
    assert kline_paging.max_bars_for("1m", "okx_cex") == 180 * 86_400 // 60
    assert kline_paging.max_bars_for("15m", "okx_cex") == 180 * 86_400 // 900
    # 未声明源 / 未给源 / 未知源 → 全局护栏
    assert kline_paging.max_bars_for("1m") == kline_paging.MAX_BARS_PER_REQUEST
    assert kline_paging.max_bars_for("1m", "gate_cex") == kline_paging.MAX_BARS_PER_REQUEST
    assert kline_paging.max_bars_for("1m", "no_such_source") == kline_paging.MAX_BARS_PER_REQUEST


def test_okx_range_fetch_uses_source_cap(monkeypatch):
    """显式区间走 OKX 可回溯上限，不再被 10,000 根拦。"""
    import nanobot_quant.data_sources.okx_cex as m

    captured: dict = {}
    monkeypatch.setattr(m, "okx_ticker", lambda s, t: "SOL-USDT")
    monkeypatch.setattr(m, "load_tokens_json", lambda: [])

    def fake_range(inst, start=None, end=None, bar="1m", max_bars=None, **kw):
        captured.update(inst=inst, max_bars=max_bars)
        return pd.DataFrame()

    monkeypatch.setattr(m, "_fetch_kline_range_okx", fake_range)
    m.fetch_kline(
        "SOL", bar="1m",
        start=datetime(2026, 8, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 2, tzinfo=timezone.utc),
    )
    assert captured["max_bars"] == kline_paging.max_bars_for("1m", "okx_cex")
    assert captured["max_bars"] >= 129_600  # 90 天 1m 必须放得下


def test_okx_backtest_fetcher_passes_source_cap(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        fetchers, "fetch_kline_range",
        lambda t, s, e, bar="1m", max_bars=None: captured.update(
            max_bars=max_bars) or pd.DataFrame(),
    )
    fetchers._okx_fetch("SOL-USDT", 1_700_000_000, 1_700_100_000, "1m")
    assert captured["max_bars"] == kline_paging.max_bars_for("1m", "okx_cex")


def test_full_range_still_uses_global_cap(monkeypatch):
    """「拉满」（start<=0）仍按全局默认截最近窗口，避免误触几百页抓取。"""
    captured: dict = {}
    monkeypatch.setattr(
        fetchers, "fetch_kline_range",
        lambda t, s, e, bar="1m", max_bars=None: captured.update(
            start=s, max_bars=max_bars) or pd.DataFrame(),
    )
    end = 1_700_000_000
    fetchers._okx_fetch("SOL-USDT", 0, end, "1m")
    start = int(datetime.fromisoformat(captured["start"]).timestamp())
    assert end - start == kline_paging.MAX_BARS_PER_REQUEST * 60


def test_insufficient_bars_error_carries_prefetch_cause():
    msg = _insufficient_bars_error(
        "1m", "2026-08-02", "2026-11-01", 0, 299,
        {"SOL": "OKX CEX 单次最多 10,000 根 1m（约 6.9 天），当前区间 91.0 天超过上限"},
    )
    assert "bars=0" in msg
    assert "取数失败原因" in msg
    assert "超过上限" in msg


def test_insufficient_bars_error_without_cause_is_unchanged():
    msg = _insufficient_bars_error("1m", "a", "b", 0, 299, None)
    assert "取数失败原因" not in msg
