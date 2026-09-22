"""``analysis/iv_leadlag``（IV 领先-滞后诊断）单元测试。

**全部离线**：归档下载、K 线、DVOL 均由桩替换——被测的是聚合口径与统计口径本身。
真实数据的端到端跑法见模块 docstring（CLI 自测入口）。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from nanobot_quant.analysis import iv_leadlag as il
from nanobot_quant.iv_surface import IVPoint

TS0 = int(datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


# ────────────────────────── 分辨率选择（踩过的坑） ──────────────────────────

def test_dvol_resolution_avoids_truncation():
    """14 天窗口必须用小时级——请求 60s 会被端点截断到最近 ~1000 点。"""
    day = 86_400_000
    assert il.dvol_resolution(14 * day) == 3600
    assert il.dvol_resolution(30 * day) == 3600
    assert il.dvol_resolution(90 * day) == 43200
    assert il.dvol_resolution(400 * day) == 43200        # 400d/12h = 800 点，仍在上限内
    assert il.dvol_resolution(2000 * day) == 86400
    assert il.dvol_resolution(3_600_000) == 60     # 1 小时窗口 60s 即足，1s 会超点上限
    assert il.dvol_resolution(120_000) == 1
    # 无论多长窗口，返回值必须落在端点允许集合内
    assert il.dvol_resolution(9999 * day) in il.DVOL_RESOLUTIONS


def test_bucket_seconds_rejects_unknown():
    assert il.bucket_seconds("5m") == 300
    with pytest.raises(ValueError):
        il.bucket_seconds("7m")


# ────────────────────────── 序列构造 ──────────────────────────

def test_realized_vol_matches_manual_formula():
    rng = np.random.default_rng(0)
    close = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.001, 500))))
    rv = il.realized_vol(close, window=12, bucket="5m")
    manual = (np.log(close).diff().rolling(12).std()
              * math.sqrt(365.0 * 24.0 * 3600.0 / 300.0))
    pd.testing.assert_series_equal(rv.dropna(), manual.dropna())
    assert rv.dropna().median() > 0


def test_spot_lookup_is_step_function_never_extrapolates():
    idx = pd.to_datetime([TS0, TS0 + 300_000, TS0 + 600_000], unit="ms", utc=True)
    kl = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
    at = il.spot_lookup(kl)
    assert at(TS0 - 1) is None            # 早于首根 → 绝不外推
    assert at(TS0) == 10.0
    assert at(TS0 + 299_999) == 10.0      # 区间内取最近前值
    assert at(TS0 + 300_000) == 11.0
    assert at(TS0 + 10 ** 8) == 12.0      # 晚于末根 → 取最后一根


# ────────────────────────── 领先-滞后口径 ──────────────────────────

def test_leadlag_sign_convention_left_leads():
    """a[t] = b[t+3] ⇒ a 领先 b 3 根 ⇒ 峰值落在 lag=+3。

    注意 ``Series.shift(-k)`` 才是「把未来值拉到当前」，写成 ``shift(+3)`` 会得到相反结论。
    """
    rng = np.random.default_rng(1)
    b = pd.Series(np.cumsum(rng.normal(0, 1, 600)))
    a = b.shift(-3)                       # a 领先 b
    tbl = il.leadlag_table(a, b, max_lag=6).dropna(subset=["corr"])
    best = tbl.loc[tbl["corr"].abs().idxmax()]
    assert int(best["lag"]) == 3
    assert abs(float(best["corr"])) > 0.9


def test_leadlag_sign_convention_right_leads():
    """对称性：b 领先 a 3 根 ⇒ 峰值落在 lag=−3（符号不可写反）。"""
    rng = np.random.default_rng(2)
    a = pd.Series(np.cumsum(rng.normal(0, 1, 600)))
    b = a.shift(-3)                       # b 领先 a
    tbl = il.leadlag_table(a, b, max_lag=6).dropna(subset=["corr"])
    best = tbl.loc[tbl["corr"].abs().idxmax()]
    assert int(best["lag"]) == -3


def test_shift_test_significant_for_real_lead_random_for_noise():
    rng = np.random.default_rng(3)
    b = pd.Series(np.cumsum(rng.normal(0, 1, 800)))
    a = b.shift(-2)                       # a 领先 b 2 根
    real = il.shift_test(a, b, max_lag=5, n_iter=200)
    assert real["best_lag"] == 2
    assert real["p_value"] < 0.05

    x = pd.Series(np.cumsum(rng.normal(0, 1, 800)))
    y = pd.Series(np.cumsum(rng.normal(0, 1, 800)))
    noise = il.shift_test(x, y, max_lag=5, n_iter=200)
    assert noise["p_value"] > 0.05
    assert noise["n"] == len(x) - 1        # diff 掉一行


# ────────────────────────── IV 桶聚合（atm_median） ──────────────────────────

def _fake_env(monkeypatch, pts: list[IVPoint], spot: float = 100.0):
    monkeypatch.setattr(il.oh, "fetch_range",
                        lambda *a, **k: ["fake.zip"])
    monkeypatch.setattr(il.oh, "iter_trades",
                        lambda paths, family=None: [
                            {"instrument_name": "SOL-USD_UM-260926-100-P"}])
    monkeypatch.setattr(il, "iv_points_from_trades",
                        lambda trades, **kw: list(pts))
    idx = pd.to_datetime([TS0 - 300_000, TS0, TS0 + 300_000], unit="ms", utc=True)
    return pd.DataFrame({"close": [spot, spot, spot]}, index=idx)


def _pt(iv: float, *, strike: float, ts_ms: int = TS0,
        tenor_days: float = 3.0, spot: float = 100.0) -> IVPoint:
    return IVPoint(ts_ms=ts_ms, inst_id="SOL-USD_UM-260926-100-P", strike=strike,
                   exp_ms=ts_ms + int(tenor_days * 86_400_000), right="P",
                   iv=iv, price=1.0, spot=spot)


def test_atm_median_takes_median_of_near_atm_only(monkeypatch):
    kl = _fake_env(monkeypatch, [
        _pt(0.50, strike=100.0),      # 平值
        _pt(0.70, strike=105.0),      # 5% OTM → 带内
        _pt(0.90, strike=130.0),      # 30% OTM → 带外，必须剔除
    ])
    out = il.atm_iv_series("SOL-USD_UM", begin_ms=TS0 - 60_000, end_ms=TS0 + 600_000,
                           bucket="5m", spot_df=kl, band_pct=10.0)
    frame = out["frame"]
    assert not frame.empty
    assert frame["iv_atm"].iloc[0] == pytest.approx(0.60)   # median(0.5, 0.7)
    assert int(frame["n_points"].iloc[0]) == 2               # 130 已按带宽剔除
    assert int(frame["n_strikes"].iloc[0]) == 2
    assert out["trades"] == 1


def test_atm_median_drops_out_of_tenor_points(monkeypatch):
    kl = _fake_env(monkeypatch, [
        _pt(0.50, strike=100.0, tenor_days=3.0),     # 窗口内
        _pt(0.60, strike=100.0, tenor_days=0.1),     # 0.1 天 → 太近，剔除
        _pt(0.99, strike=100.0, tenor_days=30.0),    # 30 天 → 太远，剔除
    ])
    out = il.atm_iv_series("SOL-USD_UM", begin_ms=TS0 - 60_000, end_ms=TS0 + 600_000,
                           bucket="5m", spot_df=kl)
    frame = out["frame"]
    assert int(frame["n_points"].iloc[0]) == 1
    assert frame["iv_atm"].iloc[0] == pytest.approx(0.50)


def test_atm_median_fills_gaps_and_reports_fill_ratio(monkeypatch):
    """无成交的桶按前值填充——填充比例必须显式记录（它人为抬高平滑度）。"""
    pts = [_pt(0.50, strike=100.0, ts_ms=TS0),
           _pt(0.80, strike=100.0, ts_ms=TS0 + 900_000)]       # 隔两个桶
    kl = _fake_env(monkeypatch, pts)
    out = il.atm_iv_series("SOL-USD_UM", begin_ms=TS0 - 60_000,
                           end_ms=TS0 + 1_200_000, bucket="5m", spot_df=kl)
    frame = out["frame"]
    assert len(frame) == 4                                     # 网格补全
    assert frame["iv_atm"].iloc[1] == pytest.approx(0.50)      # 前向填充
    assert frame["iv_atm"].iloc[2] == pytest.approx(0.50)
    assert frame["iv_atm"].iloc[3] == pytest.approx(0.80)
    assert any("前向填充" in n for n in out["notes"])


def test_no_trades_yields_empty_frame_with_note(monkeypatch):
    monkeypatch.setattr(il.oh, "fetch_range", lambda *a, **k: [])
    monkeypatch.setattr(il.oh, "iter_trades", lambda paths, family=None: [])
    out = il.atm_iv_series("SOL-USD_UM", begin_ms=TS0, end_ms=TS0 + 60_000,
                           bucket="5m")
    assert out["frame"].empty
    assert any("成交 0 笔" in n for n in out["notes"])


def test_smile_mode_requires_min_strikes(monkeypatch):
    """``smile`` 口径的守卫：只有 1 个 strike 时不建微笑（覆盖率换口径干净）。"""
    group = [_pt(0.5, strike=100.0)]
    assert il._pick_by_smile(group, TS0, 3.0, 2) is None


# ────────────────────────── 报告 ──────────────────────────

def test_markdown_reports_limitations_and_verdicts():
    rng = np.random.default_rng(4)
    n = 400
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    b = pd.Series(np.cumsum(rng.normal(0, 1, n)), index=idx)
    a = b.shift(-2)
    result = {
        "ok": True, "family": "SOL-USD_UM", "bucket": "5m", "window_bars": 12,
        "days": 14,
        "span": {"begin": "2026-09-01 00:00", "end": "2026-09-15 00:00"},
        "series": {"iv_buckets": n, "mode": "atm_median", "band_pct": 10.0,
                   "iv_filled_pct": 92.0, "iv_first": 0.51, "iv_last": 0.48,
                   "iv_median": 0.52, "rv_median": 0.6, "f1_median": 1.1,
                   "target_tenor_days": 3.0},
        "tests": {"realized_vs_iv": il.shift_test(a, b, max_lag=4, n_iter=100),
                  "f1_vs_iv": il.shift_test(a, b, max_lag=4, n_iter=100),
                  "realized_vs_f1": il.shift_test(a, b, max_lag=4, n_iter=100)},
        "dvol": {"currency": "BTC", "points": 300,
                 "read": "lag>0 = 已实现波动领先 DVOL（crypto 型）；lag<0 = DVOL 领先（SPX 型）",
                 "test": il.shift_test(a, b, max_lag=4, n_iter=100)},
        "notes": [], "trades": 100, "rejects": {},
    }
    md = il.markdown(result)
    assert "IV 领先-滞后诊断" in md
    assert "有成交桶占比 8.0%" in md
    assert "外部对照：DVOL" in md
    assert "已实现波动领先 DVOL" in md
    assert "摆动" not in md


def test_markdown_handles_failure_result():
    md = il.markdown({"ok": False, "family": "SOL-USD_UM", "error": "无成交"})
    assert "无成交" in md


# ──────────────────── DVOL 币种推导（2026-09-22 复测缺口）────────────────────


def test_dvol_currency_for_uses_official_index_when_available():
    """有官方指数的家族用自己的指数（不需要标注借用）。"""
    assert il.dvol_currency_for("BTC-USD_UM") == ("BTC", None)
    assert il.dvol_currency_for("ETH-USD_UM") == ("ETH", None)


def test_dvol_currency_for_falls_back_to_btc_with_marker():
    """无官方指数的家族只能借 BTC——但必须带回借用标记，否则会被误读成本标的的对照。"""
    assert il.dvol_currency_for("SOL-USD_UM") == ("BTC", "SOL")
    assert il.dvol_currency_for("FOO-USD_UM") == ("BTC", "FOO")
    assert il.dvol_currency_for("") == ("BTC", None)


def _fake_analyze_env(monkeypatch, seen: dict, periods: int = 96, step_ms: int = 900_000,
              start: int = TS0) -> None:
    """把 analyze 的四条外部依赖全部换成合成数据（零网络）。"""
    idx = pd.to_datetime([start + i * step_ms for i in range(periods)],
                         unit="ms", utc=True)
    close = pd.Series(np.linspace(100.0, 110.0, periods), index=idx)
    kl = pd.DataFrame({"open": close, "high": close * 1.002,
                       "low": close * 0.998, "close": close, "volume": 1.0})
    ivf = pd.DataFrame({"iv_atm": np.linspace(0.50, 0.62, periods),
                        "n_points": 3}, index=idx)

    def fake_shift(a, b, **kw):
        return {"table": [{"lag": 0, "corr": 0.0, "n": int(len(a))}],
                "best_lag": 0, "best_corr": 0.0, "p_value": 1.0,
                "points": int(len(a))}

    def fake_dvol(ccy, **kw):
        seen["ccy"] = ccy
        return pd.DataFrame({"dvol": np.linspace(0.40, 0.45, periods)}, index=idx)

    monkeypatch.setattr(il, "spot_klines", lambda *a, **k: kl.copy())
    monkeypatch.setattr(il, "spot_frame", lambda *a, **k: kl.copy())
    monkeypatch.setattr(il, "atm_iv_series",
                        lambda *a, **k: {"frame": ivf.copy(), "notes": []})
    monkeypatch.setattr(il, "dvol_series", fake_dvol)
    monkeypatch.setattr(il, "shift_test", fake_shift)


def test_analyze_borrows_btc_dvol_for_family_without_official_index(monkeypatch):
    """SOL 家族：DVOL 走 BTC（借市场基准），notes + markdown 双处显式标注。"""
    seen: dict = {}
    _fake_analyze_env(monkeypatch, seen)
    res = il.analyze_iv_leadlag("SOL-USD_UM", days=1, bucket="15m", n_iter=50)
    assert res["ok"] is True
    assert seen["ccy"] == "BTC"
    assert res["dvol"]["currency"] == "BTC"
    assert res["dvol"]["proxy_for"] == "SOL"
    assert any("无官方 DVOL" in n for n in res["notes"])
    assert "借用作市场基准" in res["markdown"]
    assert "这不是 SOL 自己的指数" in res["markdown"]


def test_analyze_uses_own_official_dvol_when_present(monkeypatch):
    """ETH 家族：用 ETH 官方 DVOL，proxy_for 为空且**不得**出现借用标注。"""
    seen: dict = {}
    _fake_analyze_env(monkeypatch, seen)
    res = il.analyze_iv_leadlag("ETH-USD_UM", days=1, bucket="15m", n_iter=50)
    assert seen["ccy"] == "ETH"
    assert res["dvol"]["proxy_for"] is None
    assert not any("无官方 DVOL" in n for n in res["notes"])
    assert "借用作市场基准" not in res["markdown"]


def test_analyze_marks_explicit_foreign_dvol_currency(monkeypatch):
    """显式指定他人指数（ETH 家族查 BTC DVOL）同样要标注借用。"""
    seen: dict = {}
    _fake_analyze_env(monkeypatch, seen)
    res = il.analyze_iv_leadlag("ETH-USD_UM", days=1, bucket="15m", n_iter=50,
                                dvol_currency="btc")
    assert seen["ccy"] == "BTC"
    assert res["dvol"]["proxy_for"] == "ETH"
    assert "借用作市场基准" in res["markdown"]
