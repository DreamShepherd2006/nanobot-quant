"""A 股 IV 分位面板（``ashare.iv_panel``）单测：纯函数 + 定位 + 状态机。

不打网络：链快照用合成 dict、历史序列用合成 rows（字段与 ``iv_series`` 一致）。
"""

from __future__ import annotations

import json

import pytest

from nanobot_quant.ashare import iv_panel as ivp


# ── 造数据 ──────────────────────────────────────────────────

def _row(ts, expire, dte, iv, *, quality="ok", stale=False, atm_c=None, atm_p=None):
    return {"ts": ts, "expire": expire, "dte": dte, "f": 2.85, "atm_iv": iv,
            "atm_iv_c": iv if atm_c is None else atm_c,
            "atm_iv_p": iv if atm_p is None else atm_p,
            "atm_strike": 2.85, "quality": quality, "stale": stale}


def _series(days=5):
    """两天到期（15D / 45D）+ 每天收盘前几个分钟点，30d 可插值。"""
    rows = []
    for d in range(days):
        day = f"202611{10 + d:02d}"
        for minute in ("145900", "145800"):
            rows.append(_row(f"{day}{minute}", "20261223", 15.0, 0.15))
            rows.append(_row(f"{day}{minute}", "20270324", 45.0, 0.21))
    return rows


def _snap(legs):
    """legs: [(expiry, dte, f, atm_c, atm_p)] → 链快照形状。"""
    rows, forwards, exps = [], {}, []
    for i, (expiry, dte, f, c, p) in enumerate(legs):
        forwards[expiry] = {"f": f, "n_pairs": 5, "mad_pct": 0.1}
        exps.append(expiry)
        rows.append({"expiry": expiry, "right": "C", "strike": round(f, 2),
                     "dte": dte, "iv": c, "mid": 0.1})
        rows.append({"expiry": expiry, "right": "P", "strike": round(f, 2),
                     "dte": dte, "iv": p, "mid": 0.1})
    return {"rows": rows, "forwards": forwards, "expiries": exps}


# ── live_cm_iv ──────────────────────────────────────────────

def test_live_cm_iv_interpolates_by_dte():
    snap = _snap([("20261223", 15.0, 2.85, 0.14, 0.16), ("20270324", 45.0, 2.83, 0.22, 0.22)])
    out = ivp.live_cm_iv(snap, 30.0)
    assert out["iv"] == pytest.approx(0.15 + (0.22 - 0.15) * (30 - 15) / (45 - 15))


def test_live_cm_iv_never_extrapolates():
    snap = _snap([("20261223", 15.0, 2.85, 0.14, 0.16), ("20270324", 20.0, 2.83, 0.22, 0.22)])
    out = ivp.live_cm_iv(snap, 30.0)
    assert out["iv"] is None and "不外推" in out["note"]
    assert out["dte_lo"] == 20.0


def test_live_cm_iv_exact_hit_uses_single_leg():
    snap = _snap([("20261223", 30.0, 2.85, 0.14, 0.16)])
    out = ivp.live_cm_iv(snap, 30.0)
    assert out["iv"] == pytest.approx(0.15)      # call/put 均值


def test_live_cm_iv_empty_snapshot():
    out = ivp.live_cm_iv({"rows": [], "forwards": {}}, 30.0)
    assert out["iv"] is None and out["note"]


# ── 分位工具 ────────────────────────────────────────────────

def test_pctile_basic():
    vals = [0.10, 0.12, 0.14, 0.16, 0.18]
    assert ivp.pctile(0.14, vals) == pytest.approx(60.0)
    assert ivp.pctile(0.09, vals) == pytest.approx(0.0)
    assert ivp.pctile(0.99, vals) == pytest.approx(100.0)
    assert ivp.pctile(0.14, []) is None
    assert ivp.pctile(None, vals) is None


def test_quantiles_of_matches_sorted_positions():
    q = ivp.quantiles_of([0.20, 0.10, 0.30, 0.40, 0.50])
    assert q["5"] is not None and q["50"] == pytest.approx(0.30)
    assert ivp.quantiles_of([])["50"] is None


