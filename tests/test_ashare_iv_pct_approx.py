"""候选表 IV 分位的两条取数路径：日代表值（精确）/ 分位数插值（近似）。

背景：线上候选表的分位一直显示 ``—``，而 IV 面板的 DTE 桶表本身有数据 ——
说明「桶里有分位数、但日代表值拿不到」这一层没有被覆盖。本文件锁住：

* ``bucket_refs`` 内存/磁盘来源判定，且**只带分位数的桶也要带出去**；
* ``percentile_approx`` 用 p5/p25/p50/p75/p95 线性插值，并把 ``approx=True``
  如实回传（页面打 ≈，不能当精确值）；
* ``build_candidates`` 的回调返回 ``(分位, 是否近似)`` 时能落到行字段。
"""

from __future__ import annotations

from datetime import date

import pytest

from nanobot_quant.ashare import chain_workbench as cwb
from nanobot_quant.ashare import iv_panel as ivp


def _payload(values=None, *, with_values: bool = True) -> dict:
    buckets = [
        {"lo": 0, "hi": 3, "days": 0, "p5": None},
        {"lo": 4, "hi": 7, "days": 2, "p5": 13.67, "p25": 13.76, "p50": 13.88,
         "p75": 14.00, "p95": 14.09},
        {"lo": 26, "hi": 40, "days": 20, "p5": 13.54, "p25": 13.95,
         "p50": 14.83, "p75": 15.20, "p95": 15.64},
    ]
    if with_values:
        buckets[1]["_values"] = list(values or [13.7, 13.8, 13.9, 14.0])
        buckets[2]["_values"] = [14.0, 14.5, 15.0, 15.6]
    return {"undl": "510050.SH", "buckets": buckets}


@pytest.fixture()
def cached(monkeypatch):
    """磁盘缓存路径：``_as_dirs`` 给一个假目录，``_read_cache`` 返回载荷。"""
    holder = {}

    monkeypatch.setattr(ivp, "_state_of", lambda undl: {})
    monkeypatch.setattr(ivp, "_as_dirs", lambda src=None: ["/tmp/__fake_run__"])
    monkeypatch.setattr(ivp, "_cache_path", lambda dirs, undl: "/tmp/__fake__.json")
    monkeypatch.setattr(ivp, "_read_cache", lambda path: holder.get("payload"))
    return holder


def test_bucket_refs_reads_values_from_disk(cached):
    cached["payload"] = _payload()
    refs, src = ivp.bucket_refs("510050.SH")
    assert src == "disk"
    assert [r["lo"] for r in refs] == [4, 26]      # 0–3 天无样本 → 不出现
    assert all(r["values"] for r in refs)
    pct, approx = ivp.percentile_approx(refs, 27, 14.48)
    assert approx is False
    assert pct == pytest.approx(25.0)              # 14.48 < 14.5 → 1/4 样本更小


def test_bucket_refs_keeps_quantiles_only(cached):
    """只有分位数的旧缓存也要能用（此前直接判「无样本」）。"""
    cached["payload"] = _payload(with_values=False)
    refs, src = ivp.bucket_refs("510050.SH")
    assert src == "disk" and len(refs) == 2
    assert all(not r["values"] for r in refs)
    pct, approx = ivp.percentile_approx(refs, 27, 14.48)
    assert approx is True
    # 14.48 落在 p25(13.95)–p50(14.83) 之间 → 25 + 0.602*25 ≈ 40
    assert pct == pytest.approx(40.0, abs=0.5)


def test_percentile_approx_clamps_and_misses(cached):
    cached["payload"] = _payload(with_values=False)
    refs, _ = ivp.bucket_refs("510050.SH")
    assert ivp.percentile_approx(refs, 27, 1.0)[0] == pytest.approx(5.0)
    assert ivp.percentile_approx(refs, 27, 99.0)[0] == pytest.approx(95.0)
    assert ivp.percentile_approx(refs, 70, 14.48) == (None, False)   # 无可比桶
    assert ivp.percentile_approx(refs, None, 14.48) == (None, False)


def test_build_candidates_marks_approx(monkeypatch):
    rows = [
        {"code": "P1", "right": "P", "strike": 2.7, "expiry": "20261223",
         "underlying": "510050.SH", "bid": 0.0084, "net_premium": 82.8,
         "net_yield_pct": 0.3, "dte": 27, "iv": 0.165, "delta": -0.13},
        {"code": "P2", "right": "P", "strike": 2.65, "expiry": "20261223",
         "underlying": "510050.SH", "bid": 0.0045, "net_premium": 43.8,
         "net_yield_pct": 0.165, "dte": 27, "iv": 0.176, "delta": -0.07},
    ]
    out = cwb.build_candidates(
        rows, 2.864, today=date(2026, 11, 26), expiry="20261223",
        underlying="510050.SH", params=cwb.PaperParams(),
        iv_pct_fn=lambda iv, dte: (42.0, True))
    assert out["rows"] and all(r["iv_pct"] == pytest.approx(42.0) for r in out["rows"])
    assert all(r["iv_pct_approx"] is True for r in out["rows"])


def test_build_candidates_accepts_plain_float(monkeypatch):
    rows = [{"code": "P1", "right": "P", "strike": 2.7, "expiry": "20261223",
             "underlying": "510050.SH", "bid": 0.0084, "net_premium": 82.8,
             "net_yield_pct": 0.3, "dte": 27, "iv": 0.165, "delta": -0.13}]
    out = cwb.build_candidates(
        rows, 2.864, today=date(2026, 11, 26), expiry="20261223",
        underlying="510050.SH", params=cwb.PaperParams(),
        iv_pct_fn=lambda iv, dte: 42.0)
    assert out["rows"][0]["iv_pct"] == pytest.approx(42.0)
    assert out["rows"][0]["iv_pct_approx"] is False
