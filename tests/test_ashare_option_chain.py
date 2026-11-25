"""A 股期权链快照（``ashare.option_chain``）单测：纯函数往返 + 打桩快照。

不打真实网络：``contracts.contracts`` / ``tape.fetch_spot`` / ``tape.fetch_quotes``
全部 monkeypatch。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from nanobot_quant import bs_pricing
from nanobot_quant.ashare import option_chain as oc
from nanobot_quant.ashare import tape

MULT = oc.MULT


@pytest.fixture(autouse=True)
def _clear():
    oc.clear_cache()
    yield
    oc.clear_cache()


# ── 小工具 ──────────────────────────────────────────────────

def test_median_even_odd_and_empty():
    assert oc.median([]) is None
    assert oc.median([3.0]) == 3.0
    assert oc.median([1.0, 3.0]) == 2.0
    assert oc.median([5.0, 1.0, 3.0]) == 3.0


def test_mid_of_prefers_two_sided_but_accepts_one_side():
    assert oc.mid_of({"bid": 0.02, "ask": 0.04}) == pytest.approx(0.03)
    assert oc.mid_of({"bid": 0.02, "ask": None}) == pytest.approx(0.02)
    assert oc.mid_of({"bid": None, "ask": 0.04}) == pytest.approx(0.04)
    assert oc.mid_of({"bid": None, "ask": None}) is None


def test_full_mid_requires_both_sides():
    assert oc.full_mid_of({"bid": 0.02, "ask": 0.04}) == pytest.approx(0.03)
    assert oc.full_mid_of({"bid": 0.02, "ask": None}) is None


def test_years_of():
    assert oc.years_of(0) == 0.0
    assert oc.years_of(-3) == 0.0
    assert oc.years_of(365) == pytest.approx(1.0)
    assert oc.years_of(73) == pytest.approx(0.2)


# ── 隐含远期 ────────────────────────────────────────────────

def test_implied_forward_recovers_forward():
    """(C−P)/MULT = F − K ⇒ F = K + (C−P)/MULT（报价单位 = 元/张）。"""
    f_true, k = 2.85, 2.90
    p_mid = 0.060                            # 元/份
    c_mid = p_mid + (f_true - k)               # = 0.010
    rows = [{"strike": k, "right": "C", "mid": c_mid},
            {"strike": k, "right": "P", "mid": p_mid}]
    fw = oc.implied_forward(rows)
    assert fw["n"] == 1 and fw["n_pairs"] == 1
    assert fw["f"] == pytest.approx(f_true, abs=1e-9)


def test_implied_forward_median_across_strikes():
    f_true, base = 2.85, 0.20                # base = P 的元/份价
    rows = []
    for k in (2.80, 2.85, 2.90):
        rows.append({"strike": k, "right": "C",
                     "mid": base + (f_true - k)})
        rows.append({"strike": k, "right": "P", "mid": base})
    fw = oc.implied_forward(rows)
    assert fw["n_pairs"] == 3
    assert fw["f"] == pytest.approx(f_true, abs=1e-9)
    assert fw["mad_pct"] == pytest.approx(0.0, abs=1e-9)


def test_implied_forward_drops_outlier():
    f_true, base = 2.85, 0.20
    rows = []
    for k in (2.80, 2.85, 2.90, 2.95):
        rows.append({"strike": k, "right": "C",
                     "mid": base + (f_true - k)})
        rows.append({"strike": k, "right": "P", "mid": base})
    # 离群对：同一 strike 再来一对，C 被抬高 1 元/份 ⇒ F 被抬到 3.85
    rows.append({"strike": 2.85, "right": "C", "mid": base + 1.0})
    rows.append({"strike": 2.85, "right": "P", "mid": base})
    fw = oc.implied_forward(rows)
    assert fw["outliers"] >= 1
    assert fw["f"] == pytest.approx(2.85, abs=1e-6)


def test_implied_forward_none_without_pair():
    rows = [{"strike": 2.9, "right": "C", "mid": 0.05},
            {"strike": 2.9, "right": "C", "mid": 0.06}]
    assert oc.implied_forward(rows)["f"] is None
    assert oc.implied_forward([])["n_pairs"] == 0


def test_implied_forward_ignores_one_sided_mid():
    rows = [{"strike": 2.9, "right": "C", "mid": None},
            {"strike": 2.9, "right": "P", "mid": 0.05}]
    assert oc.implied_forward(rows)["f"] is None


# ── IV / delta ──────────────────────────────────────────────

@pytest.mark.parametrize("right", ["P", "C"])
def test_iv_of_round_trip(right):
    f, k, t, sigma = 2.85, 2.80, 30 / 365, 0.22
    per_share = bs_pricing.bs_price(f, k, t, sigma, r=0.0, right=right)
    assert per_share and per_share > 0
    got = oc.iv_of(per_share, f, k, t, right)
    assert got == pytest.approx(sigma, rel=1e-4)


def test_iv_of_fail_closed():
    assert oc.iv_of(None, 2.85, 2.8, 0.1, "P") is None
    assert oc.iv_of(0.05, 2.85, 2.8, 0.0, "P") is None      # 当日到期
    assert oc.iv_of(0.05, 0.0, 2.8, 0.1, "P") is None


def test_delta_of_sign_and_none():
    d_put = oc.delta_of(2.85, 2.80, 30 / 365, 0.22, "P")
    d_call = oc.delta_of(2.85, 2.80, 30 / 365, 0.22, "C")
    assert d_put is not None and d_put < 0
    assert d_call is not None and d_call > 0
    assert oc.delta_of(2.85, 2.80, 0.1, None, "P") is None


# ── 快照（打桩）─────────────────────────────────────────────

_TODAY = date(2026, 11, 25)


def _fake_items():
    items = []
    for i, (k, right) in enumerate([(2.80, "P"), (2.80, "C"),
                                    (2.90, "P"), (2.90, "C")]):
        items.append({"code": f"1000900{i}", "name": f"x{right}{k}",
                      "undl": "510050.SH", "right": right, "strike": k,
                      "expire": "20261223"})
    # 已到期 + 别的标的 + 超出带宽 → 都应被过滤
    items.append({"code": "10009009", "name": "old", "undl": "510050.SH",
                  "right": "P", "strike": 2.9, "expire": "20261101"})
    items.append({"code": "10009010", "name": "other", "undl": "588000.SH",
                  "right": "P", "strike": 1.25, "expire": "20261223"})
    items.append({"code": "10009011", "name": "far", "undl": "510050.SH",
                  "right": "P", "strike": 5.0, "expire": "20261223"})
    return items


def _fake_quotes(codes, timeout=12.0):
    px = {"10009000": (0.0100, 0.0110), "10009001": (0.0800, 0.0820),
          "10009002": (0.0600, 0.0620), "10009003": (0.0200, 0.0210)}
    rows = []
    for c in codes:
        b, a = px.get(c, (0.0, 0.0))
        rows.append({"i": c + ".SHO", "ts": "15:00:00", "b": b or None,
                     "a": a or None, "last": b, "iv": 0.0, "oi": 10,
                     "vol": 5, "bids": [[b, 1]], "asks": [[a, 1]]})
    return rows, 0


def test_chain_snapshot_rows_and_iv(monkeypatch):
    monkeypatch.setattr(oc.ctr, "contracts",
                        lambda *a, **kw: {"items": _fake_items(),
                                          "source": "cache:sina", "error": ""})
    monkeypatch.setattr(oc.tape, "fetch_spot", lambda u: {"510050.SH": 2.857})
    monkeypatch.setattr(oc.tape, "fetch_quotes", _fake_quotes)
    monkeypatch.setattr(oc.tape, "now_cn",
                        lambda: datetime(2026, 11, 25, 15, 5))
    snap = oc.chain_snapshot("510050.SH", "/tmp/x", today=_TODAY, force=True)
    codes = sorted(r["code"] for r in snap["rows"])
    assert codes == ["10009000", "10009001", "10009002", "10009003"]
    fw = snap["forwards"]["20261223"]
    assert fw["f"] is not None and fw["n_pairs"] == 2
    put = next(r for r in snap["rows"] if r["code"] == "10009000")
    assert put["iv"] is not None and put["iv"] > 0
    assert put["delta"] is not None and put["delta"] < 0
    assert put["otm_pct"] > 0                     # 2.80 < F ⇒ 虚值
    assert put["spread_pct"] is not None
    assert snap["spot"] == pytest.approx(2.857)
    assert snap["errors"] == 0


def test_chain_snapshot_cache_and_frozen(monkeypatch):
    calls = {"n": 0}

    def _quotes(codes, timeout=12.0):
        calls["n"] += 1
        return _fake_quotes(codes, timeout)

    monkeypatch.setattr(oc.ctr, "contracts",
                        lambda *a, **kw: {"items": _fake_items(),
                                          "source": "cache:sina", "error": ""})
    monkeypatch.setattr(oc.tape, "fetch_spot", lambda u: {"510050.SH": 2.857})
    monkeypatch.setattr(oc.tape, "fetch_quotes", _quotes)
    monkeypatch.setattr(oc.tape, "now_cn",
                        lambda: datetime(2026, 11, 25, 15, 5))
    oc.chain_snapshot("510050.SH", "/tmp/x", today=_TODAY, force=True)
    assert calls["n"] == 1
    # TTL 内 → 缓存命中，不再拉盘口
    snap = oc.chain_snapshot("510050.SH", "/tmp/x", today=_TODAY)
    assert snap["cached"] is True and calls["n"] == 1
    # 非交易时段 + TTL 过期 → 冻结返回，仍不重拉
    monkeypatch.setattr(oc.tape, "in_session", lambda now=None: False)
    monkeypatch.setattr(oc.tape, "now_cn",
                        lambda: datetime(2026, 11, 25, 20, 0))
    snap2 = oc.chain_snapshot("510050.SH", "/tmp/x", today=_TODAY, ttl=0.0)
    assert snap2["frozen"] is True and calls["n"] == 1


def test_chain_snapshot_no_spot_still_returns_rows(monkeypatch):
    monkeypatch.setattr(oc.ctr, "contracts",
                        lambda *a, **kw: {"items": _fake_items(),
                                          "source": "cache:sina", "error": ""})
    monkeypatch.setattr(oc.tape, "fetch_spot", lambda u: {})
    monkeypatch.setattr(oc.tape, "fetch_quotes", _fake_quotes)
    monkeypatch.setattr(oc.tape, "in_session", lambda now=None: True)
    snap = oc.chain_snapshot("510050.SH", "/tmp/x", today=_TODAY, force=True)
    assert snap["spot"] == 0.0
    assert "取不到标的现价" in snap["note"]
    # 无现价 ⇒ 不按带宽裁剪，far 那张也在
    assert any(r["code"] == "10009011" for r in snap["rows"])
