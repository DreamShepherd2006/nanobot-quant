"""模型 B 前向记录器单测（只读、append-only、冻结口径）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from nanobot_quant.ashare import modelb_forward as mfw


# ── fixtures ────────────────────────────────────────────────

@pytest.fixture()
def root(tmp_path, monkeypatch):
    """把研究目录 / 参数文件指到临时目录，绝不碰真实 /data。"""
    res = tmp_path / "research"
    monkeypatch.setattr(mfw, "research_dir", lambda: res)
    monkeypatch.setattr(mfw, "params_path", lambda: tmp_path / "params.json")
    return tmp_path


def _minute_df(closes, lows=None, day="20261128", start="0930"):
    dt0 = datetime.strptime(day + start, "%Y%m%d%H%M")
    rows = []
    for i, c in enumerate(closes):
        lo = lows[i] if lows else c - 0.01
        rows.append({"dt": dt0 + timedelta(minutes=i), "close": c, "low": lo,
                     "date": day})
    return pd.DataFrame(rows)


def _bars(n, start=10.0, step=0.01, ts0=20260101093000, span=1500):
    """n 根 15m bar：ts 按 HHMMSS 递增（span 由调用方给正确偏移）。"""
    out = []
    c = start
    for i in range(n):
        o, c = c, c + step
        out.append((ts0 + i * span, o, max(o, c), min(o, c) - 0.005, c))
    return out


# ── 冻结口径：15m 聚合 ─────────────────────────────────────

def test_to_15m_matches_frozen_rule():
    closes = [10 + i * 0.01 for i in range(30)]
    lows = [c - 0.02 - (0.01 if i == 7 else 0) for i, c in enumerate(closes)]
    df = _minute_df(closes, lows)
    bars = mfw.to_15m(df)
    assert len(bars) == 2                       # 30 根 1m → 2 根 15m
    ts, o, h, l, c = bars[0]
    assert ts == 20261128093000                 # 14 位（含秒），state_at 依赖这一点
    assert o == closes[0]                       # o = 首根 close（C176 口径）
    assert h == max(closes[:15])                # h = max(close)，不是 max(high)
    assert l == min(lows[:15])
    assert c == closes[14]
    assert bars[1][0] == 20261128094500


def test_to_15m_drops_partial_chunk_and_resets_per_day():
    day1 = _minute_df([10.0] * 30, day="20261127")
    day2 = _minute_df([20.0] * 10, day="20261128")
    bars = mfw.to_15m(pd.concat([day1, day2], ignore_index=True))
    assert [b[0] for b in bars] == [20261127093000, 20261127094500]  # 第二天的 10 根不足 15 丢弃
    assert bars[0][1] == 10.0 and bars[1][1] == 10.0


# ── 冻结口径：ADX 滞回 + EMA 斜率 ──────────────────────────

def test_state_series_warmup_and_hysteresis():
    up = _bars(140, start=10.0, step=0.05)          # 强趋势：ADX 高、斜率正
    st = mfw.state_series(up)
    assert all(s is None for _, s in st[:mfw.MIN_BARS])   # 前 60 根预热
    assert st[-1][1] == "U"

    down = _bars(140, start=10.0, step=-0.05)
    assert mfw.state_series(down)[-1][1] == "D"

    # 强趋势后接窄幅震荡 → ADX 衰减到 <=18 → 出趋势 → R（滞回出线）
    osc = []
    c = 10.0
    for i in range(240):
        c += 0.002 if i % 2 == 0 else -0.002
        osc.append((20260101093000 + i * 1500, c, c + 0.01, c - 0.01, c))
    mixed = up + osc[0:0] + [(t, o, h, l, cc) for (t, o, h, l, cc) in osc]
    st2 = mfw.state_series(mixed)
    assert st2[len(up) - 1][1] == "U"
    assert st2[-1][1] == "R"


def test_state_at_uses_only_completed_bars():
    states = [(20261128093000, "R"), (20261128143000, "U"), (20261128144500, "D")]
    dts = 20261128144500                        # 14:45 决策
    assert mfw.state_at(states, dts) == "U"     # 14:45 bar 尚未完成，不得使用
    assert mfw.state_at(states, 20261128150000) == "D"


# ── 记录：每日一次、append-only ─────────────────────────────

def test_record_once_per_day_and_append_only(root, monkeypatch):
    monkeypatch.setattr(mfw, "update_bars", lambda *a, **k: {"error": "", "source": "t"})
    monkeypatch.setattr(mfw, "_chain_rows", lambda *a, **k: {
        "rows": [{"code": "10012345.SHO", "right": "P", "strike": 4.0, "expiry": "20261223",
                  "dte": 25, "bid": 0.05, "ask": 0.051, "mid": 0.0505, "iv": 0.4,
                  "delta": -0.251}],
        "spot": 4.1, "error": ""})
    now = datetime(2026, 11, 28, 14, 50)

    r1 = mfw.record_observations(["510300.SH"], now=now)
    assert len(r1["states"]) == 1
    state_bytes = mfw.state_path().read_bytes()

    r2 = mfw.record_observations(["510300.SH"], now=now)
    assert r2["states"] == [] and r2["skipped"] == ["510300.SH:recorded"]
    assert mfw.state_path().read_bytes() == state_bytes      # 不重写历史

    srows = mfw.load_states()
    assert len(srows) == 1 and srows[0]["observation_id"] == "510300.SH-20261128"
    assert srows[0]["rule_version"] == mfw.RULE_VERSION
    orows = mfw.load_options()
    assert len(orows) == 1
    con = orows[0]["contract"]
    assert con["code"] == "10012345.SHO" and con["delta"] == -0.251
    assert orows[0]["in_dte_window"] is True and orows[0]["delta_in_band"] is True


def test_record_marks_warmup_state_explicitly(root, monkeypatch):
    monkeypatch.setattr(mfw, "update_bars", lambda *a, **k: {"error": "", "source": ""})
    monkeypatch.setattr(mfw, "_chain_rows", lambda *a, **k: {"rows": [], "spot": 4.0,
                                                            "error": ""})
    mfw.record_observations(["510300.SH"], now=datetime(2026, 11, 28, 14, 50))
    row = mfw.load_states()[0]
    assert row["state"] is None and "预热不足" in row["note"]
    assert mfw.load_options()[0]["reason"] == "no_put_quote"


def test_pick_target_put_prefers_nearest_delta():
    rows = [{"right": "P", "delta": -0.10}, {"right": "P", "delta": -0.26},
            {"right": "C", "delta": 0.25}, {"right": "P", "delta": None}]
    assert mfw.pick_target_put(rows, 0.25)["delta"] == -0.26


# ── 结算：只追加、每 id 一次 ────────────────────────────────

def _seed_one(root, pnl_case="normal"):
    mfw._append(mfw.state_path(), {
        "v": 1, "rule_version": mfw.RULE_VERSION, "observation_id": "510300.SH-20261103",
        "symbol": "510300.SH", "spot": 4.0, "state": "U", "recorded_at": "x"})
    mfw._append(mfw.option_path(), {
        "v": 1, "rule_version": mfw.RULE_VERSION, "observation_id": "510300.SH-20261103",
        "symbol": "510300.SH", "contract": {
            "code": "10012345.SHO", "strike": 4.0, "expiry": "20261223", "dte": 25,
            "bid": 0.05, "ask": 0.051, "mult": 10000}})


def test_settle_computes_pnl_once(root, monkeypatch):
    _seed_one(root)
    days = [("20261103", 4.0, 4.0, 4.0, 3.9), ("20261104", 4.0, 4.02, 4.03, 3.98),
            ("20261105", 4.02, 4.05, 4.06, 4.01), ("20261106", 4.05, 4.06, 4.07, 4.04),
            ("20261109", 4.06, 4.07, 4.08, 4.05), ("20261110", 4.07, 4.08, 4.09, 3.95)]
    monkeypatch.setattr(mfw, "_daily_bars", lambda *a, **k: days)
    monkeypatch.setattr(mfw, "_tape_quotes_for", lambda *a, **k: {
        "bid": 0.02, "ask": 0.03, "quote_ts": "2026-11-10 14:45", "age_min": 0.0,
        "day": "20261110"})

    res = mfw.settle_outcomes(now=datetime(2026, 11, 28, 9, 0))
    assert len(res["settled"]) == 1
    row = mfw.load_outcomes()[0]
    assert row["exit_day"] == "20261110" and row["horizon_days"] == 5
    assert row["etf"]["ret"] == pytest.approx(4.08 / 4.0 - 1, abs=1e-6)
    assert row["etf"]["mae"] == pytest.approx(3.95 / 4.0 - 1, abs=1e-6)
    assert row["etf"]["strike_touch"] is True
    assert row["put"]["exit_ask"] == 0.03
    assert row["put"]["pnl_yuan"] == pytest.approx((0.05 - 0.03) * 10000 - 2.9, abs=0.01)
    assert row["put"]["pnl_pct_collateral"] == pytest.approx(
        ((0.05 - 0.03) * 10000 - 2.9) / 40000, abs=1e-6)

    again = mfw.settle_outcomes(now=datetime(2026, 11, 28, 9, 0))
    assert again["settled"] == [] and again["skipped"] == 1
    assert len(mfw.load_outcomes()) == 1


def test_settle_waits_when_horizon_not_reached(root, monkeypatch):
    _seed_one(root)
    days = [("20261103", 4.0, 4.0, 4.0, 3.9), ("20261104", 4.0, 4.02, 4.03, 3.98)]
    monkeypatch.setattr(mfw, "_daily_bars", lambda *a, **k: days)
    res = mfw.settle_outcomes(now=datetime(2026, 11, 28, 9, 0))
    assert res["settled"] == [] and res["waiting"] == 1
    assert not mfw.outcome_path().exists()


def test_settle_records_missing_exit_quote_once(root, monkeypatch):
    _seed_one(root)
    days = [("20261103", 4.0, 4.0, 4.0, 3.9), ("20261104", 4.0, 4.02, 4.03, 3.98),
            ("20261105", 4.02, 4.05, 4.06, 4.01), ("20261106", 4.05, 4.06, 4.07, 4.04),
            ("20261109", 4.06, 4.07, 4.08, 4.05), ("20261110", 4.07, 4.08, 4.09, 3.95)]
    monkeypatch.setattr(mfw, "_daily_bars", lambda *a, **k: days)
    monkeypatch.setattr(mfw, "_tape_quotes_for", lambda *a, **k: None)
    mfw.settle_outcomes(now=datetime(2026, 11, 28, 9, 0))
    row = mfw.load_outcomes()[0]
    assert row["missing"] == ["no_exit_quote"] and row["put"]["pnl_yuan"] is None
    mfw.settle_outcomes(now=datetime(2026, 11, 28, 9, 0))
    assert len(mfw.load_outcomes()) == 1


# ── 配置 ────────────────────────────────────────────────────

def test_config_clamps_and_filters(root):
    cfg = mfw.save_forward_config(interval_s=5, decide_hm=9999, delta_target=5,
                                  underlyings=["510300.SH", "999999.SH"])
    assert cfg["interval_s"] == mfw.INTERVAL_RANGE[0]
    assert cfg["decide_hm"] == 2359
    assert cfg["delta_target"] == 0.60
    assert cfg["underlyings"] == ["510300.SH"]


def test_files_summary(root):
    _seed_one(root)
    s = mfw.files_summary()
    assert s["states"]["rows"] == 1 and s["options"]["rows"] == 1
    assert s["rule_version"] == mfw.RULE_VERSION
    assert s["states"]["last"] == "510300.SH-20261103"


# ── 只读边界（结构性） ──────────────────────────────────────

def test_module_has_no_trading_path():
    src = Path(mfw.__file__).read_text(encoding="utf-8")
    for token in ("submit_order", "passorder", "open_lot(", "close_lot(",
                  "BatchManager", "paper_ledger", "order_schema"):
        assert token not in src, token


# ── 路由接线（记录器随网关自启 + 诊断端点） ────────────────

def test_routes_registered_and_sync_called(root, monkeypatch):
    from fastapi import FastAPI

    from nanobot_quant import ashare_handlers as h

    called = {"tape": 0, "fwd": 0}
    monkeypatch.setattr(h.tape, "sync", lambda: called.__setitem__("tape", 1))
    monkeypatch.setattr(mfw, "sync", lambda: called.__setitem__("fwd", 1))

    class _Platform:
        def is_commander(self, user):
            return True

    class _Gatekeeper:
        _platform = _Platform()

    app = FastAPI()
    h.register_ashare_routes(app, _Gatekeeper())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/config/ashare/modelb-forward/state" in paths
    assert "/config/ashare/modelb-forward/run" in paths
    assert called == {"tape": 1, "fwd": 1}
