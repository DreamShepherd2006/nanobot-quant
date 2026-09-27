"""期权回测数据层（OptionsReplayDataSource）单测。

架构（2026-09-17 起）：

* 合约列表 = **官方合约表**（在售 ∪ 已到期，``okx_options_data.family_contracts``）
* 价格     = **归档成交反解 IV → 微笑插值 → BS 重定价**

旧路线（自己枚举 instId + 逐合约拉 mark 历史）已被实测证伪：已到期合约的实时
端点一律 51001，枚举 400 个只拿到 14 个 mark；且归档成交太稀疏，「有成交的
合约」当链会经常是空的。

全部注入假合约表 / 假归档成交 / 假 K 线 —— **不打网络**，确定性。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from nanobot_quant import okx_options_data as od
from nanobot_quant.backtest.options_replay_data_source import (
    OptionsReplayDataSource,
    _deltas_monotonic,
    _fmt_strike,
    _resolve_bar,
    _to_ts,
)
from nanobot_quant.bs_pricing import bs_price

# ── 假数据 ────────────────────────────────────────────────────────────

_EXP0 = datetime(2026, 9, 3, 8, tzinfo=timezone.utc)
_SIGMA = 0.6                                    # 生成成交价用的波动率
_IDX = pd.date_range("2026-09-01 00:00", periods=24, freq="1h", tz="UTC")
_SPOT = [100.0 + i * 0.5 for i in range(len(_IDX))]
_TRADED = (90.0, 94.0, 96.0, 98.0, 100.0)       # 有成交的档
_ALL = (88.0, 90.0, 92.0, 94.0, 95.0, 96.0, 98.0, 100.0, 104.0)   # 合约表全集


def _inst_id(strike: float, right: str = "P", exp: datetime = _EXP0) -> str:
    return f"SOL-USD_UM-{exp.strftime('%y%m%d')}-{_fmt_strike(strike)}-{right}"


def _kline() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [s - 1.0 for s in _SPOT],
            "high": [s + 1.0 for s in _SPOT],
            "low": [s - 2.0 for s in _SPOT],
            "close": _SPOT,
            "volume": 1000.0,
        },
        index=_IDX,
    )


def _trades(start_ms: int, end_ms: int) -> list[dict]:
    """按 BS 正算生成成交（σ=_SIGMA）—— 反解应能拿回同一个 σ。"""
    out: list[dict] = []
    for i, t in enumerate(_IDX):
        ms = int(t.timestamp() * 1000)
        if not (start_ms <= ms <= end_ms):
            continue
        t_y = (_EXP0.timestamp() * 1000 - ms) / 1000 / 86_400 / 365
        for k in _TRADED:
            px = bs_price(_SPOT[i], k, t_y, _SIGMA, right="P")
            if px > 0:
                out.append(
                    {"instrument_name": _inst_id(k), "created_time": ms,
                     "price": px}
                )
    return out


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """合约表走假数据 —— 单测绝不打网络。"""
    monkeypatch.setattr(
        od, "family_contracts", lambda family: [_inst_id(k) for k in _ALL]
    )


def _ds(**kw):
    start = kw.pop(
        "start_ts", int(datetime(2026, 9, 1, 4, tzinfo=timezone.utc).timestamp())
    )
    end = kw.pop(
        "end_ts", int(datetime(2026, 9, 1, 23, tzinfo=timezone.utc).timestamp())
    )
    fetcher = kw.pop("fetcher", None) or (lambda inst, bar, s, e: _kline())
    archive = kw.pop("archive_fetcher", None) or _trades
    return OptionsReplayDataSource(
        family="SOL-USD_UM",
        timestep="15min",
        start_ts=start,
        end_ts=end,
        length=6,
        fetcher=fetcher,
        archive_fetcher=archive,
        **kw,
    )


# ── 纯函数 ────────────────────────────────────────────────────────────


def test_to_ts_normalises_inputs():
    want = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    assert _to_ts(None) is None
    assert _to_ts(1000) == 1000
    assert _to_ts(datetime(2026, 9, 1, tzinfo=timezone.utc)) == want
    assert _to_ts("2026-09-01") == want


def test_resolve_bar_accepts_both_styles():
    assert _resolve_bar("15min") == "15m"
    assert _resolve_bar("hour") == "1H"
    assert _resolve_bar("1H") == "1H"


def test_fmt_strike_drops_trailing_zero():
    assert _fmt_strike(95.0) == "95"
    assert _fmt_strike(95.5) == "95.5"


def test_deltas_monotonic_put_direction():
    """put delta 随 strike 上升而递减（负值越来越负）才算单调。"""
    assert _deltas_monotonic([(90.0, -0.05), (95.0, -0.12), (100.0, -0.30)]) is True
    assert _deltas_monotonic([(90.0, -0.30), (95.0, -0.12), (100.0, -0.05)]) is False
    assert _deltas_monotonic([(90.0, -0.10)]) is None


# ── 预取 / 重放 ───────────────────────────────────────────────────────


def test_prefetch_builds_bar_times_and_start_idx():
    ds = _ds()
    ds.prefetch()
    assert 0 < len(ds.bar_times) <= len(_IDX)
    assert ds.bar_times[ds.start_idx].hour == 4
    assert any("合约表" in n for n in ds.notes)
    assert any("IV 曲面" in n for n in ds.notes)


def test_seek_and_price_of_track_replay_time():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    ds.seek(ts)
    assert ds.price_of() > 0


def test_contract_table_covers_untraded_strikes():
    """合约表是全量（在售 ∪ 已到期），不只「有成交的合约」。

    ``contracts()`` 只列**有 IV 观测点**的档（供选档用）；未成交档靠同到期微笑
    插值补上，它们出现在 ``chain_at`` 的链里 —— 这正是新版相对「拿成交当链」
    的关键改进（否则策略要的窗口交集经常为空）。
    """
    ds = _ds()
    ds.prefetch()
    assert len(ds._contracts) == len(_ALL), ds._contracts.keys()
    chain = ds.chain_at(ds.bar_times[ds.start_idx])
    strikes = {c["inst_id"] for c in chain}
    assert _inst_id(95.0) in strikes, "未成交但同到期微笑可插值的档应在链上"
    assert len(strikes) > len(_TRADED)


def test_chain_at_only_returns_live_contracts():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    chain = ds.chain_at(ts)
    assert chain, "该时刻应能算出价格"
    assert all(c["exp_ms"] > int(ts.timestamp() * 1000) for c in chain)
    assert all(c["opt_type"] == "P" for c in chain)


def test_chain_at_empty_after_expiry():
    ds = _ds()
    ds.prefetch()
    after = _EXP0.replace(year=2026, month=9, day=10)
    assert ds.chain_at(after) == []


def test_premium_of_unknown_contract_is_none():
    ds = _ds()
    ds.prefetch()
    assert ds.premium_of("SOL-USD_UM-260903-9999-P") is None


def test_premium_of_recovers_bs_sigma():
    """用 σ=0.6 造的成交价，重定价出来的 IV 应回到 0.6 附近。"""
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    px = ds.premium_of(_inst_id(94.0), ts)
    assert px is not None and px > 0
    cd = ds.chain_dict_at(ts, opt_type="P")
    ivs = [r["P"]["iv"] for r in cd["groups"][0]["rows"] if r["P"]["iv"]]
    assert ivs and all(0.4 < v < 0.8 for v in ivs), ivs


# ── chain_dict_at（选档桥接） ─────────────────────────────────────────


def test_chain_dict_at_shape():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    cd = ds.chain_dict_at(ts, opt_type="P")
    assert cd["spot"] > 0
    assert cd["lot_coin"] == pytest.approx(0.1)
    assert cd["groups"]
    for g in cd["groups"]:
        assert g["rows"] and g["days"] > 0
        for r in g["rows"]:
            p = r["P"]
            assert p["bid"] >= 0 and p["ask"] > 0
            if p["bid"] > 0:
                assert p["ask"] > p["bid"]
            assert p["delta"] is not None and -1.0 < p["delta"] < 0.0


def test_chain_dict_at_extra_slippage_widens_spread():
    """额外滑点（dsigma/tick 置 0 隔离）在价差模型之上再扩一层。"""
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    tight = ds.chain_dict_at(ts, opt_type="P", dsigma_pts=0.0, tick=0.0)
    wide = ds.chain_dict_at(ts, opt_type="P", dsigma_pts=0.0, tick=0.0,
                            slippage=0.01)
    a = tight["groups"][0]["rows"][0]["P"]
    b = wide["groups"][0]["rows"][0]["P"]
    assert b["bid"] < a["bid"]
    assert b["ask"] > a["ask"]


def test_ask_at_matches_chain_ask_and_honors_overrides():
    """出场买回价与入场链快照同源：同一套 Δσ + tick 口径（单一实现）。"""
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    row = ds.chain_dict_at(ts, opt_type="P")["groups"][0]["rows"][0]["P"]
    inst = row["inst_id"]
    assert ds.ask_at(inst, ts) == pytest.approx(row["ask"], rel=1e-9)
    # 覆盖参数生效：Δσ=0/tick=0 → 退回中价 mark
    assert ds.ask_at(inst, ts, dsigma_pts=0.0, tick=0.0) == pytest.approx(
        row["mark_px"], rel=1e-9)
    # 额外滑点叠加在 ask 之上（置零 Δσ/tick 以免被 tick 网格吞掉）
    assert ds.ask_at(inst, ts, extra_slip=0.01, dsigma_pts=0.0, tick=0.0) == pytest.approx(
        row["mark_px"] * 1.01, rel=1e-9)


def test_chain_dict_at_expiry_window_filters_groups():
    ds = _ds()
    ds.prefetch()
    ts = ds.bar_times[ds.start_idx]
    allg = ds.chain_dict_at(ts, opt_type="P")
    kept = ds.chain_dict_at(ts, opt_type="P", expiry_max_days=1.0)
    assert len(kept["groups"]) < len(allg["groups"])


def test_chain_dict_at_empty_when_no_spot():
    ds = _ds(fetcher=lambda inst, bar, s, e: pd.DataFrame())
    ds.prefetch()
    cd = ds.chain_dict_at(ds.bar_times[ds.start_idx] if ds.bar_times else None)
    assert cd["groups"] == []


# ── 失败留痕（不静默降级） ────────────────────────────────────────────


def test_archive_failure_is_recorded_not_raised():
    def boom(a, b):
        raise RuntimeError("归档挂了")

    ds = _ds(archive_fetcher=boom)
    ds.prefetch()                      # 不抛
    assert any("归档成交拉取失败" in n for n in ds.notes), ds.notes


def test_empty_archive_keeps_contract_table():
    ds = _ds(archive_fetcher=lambda a, b: [])
    ds.prefetch()
    assert any("归档成交为空" in n for n in ds.notes), ds.notes


def test_contract_table_failure_is_recorded(monkeypatch):
    def boom(family):
        raise RuntimeError("合约表挂了")

    monkeypatch.setattr(od, "family_contracts", boom)
    ds = _ds()
    ds.prefetch()
    assert any("合约表" in n and "失败" in n for n in ds.notes), ds.notes


# ── lumibot DataSource 契约 ───────────────────────────────────────────


def test_get_historical_prices_returns_bars():
    ds = _ds()
    ds.prefetch()
    ds.seek(ds.bar_times[ds.start_idx])
    bars = ds.get_historical_prices("SOL-USDT", 6, timestep="bar:15m")
    assert bars is not None
    assert len(bars.df) > 0


def test_get_historical_prices_insufficient_window_is_short():
    ds = _ds()
    ds.prefetch()
    ds.seek(ds.bar_times[0])
    bars = ds.get_historical_prices("SOL-USDT", 999, timestep="bar:15m")
    assert bars is not None and len(bars.df) <= len(ds.bar_times)


def test_lumibot_interface_shape():
    ds = _ds()
    ds.prefetch()
    ds.seek(ds.bar_times[ds.start_idx])
    assert isinstance(ds.get_datetime(), datetime)
    assert isinstance(ds.get_timestamp(), (int, float))
    assert ds.get_timestep() == "15min"