# ── summarize ───────────────────────────────────────────────

def test_summarize_buckets_and_cm30():
    stats = {"minutes": 10, "rows": 20, "iv_ok": 20, "stale_rows": 1,
             "quality": {"ok": 20}}
    out = ivp.summarize(_series(), stats)
    assert out["days"] == 5 and out["stale_pct"] == pytest.approx(5.0)
    labels = [b["label"] for b in out["buckets"]]
    assert "8–15" in labels and "41+" in labels
    b15 = next(b for b in out["buckets"] if b["label"] == "8–15")
    assert b15["days"] == 5 and b15["current"] == pytest.approx(0.15)
    cm = out["cm30"]
    assert cm["days"] == 5
    assert cm["last_iv"] == pytest.approx(0.15 + (0.21 - 0.15) * (30 - 15) / (45 - 15))
    assert cm["last_day"] == "20261114"


def test_summarize_is_json_serializable():
    out = ivp.summarize(_series(), {"minutes": 10, "rows": 20, "iv_ok": 20,
                                    "stale_rows": 0, "quality": {"ok": 20}})
    json.dumps(out)          # 面板载荷要能落缓存 / 直接回前端


def test_summarize_empty_rows():
    out = ivp.summarize([], {"minutes": 0, "rows": 0, "iv_ok": 0,
                             "stale_rows": 0, "quality": {}})
    assert out["days"] == 0 and out["cm30"]["days"] == 0
    assert all(b["days"] == 0 for b in out["buckets"])


# ── 数据定位 / 状态机 ──────────────────────────────────────

def _mk_run(root, name="run_20261120_012436"):
    d = root / "qmt-option-1m" / name
    d.mkdir(parents=True)
    (d / "contracts_all.csv").write_text("h\n", encoding="utf-8")
    (d / "opt1m_510050SH_202612_p1.csv").write_text("h\n", encoding="utf-8")
    return d


def test_find_option_dir_picks_latest(tmp_path, monkeypatch):
    monkeypatch.setenv(ivp.DATA_ROOT_ENV, str(tmp_path))
    _mk_run(tmp_path, "run_20260101_000000")
    latest = _mk_run(tmp_path, "run_20261120_012436")
    assert ivp.find_option_dir() == latest


def test_panel_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv(ivp.DATA_ROOT_ENV, str(tmp_path / "nope"))
    ivp.reset()
    out = ivp.panel("510050.SH")
    assert out["ok"] is False and out["status"] == "no_data"
    assert out["data_roots"]


def test_panel_uses_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setenv(ivp.DATA_ROOT_ENV, str(tmp_path))
    src = _mk_run(tmp_path)
    ivp.reset()
    payload = {"ok": True, "symbol": "510050.SH", "run": src.name,
               "cm30": {"values": [0.15], "days": 1}, "buckets": []}
    cache = ivp._cache_path(src, "510050.SH")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    out = ivp.panel("510050.SH")
    assert out["status"] == "ready" and out.get("from_disk") is True
    assert out["run"] == src.name


def test_panel_starts_background_thread_once(tmp_path, monkeypatch):
    monkeypatch.setenv(ivp.DATA_ROOT_ENV, str(tmp_path))
    _mk_run(tmp_path)
    ivp.reset()
    calls = []

    def _fake_compute(undl, src=None):
        calls.append(undl)
        import time
        time.sleep(0.05)
        return {"ok": True, "symbol": undl, "run": "x", "cm30": {}, "buckets": []}

    monkeypatch.setattr(ivp, "compute", _fake_compute)
    first = ivp.panel("510050.SH")
    second = ivp.panel("510050.SH")
    assert first["status"] == "computing" and second["status"] == "computing"
    import time
    for _ in range(100):
        if ivp._state_of("510050.SH")["status"] == "ready":
            break
        time.sleep(0.02)
    assert ivp._state_of("510050.SH")["status"] == "ready"
    assert calls == ["510050.SH"]            # 同一标的不重复起线程
