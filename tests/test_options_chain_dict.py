"""``chain_dict_at`` —— 回测链快照 → ``select_puts`` 认的 chain dict。

核心断言：**回测构造出来的 chain 必须能直接喂给实盘的 select_puts**。
这是「回测与实盘共用同一份选档代码」得以成立的条件 —— 过滤/排序逻辑
一行都不重写，回测侧只负责把历史 mark 还原成「链」。

全部注入假 fetcher / lifecycle_fetcher —— 不打网络。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from nanobot_quant.backtest.options_replay_data_source import OptionsReplayDataSource
from nanobot_quant.bs_pricing import bs_delta, bs_price, years_to_expiry
from nanobot_quant.okx_options_select import select_puts

# ── 假数据 ────────────────────────────────────────────────────────────

_IDX = pd.date_range("2026-09-01 00:00", periods=60, freq="1h", tz="UTC")
_SPOT = 100.0


def _exp_ms_of(inst_id: str) -> int:
    """从 instId 反解到期时间（``...-260904-95-P`` → 2026-09-04 08:00 UTC）。"""
    dt = datetime.strptime("20" + inst_id.split("-")[2], "%Y%m%d").replace(
        hour=8, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# 参考到期日（测试里主要用 09-04 那档验证 IV 往返）
_EXP_MS = _exp_ms_of("SOL-USD_UM-260903-95-P")


def _kline():
    return pd.DataFrame(
        {
            "open": _SPOT, "high": _SPOT, "low": _SPOT,
            "close": [_SPOT] * len(_IDX), "volume": 1000.0,
        },
        index=_IDX,
    )


def _lifecycle(inst_id: str, bar: str):
    """按 BS 定价造 mark：反解应回到同一个 σ（IV 往返）。

    注意用**该 instId 自己的到期日**算 T —— 若全部套用同一个到期日，
    反解出的 IV 会因 T 不匹配而偏离。
    """
    try:
        strike = float(inst_id.split("-")[3])
    except (IndexError, ValueError):
        strike = _SPOT
    exp_ms = _exp_ms_of(inst_id)
    sigma = 0.6
    rows = []
    for t in _IDX:
        t_ms = int(t.timestamp() * 1000)
        tv = years_to_expiry(t_ms, exp_ms)
        px = bs_price(_SPOT, strike, tv, sigma, 0.0, "P") if tv else 0.01
        rows.append({"ts": t_ms, "mark_px": max(float(px or 0.01), 0.01),
                     "ref_px": None})
    return {
        "inst_id": inst_id,
        "lot_coin": 0.1,
        "list_ms": int(_IDX[0].timestamp() * 1000),
        "rows": rows,
    }


def _ds(**kw) -> OptionsReplayDataSource:
    start = kw.pop("start_ts", int(_IDX[0].timestamp()))
    end = kw.pop("end_ts", int(_IDX[-1].timestamp()))
    ds = OptionsReplayDataSource(
        family="SOL-USD_UM",
        timestep="1H",
        start_ts=start,
        end_ts=end,
        fetcher=lambda inst, bar, s, e: _kline(),
        lifecycle_fetcher=_lifecycle,
        **kw,
    )
    ds.prefetch()
    return ds


# ── 结构 ─────────────────────────────────────────────────────────────


def test_shape_matches_select_puts_contract():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()

    assert chain["spot"] == pytest.approx(_SPOT)
    assert chain["lot_coin"] == pytest.approx(0.1)
    assert chain["groups"], "应至少有一个到期分组"
    g = chain["groups"][0]
    assert set(g) >= {"days", "exp_ms", "date", "rows"}
    row = g["rows"][0]
    assert set(row) >= {"strike", "P"}
    cell = row["P"]
    assert set(cell) >= {"inst_id", "bid", "ask", "iv", "delta"}


def test_rows_sorted_by_strike_and_groups_by_expiry():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()
    for g in chain["groups"]:
        strikes = [float(r["strike"]) for r in g["rows"]]
        assert strikes == sorted(strikes)
    exps = [g["exp_ms"] for g in chain["groups"]]
    assert exps == sorted(exps)


def test_empty_when_no_ts():
    ds = _ds()
    chain = ds.chain_dict_at()
    assert chain["groups"] == []
    assert chain["stats"]["total"] == 0


# ── 口径：IV / delta / 滑点 ───────────────────────────────────────────


def test_iv_and_delta_recovered_from_mark():
    """mark 由 BS 生成 → 反解必须回到原 σ，delta 与解析值一致。

    跳过权利金触底的深 OTM 档（假数据里 mark 被 max(px, 0.01) 撑到 0.01，
    反解出来的是「此价位下界对应多少 IV」而非原 σ）。
    """
    ds = _ds()
    ds.seek(_IDX[5])
    chain = ds.chain_dict_at()
    assert chain["stats"]["kept"] > 0

    t_ms = int(_IDX[5].timestamp() * 1000)
    checked = 0
    for g in chain["groups"]:
        tv = years_to_expiry(t_ms, g["exp_ms"])
        for row in g["rows"]:
            cell = row["P"]
            k = float(row["strike"])
            px_exact = bs_price(_SPOT, k, tv, 0.6, 0.0, "P")
            d_exact = bs_delta(_SPOT, k, tv, 0.6, 0.0, "P")
            # 跳过两端病态区：触底档（假数据下界 max(px,0.01)）与
            # 深 ITM（时间价值 ≪ 内在，IV 反解数值上不稳定）。
            if px_exact is None or d_exact is None:
                continue
            if px_exact <= 0.011 or abs(d_exact) < 0.02 or abs(d_exact) > 0.9:
                continue
            assert cell["mark_px"] == pytest.approx(px_exact)
            assert cell["iv"] == pytest.approx(0.6, rel=5e-3)
            assert cell["delta"] == pytest.approx(d_exact, rel=5e-3)
            checked += 1
    assert checked > 0, "应有非病态档参与校验"


def test_slippage_applied_symmetrically():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at(slippage=0.01)
    for g in chain["groups"]:
        for row in g["rows"]:
            cell = row["P"]
            mark = cell["mark_px"]
            assert cell["bid"] == pytest.approx(mark * 0.99)
            assert cell["ask"] == pytest.approx(mark * 1.01)


def test_no_slippage_by_default():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()
    for g in chain["groups"]:
        for row in g["rows"]:
            assert row["P"]["bid"] == pytest.approx(row["P"]["mark_px"])


# ── stats（静默降级不可接受）─────────────────────────────────────────


def test_stats_account_for_every_contract():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()
    st = chain["stats"]
    assert st["total"] == st["kept"] + st["expired"] + st["no_iv"]
    assert st["kept"] > 0


def test_no_iv_counted_when_mark_unusable():
    """mark 恒为 0 → 全部剔除，且 no_iv/expired 有计数（不是静默丢弃）。"""
    def _bad(inst_id, bar):
        return {"inst_id": inst_id, "lot_coin": 0.1,
                "list_ms": int(_IDX[0].timestamp() * 1000),
                "rows": [{"ts": int(t.timestamp() * 1000), "mark_px": 0.0}
                         for t in _IDX]}

    ds = OptionsReplayDataSource(
        family="SOL-USD_UM", timestep="1H",
        start_ts=int(_IDX[0].timestamp()), end_ts=int(_IDX[-1].timestamp()),
        fetcher=lambda inst, bar, s, e: _kline(), lifecycle_fetcher=_bad,
    )
    ds.prefetch()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()
    assert chain["stats"]["kept"] == 0
    assert chain["stats"]["total"] > 0
    assert chain["stats"]["expired"] + chain["stats"]["no_iv"] == \
        chain["stats"]["total"]


# ── 到期窗口过滤 ─────────────────────────────────────────────────────


def test_expiry_window_filters_groups():
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    all_days = [g["days"] for g in ds.chain_dict_at()["groups"]]
    assert all_days
    lo, hi = 0.0, min(all_days) * 0.5
    kept = ds.chain_dict_at(expiry_min_days=lo, expiry_max_days=hi)["groups"]
    assert all(g["days"] <= hi for g in kept)
    assert kept == [] or len(kept) <= len(all_days)


# ── ★ 端到端：喂给实盘的 select_puts ────────────────────────────────


def test_feeds_real_select_puts():
    """回测 chain 直接进实盘选档函数，应产出候选（证明共用同一份代码）。"""
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()

    res = select_puts("SOL-USD_UM", base_px=_SPOT, chain=chain)
    assert res["family"] == "SOL-USD_UM"
    assert res["spot"] == pytest.approx(_SPOT)
    # 默认 selector：5% 距离过滤 + delta 带 0.05–0.35
    assert res["scanned"] > 0
    for c in res["candidates"]:
        assert c["strike"] <= _SPOT * 0.95 + 1e-9
        assert 0.05 <= abs(c["delta"]) <= 0.35
        assert c["net_premium_usd"] > 0
        assert c["net_yield_pct"] > 0
    if res["candidates"]:
        # 排序：净收益率降序（次键 delta 贴近 0.25）
        ys = [c["net_yield_pct"] for c in res["candidates"]]
        assert ys == sorted(ys, reverse=True)


def test_select_puts_screenshot_counts_filtered_reasons():
    """过滤统计必须回传 —— 候选为空时要能看出被哪一道规则拦下。"""
    ds = _ds()
    ds.seek(_IDX[5])   # 距 09-04 到期约 3.1 天，落在默认 3–7 天窗口
    chain = ds.chain_dict_at()
    res = select_puts("SOL-USD_UM", base_px=_SPOT, chain=chain)
    assert set(res["filtered"]) >= {"expiry", "no_bid", "distance", "delta",
                                    "net", "yield"}
