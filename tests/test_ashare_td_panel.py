"""``ashare.td_panel`` 单测（纯本地、不联网）。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from nanobot_quant.ashare import td_panel as tp


def _mk(dts, closes, opener=None):
    """构造腾讯同形的分钟线 DataFrame。"""
    n = len(closes)
    closes = list(closes)
    opens = opener or [closes[0]] + closes[:-1]
    df = pd.DataFrame({
        "dt": pd.to_datetime(dts),
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [1.0] * n,
    })
    df["date"] = df["dt"].dt.strftime("%Y%m%d")
    return df


# ── 解析 ─────────────────────────────────────────────────────

def test_fetch_klines_parses_tencent_rows(monkeypatch):
    payload = {"data": {"sh510050": {"m1": [
        ["202611251459", "2.870", "2.868", "2.872", "2.865", "100.0", {}, "0.1"],
        ["202611251500", "2.868", "2.857", "2.870", "2.857", "200.0", {}, "0.2"],
    ]}}}
    monkeypatch.setattr(tp, "_get_json", lambda *a, **k: payload)
    df = tp.fetch_klines("m1", n=2)
    assert len(df) == 2
    # 腾讯顺序是 开/收/高/低 —— close 取第 3 个字段
    assert df["close"].tolist() == [2.868, 2.857]
    assert df["open"].tolist() == [2.870, 2.868]
    assert df["high"].iloc[0] == 2.872
    assert df["low"].iloc[1] == 2.857
    assert df["date"].iloc[0] == "20261125"


def test_fetch_klines_empty_payload(monkeypatch):
    monkeypatch.setattr(tp, "_get_json", lambda *a, **k: {"data": {}})
    df = tp.fetch_klines("m1")
    assert df.empty


# ── 进行中 bar ───────────────────────────────────────────────

def test_drop_unclosed_keeps_only_closed(monkeypatch):
    dts = ["2026-11-25 09:30", "2026-11-25 09:31", "2026-11-25 09:32"]
    df = _mk(dts, [2.9, 2.9, 2.9])
    now = datetime(2026, 11, 25, 9, 32, 30)      # 09:32 bar（09:32–09:33）未收盘
    out = tp.drop_unclosed(df, 60, now)
    assert out["dt"].iloc[-1] == pd.Timestamp("2026-11-25 09:31")


def test_drop_unclosed_after_close_keeps_all():
    dts = ["2026-11-25 14:59", "2026-11-25 15:00"]
    df = _mk(dts, [2.87, 2.857])
    out = tp.drop_unclosed(df, 60, datetime(2026, 11, 25, 16, 10))
    assert len(out) == 2


# ── 日界切分 ─────────────────────────────────────────────────

def test_setup_resets_at_session_boundary():
    """跨交易日计数必须从 0 重数（隔夜不参与比较）。"""
    day1 = ["2026-11-24 14:56", "2026-11-24 14:57", "2026-11-24 14:58",
            "2026-11-24 14:59", "2026-11-24 15:00"]
    day2 = ["2026-11-25 09:30", "2026-11-25 09:31", "2026-11-25 09:32",
            "2026-11-25 09:33", "2026-11-25 09:34", "2026-11-25 09:35"]
    closes = [2.90, 2.89, 2.88, 2.87, 2.86,      # day1 连续下跌 → buy 累加
              2.85, 2.84, 2.83, 2.82, 2.81, 2.80]  # day2 继续下跌
    df = _mk(day1 + day2, closes)
    out = tp.setup_by_session(df, cmp_length=4)

    d1 = out[out["date"] == "20261124"]
    d2 = out[out["date"] == "20261125"]
    # day1：前 4 根为 0（无比较基准），第 5 根背靠背下跌 → buy=1
    assert d1["buy_setup_count"].tolist()[0] == 0
    assert int(d1["buy_setup_count"].iloc[-1]) == 1
    # day2 从 0 重数：前 4 根 0，第 5/6 根各 +1
    assert d2["buy_setup_count"].tolist()[:4] == [0, 0, 0, 0]
    assert int(d2["buy_setup_count"].iloc[-1]) == 2
    # 若未按日切分，day2 首根会接上 day1 的计数（=2），这里必须为 0
    assert int(d2["buy_setup_count"].iloc[0]) == 0


def test_setup_matches_strategy_engine_per_day():
    """同源保证：分组调策略引擎与包装函数结果一致。"""
    from nanobot_quant.strategies.td_sequential_futu import FutuDeMarkEngine

    dates = ["2026-11-24 09:30", "2026-11-24 09:31", "2026-11-24 09:32",
             "2026-11-24 09:33", "2026-11-24 09:34", "2026-11-24 09:35",
             "2026-11-25 09:30", "2026-11-25 09:31", "2026-11-25 09:32",
             "2026-11-25 09:33", "2026-11-25 09:34", "2026-11-25 09:35"]
    closes = [2.90, 2.89, 2.88, 2.87, 2.86, 2.85,
              2.84, 2.83, 2.82, 2.81, 2.80, 2.79]
    df = _mk(dates, closes)
    got = tp.setup_by_session(df, cmp_length=4)

    for date, g in df.groupby("date"):
        work = g.rename(columns={"open": "Open", "high": "High",
                                 "low": "Low", "close": "Close", "volume": "Volume"})
        ref = FutuDeMarkEngine(work, {"compare_length": 4}).calculate_setup()
        mine = got[got["date"] == date]
        assert mine["buy_setup_count"].tolist() == ref["buy_setup_count"].tolist()
        assert mine["sell_setup_count"].tolist() == ref["sell_setup_count"].tolist()


# ── 最近信号与描述块 ─────────────────────────────────────────

def test_last_signal_is_first_cross_only():
    """1→9 只算一次；9→10 不再算新信号。"""
    dates = [f"2026-11-25 10:{i:02d}" for i in range(14)]
    # 前 3 根横盘，之后连续下跌 → buy 从第 5 根开始累加
    closes = [2.90, 2.90, 2.90] + [2.90 - 0.01 * i for i in range(1, 12)]
    df = _mk(dates, closes)
    out = tp.setup_by_session(df, cmp_length=4)
    sig = tp._last_signal(out, "buy_setup_count")
    assert sig is not None
    # 最后一根的计数应 >= 9（连续下跌足够多），首穿时刻应早于末根
    assert out["buy_setup_count"].iloc[-1] >= 9
    assert sig["dt"] != out["dt"].iloc[-1].strftime("%m-%d %H:%M")


def test_describe_period_fields():
    dts = [f"2026-11-25 09:{30+i:02d}" for i in range(12)]
    closes = [2.90, 2.89, 2.88, 2.87, 2.86, 2.85, 2.84, 2.83, 2.82, 2.81, 2.80, 2.79]
    df = _mk(dts, closes)
    out = tp.setup_by_session(df, 4)
    # 收盘后看
    d = tp.describe_period(out, "1m", datetime(2026, 11, 25, 16, 0))
    assert d["ok"] is True
    assert d["last_close"] == 2.79
    assert d["session"] == "closed"
    assert d["remaining_bars"] == 0
    assert d["buy"]["count"] >= 1
    assert d["buy"]["need"] == max(0, 9 - d["buy"]["count"])


# ── 时段/剩余根数 ────────────────────────────────────────────

@pytest.mark.parametrize("hm,expect", [
    ((9, 0), "pre"), ((9, 45), "am"), ((11, 45), "noon"),
    ((14, 0), "pm"), ((15, 30), "closed"),
])
def test_session_state(hm, expect):
    now = datetime(2026, 11, 25, hm[0], hm[1])
    assert tp._session_state(now) == expect


@pytest.mark.parametrize("hm,label,expect", [
    ((9, 30), "1m", 240),      # 全天 240 分钟 → 240 根 1m（含跨午休）
    ((14, 55), "1m", 5),
    ((14, 55), "5m", 1),
    ((14, 55), "15m", 0),
    ((16, 0), "1m", 0),
])
def test_remaining_bars(hm, label, expect):
    now = datetime(2026, 11, 25, hm[0], hm[1])
    assert tp._remaining_bars(label, now) == expect


# ── 缓存 ─────────────────────────────────────────────────────

def test_panel_caches_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(bar="m1", n=640, symbol=tp.DEFAULT_SYMBOL):
        calls["n"] += 1
        dts = [f"2026-11-25 09:{30+i:02d}" for i in range(12)]
        return _mk(dts, [2.90] * 12)

    monkeypatch.setattr(tp, "fetch_klines", fake_fetch)
    tp.clear_cache()
    p1 = tp.panel()
    n_after_first = calls["n"]
    p2 = tp.panel()          # 命中缓存
    assert calls["n"] == n_after_first          # 没有再发请求
    assert p2["cached_s"] >= 0
    assert p1["ok"] is True
    p3 = tp.panel(force=True)                    # 强制刷新
    assert calls["n"] > n_after_first
    assert p3["ok"] is True
    tp.clear_cache()
