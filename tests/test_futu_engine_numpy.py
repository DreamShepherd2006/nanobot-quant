"""FutuDeMarkEngine numpy 化：逐位 parity + 性能基准 + 边界场景。

背景：2026-11-04 把富途引擎里的 pandas 逐格访问去掉（``df.at[]`` 写 /
``df.iloc[]`` 标量读 / ``df.iloc[i][col]`` 整行 Series 构造 → numpy 数组下标），
并跳过富途用不到的布林带计算。**算法与语义不得有任何变化**，本文件的 golden
fixture 由改前实现生成，任何差异都视为回归。

fixture: ``tests/fixtures/futu_engine_parity.json``（800 根真实 ZEC 1m K 线，
覆盖 setup 累加（max=19）、交替重置、11 次 BUY / 5 次 SELL 精确触发）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from nanobot_quant.strategies import td_sequential_futu as F
from nanobot_quant.strategies.td_sequential import calculate_series

FIXTURE = Path(__file__).parent / "fixtures" / "futu_engine_parity.json"


def _to_df(bars: list[list]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": pd.to_datetime(b[0], unit="ms", utc=True),
                "Open": b[1],
                "High": b[2],
                "Low": b[3],
                "Close": b[4],
                "Volume": b[5],
            }
            for b in bars
        ]
    ).set_index("ts")


def _close_df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1min", tz="UTC")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"Open": c, "High": c, "Low": c, "Close": c, "Volume": np.ones(len(c))}, index=idx
    )


# ── golden fixture parity ────────────────────────────────────────────────


def test_parity_with_golden_fixture():
    """改前实现生成的全列输出必须逐位复现（buy/sell 计数 + 推荐）。"""
    fx = json.loads(FIXTURE.read_text())
    df = _to_df(fx["bars"])
    out = calculate_series(df, engine_cls=F.FutuDeMarkEngine)
    exp = fx["expected"]

    assert out["buy_setup_count"].astype(int).tolist() == exp["buy_setup_count"]
    assert out["sell_setup_count"].astype(int).tolist() == exp["sell_setup_count"]
    assert out["recommendation"].tolist() == exp["recommendation"]


def test_summary_contract_unchanged():
    """``calculate()`` 的 summary 字段口径不变（score 为 setup 进度、rvol 中性）。"""
    fx = json.loads(FIXTURE.read_text())
    df = _to_df(fx["bars"])
    s = F.calculate(df)
    for k in ("setup_buy", "setup_sell", "cd_buy", "cd_sell", "recommendation", "price"):
        assert k in s
    # 富途无 countdown
    assert s["cd_buy"] == 0 and s["cd_sell"] == 0
    assert 0.0 <= s["score"] <= 1.0


# ── 语义边界（不依赖 fixture，构造式）──────────────────────────────────────


def test_monotonic_drop_accumulates_without_cap():
    """连续下跌 setup 无上限累加（富途口径：9 后继续 10/11/…）。"""
    closes = [1000.0 - i for i in range(40)]
    out = calculate_series(_close_df(closes), engine_cls=F.FutuDeMarkEngine)
    buy = out["buy_setup_count"].tolist()
    assert max(buy) > 9, "长跌势必须能累加超过 setup 周期"
    assert max(buy) == 40 - 4, f"累加值应等于连续满足根数，实际 {max(buy)}"


def test_monotonic_rise_only_sell_setup():
    closes = [1000.0 + i for i in range(40)]
    out = calculate_series(_close_df(closes), engine_cls=F.FutuDeMarkEngine)
    assert max(out["buy_setup_count"].tolist()) == 0
    assert max(out["sell_setup_count"].tolist()) == 40 - 4


def test_flat_prices_reset_to_zero():
    """相等价不满足 < / > ⇒ 计数归零（equality 语义不能因 numpy 化而改变）。"""
    out = calculate_series(_close_df([100.0] * 30), engine_cls=F.FutuDeMarkEngine)
    assert max(out["buy_setup_count"].tolist()) == 0
    assert max(out["sell_setup_count"].tolist()) == 0


def test_signal_fires_only_on_exact_period():
    """信号只在计数恰等于 setup_period 的 bar 触发一次，累加期不复触发。"""
    closes = [1000.0 - i for i in range(30)]
    out = calculate_series(_close_df(closes), engine_cls=F.FutuDeMarkEngine)
    recs = out["recommendation"].tolist()
    buy = out["buy_setup_count"].tolist()
    assert recs.count("BUY (Setup Complete)") == 1
    fire_at = recs.index("BUY (Setup Complete)")
    assert buy[fire_at] == 9  # 默认 setup_period


def test_short_and_empty_df_do_not_crash():
    """引擎层：长度不足 / 空表不得抛异常（回测预热段会喂短窗口）。

    注：这里只测引擎（``run_all``）；``_base_calculate`` 对空表仍取 ``iloc[-1]``
    会抛 IndexError，属改前既有行为、不属本次重构范围。
    """
    for n in (0, 1, 3, 4):
        out = F.FutuDeMarkEngine(_close_df([100.0 + i for i in range(n)])).run_all(0)
        assert len(out) == n
        if n:
            assert set(out["buy_setup_count"].unique()) <= {0}


def test_sell_priority_when_both_hit():
    """原实现是 ``if s_9 / elif b_9`` ⇒ SELL 优先；向量化写入顺序必须复刻。"""
    df = _close_df([100.0] * 12)
    df["buy_setup_count"] = [0] * 11 + [9]
    df["sell_setup_count"] = [0] * 11 + [9]
    engine = F.FutuDeMarkEngine(df)
    out = engine.calculate_recommendations()
    assert out["recommendation"].iloc[-1] == "SELL (Setup Complete)"


# ── 性能基准（防回退）───────────────────────────────────────────────────


def test_benchmark_300_bars_under_10ms():
    """单次 calculate(300 根) 必须 < 10 ms。

    改前 52.8 ms / 改后约 2 ms；阈值留足 CI 慢机余量，但足以拦住「有人把
    逐格访问加回来」这类回退（一旦回到 50 ms 量级，回测 5 标的又要 7 分钟）。
    """
    fx = json.loads(FIXTURE.read_text())
    df = _to_df(fx["bars"]).iloc[-300:]

    F.calculate(df)  # 预热（首次调用含 pandas 列缓存 / import 惰性开销）

    runs = 10
    t0 = time.perf_counter()
    for _ in range(runs):
        F.calculate(df)
    per_call_ms = (time.perf_counter() - t0) / runs * 1000

    assert per_call_ms < 10.0, f"calculate(300 根) 耗时 {per_call_ms:.1f} ms，超过 10 ms 阈值"


def test_bollinger_skipped_for_futu():
    """富途不计算布林带——bb_upper/bb_lower 不应出现（无人消费，白算 35%）。"""
    fx = json.loads(FIXTURE.read_text())
    df = _to_df(fx["bars"])
    out = calculate_series(df, engine_cls=F.FutuDeMarkEngine)
    assert "bb_upper" not in out.columns
    assert "bb_lower" not in out.columns
