"""合成周期（2026-10-22）：准入规则 + 聚合正确性 + 回测接线。

背景：官方 API 不提供 2m/4m/6m/10m/20m/45m。这些档位**只做回测的可选档**，
由更粗的原生周期按 UTC 整除精确聚合（不整除一律不做——「3m+1m 拼 4m」
只在 1/3 的窗口成立，其余窗口必须退回最细源，深度退化且无收益）。

护栏：
- OHLC = 首/最大/最小/末、量求和；
- 内部缺口 fail-closed（不静默补齐，否则 high/low 偏小 → TD 计数错）；
- 边界不完整组丢弃（序列头从半根开始 / 末尾未收盘）；
- 合成档对 fetcher 调用方透明，但**底层必须按源周期取数**；
- 原生周期行为零变化。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from nanobot_quant.data_sources.aggregate import aggregate_bars
from nanobot_quant.data_sources.periods import (
    COMPOSITE_PERIODS,
    INTERVAL_SECONDS,
    PERIODS,
    backtest_periods,
    composite_factor,
    composite_source,
    is_composite_period,
    period_label,
)


BASE = 1_759_996_800  # 对齐到整小时（同时是 120s / 900s 的整数倍），避免首组边界干扰


def _bars(n: int, start_ts: int = BASE) -> pd.DataFrame:
    """构造连续 n 根 1m bar（价格可预测，便于断言聚合结果）。"""
    idx = pd.to_datetime(
        start_ts + np.arange(n, dtype="int64") * 60, unit="s", utc=True
    ).tz_localize(None)
    return pd.DataFrame(
        {
            "Open": np.arange(n, dtype=float),
            "High": np.arange(n, dtype=float) + 0.5,
            "Low": np.arange(n, dtype=float) - 0.5,
            "Close": np.arange(n, dtype=float) + 0.25,
            "Volume": np.ones(n),
        },
        index=idx,
    )


# ── 注册表 / 准入规则 ────────────────────────────────────────────────────
def test_registry_is_exact_multiples_of_native_source():
    assert set(COMPOSITE_PERIODS) == {"2m", "4m", "6m", "10m", "20m", "45m"}
    for period, (source, factor) in COMPOSITE_PERIODS.items():
        assert source in PERIODS, f"{period} 的源 {source} 必须是原生周期"
        assert period not in PERIODS, f"{period} 不应混进原生 PERIODS"
        assert INTERVAL_SECONDS[period] == INTERVAL_SECONDS[source] * factor
        assert is_composite_period(period)
        assert composite_source(period) == source
        assert composite_factor(period) == factor


def test_native_periods_untouched():
    for p in PERIODS:
        assert not is_composite_period(p)
        assert composite_source(p) is None
        assert composite_factor(p) is None
        assert period_label(p) == p
    assert "4m" not in PERIODS and "2m" not in PERIODS


def test_backtest_periods_native_first_then_composite():
    got = backtest_periods()
    assert got[: len(PERIODS)] == PERIODS
    assert got[len(PERIODS):] == tuple(COMPOSITE_PERIODS)


def test_period_label_marks_source():
    assert period_label("4m") == "4m（1m×4 合成）"
    assert period_label("6m") == "6m（3m×2 合成）"
    assert period_label("15m") == "15m"


# ── 聚合正确性 ──────────────────────────────────────────────────────────
def test_aggregate_ohlcv_first_max_min_last_sum():
    df = _bars(8)
    out = aggregate_bars(df, period="2m", source="1m", factor=2)

    assert len(out) == 4
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # 第 1 组 = 前两根 1m
    assert out["Open"].iloc[0] == pytest.approx(0.0)      # 首根 open
    assert out["High"].iloc[0] == pytest.approx(1.5)      # max(0.5, 1.5)
    assert out["Low"].iloc[0] == pytest.approx(-0.5)      # min(-0.5, 0.5)
    assert out["Close"].iloc[0] == pytest.approx(1.25)    # 末根 close
    assert out["Volume"].iloc[0] == pytest.approx(2.0)
    # 索引对齐到 UTC 整除的目标格（输出统一 tz-aware UTC，与原生档同形）
    assert out.index[0] == pd.Timestamp(df.index[0], tz="UTC")
    assert (out.index[1] - out.index[0]) == pd.Timedelta(minutes=2)


def test_aggregate_alignment_follows_utc_grid_not_first_bar():
    """起点不在目标格上时：分组按 UTC 整除，首组为边界残组（丢弃）。"""
    df = _bars(8, start_ts=BASE + 60)  # 偏移 1 分钟，不在 2m 格上
    out = aggregate_bars(df, period="2m", source="1m", factor=2)
    # 首组只有 1 根（边界）→ 丢弃；其余 7 根里能成 3 组完整 2m
    assert len(out) == 3
    for ts in out.index:
        assert int(ts.timestamp()) % 120 == 0


def test_aggregate_tail_partial_dropped():
    df = _bars(7)  # 7 根 → 3 组完整 + 1 根尾巴
    out = aggregate_bars(df, period="2m", source="1m", factor=2)
    assert len(out) == 3


def test_aggregate_interior_gap_fail_closed():
    df = _bars(8)
    df = df.drop(df.index[3])  # 删掉中间一根 → 第 2 组缺 bar
    with pytest.raises(ValueError) as exc:
        aggregate_bars(df, period="2m", source="1m", factor=2)
    msg = str(exc.value)
    assert "聚合缺口" in msg and "应 2 根" in msg


def test_aggregate_rejects_non_divisible_factor():
    df = _bars(8)
    with pytest.raises(ValueError) as exc:
        aggregate_bars(df, period="4m", source="3m", factor=2)  # 4m≠3m×2
    assert "不整除" in str(exc.value)


def test_aggregate_factor_lt_two_rejected():
    with pytest.raises(ValueError):
        aggregate_bars(_bars(4), period="2m", source="1m", factor=1)


def test_aggregate_empty_returns_empty_frame():
    out = aggregate_bars(
        pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]),
        period="4m", source="1m", factor=4,
    )
    assert len(out) == 0
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_aggregate_handles_lowercase_columns_and_tz_aware_index():
    df = _bars(4).rename(columns=str.lower)
    df.index = df.index.tz_localize("UTC")
    out = aggregate_bars(df, period="2m", source="1m", factor=2)
    assert len(out) == 2
    # 统一输出 tz-aware UTC（对齐原生 fetcher：Gate/OKX 返回的都是 UTC 感知索引）
    assert str(out.index.tz) == "UTC"
    assert out["Volume"].iloc[0] == pytest.approx(2.0)


# ── fetcher 接线：合成档对上层透明，但底层必须按源周期取 ─────────────────
def test_fetcher_routes_composite_to_source_period():
    from nanobot_quant.backtest import fetchers

    calls: list[tuple] = []

    def fake_fetch(pair, start_ts, end_ts, bar="1D"):
        calls.append((pair, start_ts, end_ts, bar))
        return _bars(8, start_ts=int(end_ts) - 8 * 60)

    wrapped = fetchers._composite_fetch(fake_fetch)
    out = wrapped("SOL_USDT", 1_760_000_000, 1_760_000_000 + 8 * 60, "4m")

    assert calls and calls[0][3] == "1m"          # 底层按源周期取数
    assert calls[0][1] % 240 == 0                  # 起点对齐到目标格
    assert list(out.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(out) <= 2                            # 8 根 1m → 2 根 4m


def test_fetcher_native_passthrough_unchanged():
    from nanobot_quant.backtest import fetchers

    calls: list[tuple] = []

    def fake_fetch(pair, start_ts, end_ts, bar="1D"):
        calls.append((pair, start_ts, end_ts, bar))
        return _bars(5)

    wrapped = fetchers._composite_fetch(fake_fetch)
    out = wrapped("SOL_USDT", 1_760_000_000, 1_760_000_300, "5m")
    assert calls == [("SOL_USDT", 1_760_000_000, 1_760_000_300, "5m")]
    assert len(out) == 5


# ── 驱动层：合成周期可被回测接受，非法周期仍 fail-closed ─────────────────
def test_driver_accepts_composite_periods():
    from nanobot_quant.backtest.driver import _timestep_for

    for p in COMPOSITE_PERIODS:
        assert _timestep_for(p) == p
    assert _timestep_for("15m") == "15m"


def test_driver_rejects_unknown_period():
    from nanobot_quant.backtest.driver import _timestep_for

    with pytest.raises(ValueError) as exc:
        _timestep_for("7m")
    assert "合成" in str(exc.value)  # 报错里明确列出合成档，便于自查


# ── 数据源层：合成档可被 ReplayDataSource 解析并落成聚合后的 bar ────────
def test_resolve_bar_accepts_composite():
    """回归（2026-10-22 quant-demo 实测）：多周期回测 2m/4m 报“不支持的回测周期”。

    入参校验放行后还有一层：``ReplayDataSource._resolve_bar`` 只认原生映射，
    合成档直接 fail-closed —— 于是“能选、能提交、回放阶段才错”。
    """
    from nanobot_quant.backtest.replay_data_source import _resolve_bar

    assert _resolve_bar("1m") == "1m"
    assert _resolve_bar("4H") == "4H"
    assert _resolve_bar("bar:4m") == "4m"
    for p in COMPOSITE_PERIODS:
        assert _resolve_bar(p) == p
        assert _resolve_bar(p.lower()) == p
    with pytest.raises(ValueError) as exc:
        _resolve_bar("7m")
    # 错误提示必须列出合成档，否则用户看不出 2m/4m 可用
    assert "4m" in str(exc.value) and "45m" in str(exc.value)


def test_replay_data_source_aggregates_composite_bars():
    """4m 回测：数据源按 1m 取数、按 4m 回放（索引步长 240s、根数减为 1/4）。"""
    from nanobot_quant.backtest.fetchers import _composite_fetch
    from nanobot_quant.backtest.replay_data_source import ReplayDataSource

    calls: list[str] = []

    def fake_fetch(pair, start_ts, end_ts, bar="1D"):
        calls.append(bar)
        return _bars(240)  # 240 根 1m = 60 根 4m

    ds = ReplayDataSource(
        symbols=["SOL"],
        timestep="4m",
        start_ts=BASE + 120 * 60,
        end_ts=BASE + 240 * 60,
        length=2,
        fetcher=_composite_fetch(fake_fetch),
        resolver=lambda sym: "SOL_USDT",
    )
    ds.prefetch()
    assert calls == ["1m"], "合成档必须按源周期取数"
    idx = pd.DatetimeIndex(ds.bar_times)
    assert len(idx) == 60
    assert (idx.to_series().diff().dropna() == pd.Timedelta(minutes=4)).all()
    # 聚合后仍是 tz-aware UTC（与原生档同形，下游 Bars 构造不变）
    assert str(idx.tz) == "UTC"


# ── 页面注入 ────────────────────────────────────────────────────────────
def test_page_injects_composite_periods():
    from nanobot_quant import backtest_handlers as bh

    bh._PAGE_HTML = bh._load_template("backtest_page.html")
    html = bh._render_page({"high": {}}, ["SOL"])
    assert "__COMPOSITE_PERIODS__" not in html
    for p in COMPOSITE_PERIODS:
        assert f'"{p}"' in html
    assert "合成周期（原生聚合）" in html
