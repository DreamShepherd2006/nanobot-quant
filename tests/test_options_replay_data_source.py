"""期权回测数据层（OptionsReplayDataSource）单测。

全部注入假 fetcher / lifecycle_fetcher —— **不打网络**，确定性。
覆盖：区间归一化、周期映射、合约枚举（instId 规则）、在售过滤、
权利金就近取价、lumibot DataSource 契约。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from nanobot_quant.backtest.options_replay_data_source import (
    OptionsReplayDataSource,
    _fmt_strike,
    _resolve_bar,
    _to_ts,
)

# ── 假数据 ────────────────────────────────────────────────────────────

_IDX = pd.date_range("2026-09-01 00:00", periods=24, freq="1h", tz="UTC")


def _kline():
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": [100.0 + i for i in range(len(_IDX))],
            "volume": 1000.0,
        },
        index=_IDX,
    )


def _lifecycle(inst_id: str, bar: str):
    """每合约一条 mark 序列（覆盖全区间，价格随 bar 波动）。"""
    rows = [
        {"ts": int(t.timestamp() * 1000), "mark_px": 2.0 + (i % 4) * 0.25, "ref_px": None}
        for i, t in enumerate(_IDX)
    ]
    return {
        "inst_id": inst_id,
        "lot_coin": 0.1,
        "list_ms": int(_IDX[0].timestamp() * 1000),
        "rows": rows,
    }


def _ds(**kw):
    start = kw.pop("start_ts", int(datetime(2026, 9, 1, 4, tzinfo=timezone.utc).timestamp()))
    end = kw.pop("end_ts", int(datetime(2026, 9, 1, 23, tzinfo=timezone.utc).timestamp()))
    return OptionsReplayDataSource(
        family="SOL-USD_UM",
        timestep="15min",
        start_ts=start,
        end_ts=end,
        length=6,
        strike_pct=0.05,
        strike_step=1.0,
        fetcher=lambda inst, bar, s, e: _kline(),
        lifecycle_fetcher=_lifecycle,
        **kw,
    )


# ── 纯函数 ────────────────────────────────────────────────────────────


def test_to_ts_normalises_inputs():
    want = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    assert _to_ts(None) is None
    assert _to_ts(1000) == 1000
    assert _to_ts(datetime(2026, 9, 1, tzinfo=timezone.utc)) == want
    assert _to_ts("2026-09-01") == want


def test_resolve_bar_maps_lumibot_names():
    assert _resolve_bar("") == "15m"
    assert _resolve_bar("5min") == "5m"
    assert _resolve_bar("15min") == "15m"
    assert _resolve_bar("hour") == "1H"
    assert _resolve_bar("bar:1H") == "1H"


def test_resolve_bar_fail_closed_on_unknown():
    with pytest.raises(ValueError):
        _resolve_bar("17sec")


def test_resolve_bar_rejects_okx_unsupported_8h():
    with pytest.raises(ValueError):
        _resolve_bar("8H")


def test_fmt_strike_drops_trailing_zero():
    assert _fmt_strike(101.0) == "101"
    assert _fmt_strike(101.5) == "101.5"


def test_unknown_family_rejected():
    with pytest.raises(ValueError):
        OptionsReplayDataSource(family="DOGE-USD_UM")


# ── prefetch / 驱动辅助 ───────────────────────────────────────────────


def test_prefetch_builds_bar_times_and_start_idx():
    ds = _ds()
    ds.prefetch()
    # 预拉区间 = [start − (length−1)×bar, end]，经本地裁剪 → 根数 ≤ 原始
    assert 0 < len(ds.bar_times) <= len(_IDX)
    # start_idx = 第一个 ≥ 用户 start 的 bar（本例 start = 04:00）
    assert ds.bar_times[ds.start_idx].hour == 4
    assert any("合约 mark 预拉" in n for n in ds.notes)


def test_seek_and_price_of_track_replay_time():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    ds.seek(ts)
    # 假数据 close = 100 + 小时序号
    assert ds.price_of() == pytest.approx(100.0 + ts.hour)
    # 未 seek → fail-closed
    ds2 = _ds()
    ds2.prefetch()
    assert ds2.price_of() == 0.0


def test_contract_enumeration_uses_instid_rule():
    ds = _ds()
    ds.prefetch()
    insts = [c["inst_id"] for c in ds.contracts()]
    assert insts, "应枚举出候选合约"
    # 形如 SOL-USD_UM-260901-101-P
    for inst in insts[:5]:
        parts = inst.split("-")
        assert parts[0] == "SOL"
        assert parts[1] == "USD_UM"
        assert len(parts[2]) == 6 and parts[2].isdigit()
        assert parts[4] == "P"


def test_strike_band_respects_pct():
    ds = _ds()  # strike_pct=0.05，标的价 100~123
    ds.prefetch()
    strikes = {c["strike"] for c in ds.contracts()}
    assert min(strikes) >= 100.0 * 0.95
    assert max(strikes) <= 123.0 * 1.05


def test_chain_at_filters_live_contracts_only():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    ds.seek(ts)
    chain = ds.chain_at()
    assert chain, "该时刻应有在售合约"
    for c in chain:
        assert c["list_ms"] <= int(ts.timestamp() * 1000) < c["exp_ms"]
        assert c["mark_px"] is not None


def test_chain_at_before_list_or_after_expiry_is_empty():
    ds = _ds()
    ds.prefetch()
    # 区间起点前（无合约上市）
    ds.seek(_IDX[0] - pd.Timedelta(days=30))
    assert ds.chain_at() == []


def test_premium_of_falls_back_to_prior_bar():
    ds = _ds()
    ds.prefetch()
    inst = ds.contracts()[0]["inst_id"]
    # 取一个不落在 mark 时间戳上的中间时刻 → 应回退到之前最近一笔
    ts = _IDX[5] + pd.Timedelta(minutes=20)
    px = ds.premium_of(inst, ts)
    assert px is not None
    assert px == ds.premium_of(inst, _IDX[5])


def test_premium_of_unknown_contract_is_none():
    ds = _ds()
    ds.prefetch()
    assert ds.premium_of("SOL-USD_UM-999999-1-P") is None


def test_opt_type_filter_defaults_to_puts():
    ds = _ds()
    ds.prefetch()
    assert all(c["opt_type"] == "P" for c in ds.contracts())


def test_calls_can_be_enabled():
    ds = _ds(opt_types=("P", "C"))
    ds.prefetch()
    types = {c["opt_type"] for c in ds.contracts()}
    assert types == {"P", "C"}


# ── lumibot DataSource 契约 ───────────────────────────────────────────


def test_lumibot_interface_shape():
    ds = _ds()
    ds.prefetch()
    ds.seek(ds.bar_times[ds.start_idx])
    assert ds.get_timestep() == "15min"
    assert ds.get_datetime().tzinfo is not None
    assert ds.drops_in_progress_bars is False
    assert ds.SOURCE == "backtest-options"

    class _Asset:
        symbol = "SOL"

    bars = ds.get_historical_prices(_Asset(), 6)
    # 窗口 = [start_idx−5, start_idx]；start_idx=4 → 只有 5 根（数据不足是预期行为）
    assert len(bars.df) == ds.start_idx + 1
    assert ds.get_last_price(_Asset()) > 0


def test_get_historical_prices_returns_short_window_when_insufficient():
    ds = _ds()
    ds.prefetch()
    ds.seek(ds.bar_times[0])          # 第一根：窗口只有 1 根

    class _Asset:
        symbol = "SOL"

    bars = ds.get_historical_prices(_Asset(), 10)
    assert len(bars.df) == 1


def test_empty_underlying_degrades_gracefully():
    ds = OptionsReplayDataSource(
        family="SOL-USD_UM", timestep="15min", length=6,
        fetcher=lambda inst, bar, s, e: pd.DataFrame(),
        lifecycle_fetcher=_lifecycle,
    )
    ds.prefetch()
    assert ds.bar_times == []
    assert ds.contracts() == []
    assert ds.price_of() == 0.0
    # 空数据必须留痕（静默成功不可接受）
    assert any("标的 K 线为空" in n for n in ds.notes)


def test_fetcher_failure_is_recorded_not_raised():
    def boom(inst, bar, s, e):
        raise RuntimeError("network down")

    ds = OptionsReplayDataSource(
        family="SOL-USD_UM", timestep="15min", length=6,
        fetcher=boom, lifecycle_fetcher=_lifecycle,
    )
    ds.prefetch()
    assert any("K 线拉取失败" in n or "网络" in n for n in ds.notes)


# ── 自检探针（probe）───────────────────────────────────────────────


def test_probe_returns_full_shape(monkeypatch):
    """探针在真实拉数路径上不应抛异常，且必须报告两个假设的判定结果。"""
    import nanobot_quant.backtest.options_replay_data_source as m

    monkeypatch.setattr(
        m.OptionsReplayDataSource, "_default_fetch",
        lambda self, inst, bar, s, e: _kline(),
    )
    monkeypatch.setattr(m, "fetch_lifecycle", _lifecycle)

    res = m.probe("SOL-USD_UM", "15min", days=1, length=6, strike_pct=0.05,
                  end_ts=int(_IDX[-1].timestamp()))
    assert "error" not in res, res.get("error")
    assert set(res) >= {
        "family", "timestep", "days", "window", "hypotheses",
        "bar", "ref_inst", "underlying", "contracts", "notes", "elapsed_s",
    }
    # 两个待验证假设必须有明确布尔判定（不能静默缺失）
    assert "okx_history_candles" in res["hypotheses"]
    assert "daily_expiry_integer_strike" in res["hypotheses"]
    assert res["bar"] == "15m"
    assert res["ref_inst"] == "SOL-USDT"
    assert 0 < res["underlying"]["bars"] <= len(_IDX)
    assert res["contracts"]["enumerated"] > 0
    assert "hit_rate" in res["contracts"]
    assert res["elapsed_s"] >= 0


def test_probe_reports_error_instead_of_raising():
    """未知家族 → 错误进结果，不能把异常抛给调用方（诊断端点不 500）。"""
    import nanobot_quant.backtest.options_replay_data_source as m

    res = m.probe("DOGE-USD_UM", "15min", days=1, end_ts=int(_IDX[-1].timestamp()))
    assert "error" in res
    assert "DOGE" in res["error"]


def test_probe_records_fetch_failure_hypothesis_false(monkeypatch):
    """标的 K 线拉不到 → 假设判定必须为 False（而非静默 True）。"""
    import nanobot_quant.backtest.options_replay_data_source as m

    def boom(self, inst, bar, s, e):
        raise RuntimeError("network down")

    monkeypatch.setattr(m.OptionsReplayDataSource, "_default_fetch", boom)
    monkeypatch.setattr(m, "fetch_lifecycle", _lifecycle)

    res = m.probe("SOL-USD_UM", "15min", days=1, length=6,
                  end_ts=int(_IDX[-1].timestamp()))
    assert res["hypotheses"]["okx_history_candles"] is False
    assert res["underlying"]["bars"] == 0
    assert any("K 线拉取失败" in n for n in res["notes"])
