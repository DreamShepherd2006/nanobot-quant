"""okx_options_td（C24 ⑤ 标的 TD 状态面板）单元测试。

面板是只读展示，但有两处必须锁住：
1. 阈值与自动循环同源（读 option_params.json 的 live.strategy）——否则页面
   会与策略轮次各说各话；
2. 信号判定口径 = setup_buy >= entry_setup 或 cd_buy >= entry_countdown，
   与 okx_options_strategy.evaluate_entry 一致。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant import okx_options_td as otd


def _seq(**kw) -> pd.DataFrame:
    row = {
        "buy_setup_count": 7, "sell_setup_count": 0,
        "buy_countdown_count": 3, "sell_countdown_count": 0,
        "combined_score": 18.4, "Close": 100.0,
    }
    row.update(kw)
    return pd.DataFrame([row], index=pd.to_datetime(["2026-09-15 14:00"]))


@pytest.fixture(autouse=True)
def _clear_cache():
    otd._cache.clear()
    yield
    otd._cache.clear()


def test_base_of():
    assert otd.base_of("SOL-USD_UM") == "SOL"
    assert otd.base_of("BTC-USD_UM") == "BTC"


def test_thresholds_falls_back_when_config_broken(monkeypatch):
    """配置读不出来时回落默认值，面板不空白、不抛异常。"""
    def boom():
        raise RuntimeError("config gone")

    monkeypatch.setattr("nanobot_quant.okx_options_live.live_config", boom)
    thr = otd.thresholds()
    assert thr["entry_setup"] == 9
    assert thr["entry_countdown"] == 13
    assert "阈值读取失败" in thr.get("note", "")


def test_compute_marks_ready_on_setup_buy(monkeypatch):
    monkeypatch.setattr(otd, "_fetch_cex_kline", lambda *a, **k: _seq(buy_setup_count=9))
    monkeypatch.setattr(otd, "_engine_run", lambda *a, **k: _seq(buy_setup_count=9))
    thr = {"entry_setup": 9, "entry_countdown": 13}
    row = otd._compute("SOL-USD_UM", "5m", 120, thr)
    assert row["sell_put_ready"] is True
    assert row["near"] is False
    assert row["progress"] == "买9 9/9 · CD 3/13"
    assert row["base"] == "SOL"


def test_compute_marks_ready_on_cd_buy(monkeypatch):
    """cd_buy 独立触发（与执行层 cd_buy>=entry_countdown 一致）。"""
    seq = _seq(buy_setup_count=2, buy_countdown_count=13)
    monkeypatch.setattr(otd, "_fetch_cex_kline", lambda *a, **k: _seq())
    monkeypatch.setattr(otd, "_engine_run", lambda *a, **k: seq)
    row = otd._compute("SOL-USD_UM", "5m", 120, {"entry_setup": 9, "entry_countdown": 13})
    assert row["sell_put_ready"] is True


def test_compute_near_flag(monkeypatch):
    """差 ≤2 记「临近」（橙行），未到阈值不算 ready。"""
    seq = _seq(buy_setup_count=7)
    monkeypatch.setattr(otd, "_fetch_cex_kline", lambda *a, **k: _seq())
    monkeypatch.setattr(otd, "_engine_run", lambda *a, **k: seq)
    row = otd._compute("SOL-USD_UM", "5m", 120, {"entry_setup": 9, "entry_countdown": 13})
    assert row["sell_put_ready"] is False
    assert row["near"] is True


def test_compute_not_near_when_far(monkeypatch):
    seq = _seq(buy_setup_count=3, buy_countdown_count=1)
    monkeypatch.setattr(otd, "_fetch_cex_kline", lambda *a, **k: _seq())
    monkeypatch.setattr(otd, "_engine_run", lambda *a, **k: seq)
    row = otd._compute("SOL-USD_UM", "5m", 120, {"entry_setup": 9, "entry_countdown": 13})
    assert row["near"] is False


def test_compute_reports_kline_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gate down")

    monkeypatch.setattr(otd, "_fetch_cex_kline", boom)
    row = otd._compute("SOL-USD_UM", "5m", 120, {"entry_setup": 9, "entry_countdown": 13})
    assert "K 线获取失败" in row["error"]
    assert "sell_put_ready" not in row


def test_compute_reports_empty_kline(monkeypatch):
    monkeypatch.setattr(otd, "_fetch_cex_kline", lambda *a, **k: pd.DataFrame())
    row = otd._compute("SOL-USD_UM", "5m", 120, {"entry_setup": 9, "entry_countdown": 13})
    assert row["error"] == "无 K 线数据"


def test_family_td_caches(monkeypatch):
    calls = {"n": 0}

    def fake_compute(family, period, bars, thr):
        calls["n"] += 1
        return {"family": family, "base": family.split("-")[0]}

    monkeypatch.setattr(otd, "_compute", fake_compute)
    monkeypatch.setattr(otd, "thresholds", lambda: {
        "entry_setup": 9, "entry_countdown": 13, "period": "5m", "bars": 120})
    a = otd.family_td("SOL-USD_UM")
    b = otd.family_td("SOL-USD_UM")
    assert a == b and calls["n"] == 1


def test_family_td_rejects_unknown_period(monkeypatch):
    seen = {}

    def fake_compute(family, period, bars, thr):
        seen["period"] = period
        return {"family": family}

    monkeypatch.setattr(otd, "_compute", fake_compute)
    monkeypatch.setattr(otd, "thresholds", lambda: {
        "entry_setup": 9, "entry_countdown": 13, "period": "5m", "bars": 120})
    otd.family_td("SOL-USD_UM", period="8H")   # 非法周期 → 回默认
    assert seen["period"] == otd.DEFAULT_PERIOD


def test_panel_returns_all_families(monkeypatch):
    monkeypatch.setattr(otd, "family_td", lambda f, p=None, b=None: {"family": f})
    data = otd.panel("5m")
    assert [r["family"] for r in data["rows"]] == list(otd.FAMILIES)
    assert data["periods"] == list(otd.PERIODS)
