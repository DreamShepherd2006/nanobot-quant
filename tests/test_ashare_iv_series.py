"""A 股 IV 序列与分位（``ashare.iv_series``）单测：合成数据有解析解可核对。

不打网络、不依赖 pandas —— 全部用 ``bs_pricing`` 生成价格再反解，验证
（a）IV 往返、（b）ATM/25d 选档、（c）多分片归并、（d）分位的「按日」口径。
"""

from __future__ import annotations

import csv
import gzip

import pytest

from nanobot_quant import bs_pricing
from nanobot_quant.ashare import iv_series as ivs

UNDL = "510050.SH"
MONTH = "202612"
EXP = "20261223"
F = 2.85
SIGMA = 0.15
STRIKES = (2.65, 2.70, 2.75, 2.80, 2.85, 2.90, 2.95, 3.00, 3.05)
TS = "20261119100000"

C_HEAD = ["undl", "code", "month", "type", "strike", "expire", "mult", "tick"]
B_HEAD = ["ts", "code", "open", "high", "low", "close", "volume", "amount"]


# ── 造数据 ──────────────────────────────────────────────────

def _contracts(strikes=STRIKES, exp=EXP, month=MONTH, undl=UNDL):
    out, n = [], 10010000
    for k in strikes:
        for right in ("C", "P"):
            n += 1
            out.append({"undl": undl, "code": f"{n}.SHO", "month": month, "type": right,
                        "strike": k, "expire": exp, "mult": 10000, "tick": 0.0001})
    return out


def _write_contracts(path, ctrs):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=C_HEAD)
        w.writeheader()
        w.writerows(ctrs)


def _shard_rows(ctrs, ts_list, *, f=F, sigma=SIGMA, sigma_shift=None, vol=10):
    """用 BS 生成每分钟各合约收盘价；``sigma_shift(code)->Δσ`` 支持构造微笑。"""
    rows = []
    for ts in ts_list:
        dte = ivs.dte_at(ts, ctrs[0]["expire"])
        t = dte / 365.0
        for c in ctrs:
            sig = sigma + (sigma_shift(c) if sigma_shift else 0.0)
            px = bs_pricing.bs_price(f, float(c["strike"]), t, sig, r=0.0,
                                     right=c["type"])
            rows.append({"ts": ts, "code": c["code"], "open": px, "high": px,
                         "low": px, "close": px, "volume": vol, "amount": px * 10000})
    return rows


def _write_shard(path, rows, *, gz=False):
    opener = gzip.open if gz else open
    kw = {"mode": "wt"} if gz else {"mode": "w"}
    with opener(path, encoding="utf-8-sig", newline="", **kw) as fh:
        w = csv.DictWriter(fh, fieldnames=B_HEAD)
        w.writeheader()
        w.writerows(rows)


def _build(tmp_path, *, strikes=STRIKES, ts_list=(TS,), sigma_shift=None, vol=10, gz=False):
    ctrs = _contracts(strikes=strikes)
    _write_contracts(tmp_path / "contracts_all.csv", ctrs)
    rows = _shard_rows(ctrs, ts_list, sigma_shift=sigma_shift, vol=vol)
    name = f"opt1m_{ivs.compact_undl(UNDL)}_{MONTH}_p01.csv" + (".gz" if gz else "")
    _write_shard(tmp_path / name, rows, gz=gz)
    return ctrs


# ── 基础工具 ────────────────────────────────────────────────

def test_dte_at_uses_expiry_1500():
    assert ivs.dte_at("20261223150000", EXP) == pytest.approx(0.0, abs=1e-9)
    assert ivs.dte_at("20261222150000", EXP) == pytest.approx(1.0, abs=1e-9)
    # 早盘到到期日 15:00 = 34 天 + 5 小时
    assert ivs.dte_at(TS, EXP) == pytest.approx(34 + 5 / 24.0, abs=1e-6)


def test_compact_undl():
    assert ivs.compact_undl("510050.SH") == "510050SH"
    assert ivs.compact_undl("588000.SH") == "588000SH"


def test_shard_paths_filters_by_undl_and_month(tmp_path):
    for name in (f"opt1m_510050SH_{MONTH}_p01.csv", "opt1m_588000SH_202611_p01.csv",
                 "opt1m_510050SH_202703_p02.csv.gz"):
        (tmp_path / name).write_text("", encoding="utf-8")
    got = ivs.shard_paths(tmp_path, UNDL)
    assert len(got) == 2
    got_m = ivs.shard_paths(tmp_path, UNDL, months=["202612"])
    assert len(got_m) == 1 and got_m[0].endswith("202612_p01.csv")


# ── 序列 ────────────────────────────────────────────────────

def test_single_minute_recovers_iv(tmp_path):
    _build(tmp_path)
    res = ivs.iv_series_from_bars(tmp_path, undl=UNDL)
    assert res["stats"]["minutes"] == 1
    row = res["rows"][0]
    assert row["quality"] == "ok"
    assert row["f"] == pytest.approx(F, abs=5e-4)
    assert row["atm_strike"] == 2.85
    assert row["atm_iv"] == pytest.approx(SIGMA, abs=5e-4)
    assert row["atm_n"] == 2


def test_atm_follows_forward_not_mid_strike(tmp_path):
    # F 上移到 2.93 → ATM 档应为 2.95（两侧 2.90/2.95 中最近的）
    ctrs = _contracts()
    _write_contracts(tmp_path / "contracts_all.csv", ctrs)
    rows = _shard_rows(ctrs, (TS,), f=2.93)
    _write_shard(tmp_path / f"opt1m_{ivs.compact_undl(UNDL)}_{MONTH}_p01.csv", rows)
    row = ivs.iv_series_from_bars(tmp_path, undl=UNDL)["rows"][0]
    assert row["f"] == pytest.approx(2.93, abs=5e-4)
    assert row["atm_strike"] == 2.95


def test_stale_flagged_when_volume_zero(tmp_path):
    _build(tmp_path, vol=0)
    row = ivs.iv_series_from_bars(tmp_path, undl=UNDL)["rows"][0]
    assert row["stale"] is True
    assert row["quality"] == "ok"          # 能算 ≠ 可信，两个维度分开


def test_gzip_shard_supported(tmp_path):
    _build(tmp_path, gz=True)
    res = ivs.iv_series_from_bars(tmp_path, undl=UNDL)
    assert res["rows"][0]["atm_iv"] == pytest.approx(SIGMA, abs=5e-4)


def test_multi_shard_merge_same_ts(tmp_path):
    # 两个到期（两个月）各自一个分片，同一分钟 → 两行
    c1 = _contracts(month="202611", exp="20261125")
    c2 = _contracts(month="202612", exp="20261223")
    # code 不能重名
    for i, c in enumerate(c2):
        c["code"] = f"2000{i:04d}.SHO"
    _write_contracts(tmp_path / "contracts_all.csv", c1 + c2)
    _write_shard(tmp_path / f"opt1m_{ivs.compact_undl(UNDL)}_202611_p01.csv",
                 _shard_rows(c1, (TS,)))
    _write_shard(tmp_path / f"opt1m_{ivs.compact_undl(UNDL)}_202612_p01.csv",
                 _shard_rows(c2, (TS,)))
    res = ivs.iv_series_from_bars(tmp_path, undl=UNDL)
    assert res["stats"]["minutes"] == 1
    assert len(res["rows"]) == 2
    assert {r["expire"] for r in res["rows"]} == {"20261125", "20261223"}
    for r in res["rows"]:
        assert r["atm_iv"] == pytest.approx(SIGMA, abs=5e-4)


def test_band_filter_shrinks_candidates(tmp_path):
    # 带宽 0.1% → 只剩最接近 F 的 2.85 档，ATM 仍能算
    _build(tmp_path, strikes=(2.60, 2.85, 3.10))
    res = ivs.iv_series_from_bars(tmp_path, undl=UNDL, band_pct=0.1)
    row = res["rows"][0]
    assert row["atm_strike"] == 2.85
    assert row["atm_iv"] == pytest.approx(SIGMA, abs=5e-4)


def test_min_pairs_fail_closed(tmp_path):
    _build(tmp_path, strikes=(2.85,))          # 只有 1 对
    row = ivs.iv_series_from_bars(tmp_path, undl=UNDL)["rows"][0]
    assert row["quality"] == "no_pair"
    assert row["atm_iv"] is None


def test_skew_sign_when_puts_richer(tmp_path):
    # 构造**无套利**的微笑：同 strike 的 call/put 用同一 σ（否则 parity 不成立、
    # 反解 F 会被拉偏），低 strike 侧 IV 更高 → skew = iv25c − iv25p < 0。
    def shift(c):
        return 0.02 * (F - float(c["strike"])) / 0.10

    _build(tmp_path, sigma_shift=shift)
    row = ivs.iv_series_from_bars(tmp_path, undl=UNDL)["rows"][0]
    assert row["f"] == pytest.approx(F, abs=5e-4)
    assert row["skew25_c"] is not None and row["skew25_p"] is not None
    assert row["skew25_p"] > row["skew25_c"]
    assert row["skew"] == pytest.approx(-0.04, abs=6e-3)


def test_days_filter(tmp_path):
    _build(tmp_path, ts_list=(TS, "20261120100000"))
    res = ivs.iv_series_from_bars(tmp_path, undl=UNDL, days={"20261120"})
    assert res["stats"]["minutes"] == 1
    assert res["rows"][0]["ts"] == "20261120100000"


# ── 分位 ────────────────────────────────────────────────────

def _fake_rows(days=10, per_day=3, dte=10, base=0.10):
    rows = []
    for d in range(1, days + 1):
        for i in range(per_day):
            rows.append({"ts": f"202611{d:02d}10{i:02d}00", "dte": dte,
                         "atm_iv": base + d / 1000.0 + i / 100000.0,
                         "quality": "ok", "stale": False})
    return rows


def test_percentile_uses_daily_samples_not_minutes():
    rows = _fake_rows(days=10, per_day=3)
    res = ivs.iv_percentile(rows, dte=(8, 15))
    assert res["days"] == 10            # 分母按天
    assert res["minutes"] == 30
    # daily=last → 每天最后一个点 = base + d/1000 + 2/100000
    assert res["values"][0] == pytest.approx(0.10 + 0.001 + 0.00002, abs=1e-9)


def test_percentile_current_position():
    rows = _fake_rows(days=10, per_day=1)
    res = ivs.iv_percentile(rows, current=0.1055)   # 第 5 名与第 6 名之间
    assert res["current_pct"] == pytest.approx(50.0, abs=1e-6)
    assert res["percentiles"][50] == pytest.approx(0.1055, abs=1e-9)


def test_percentile_filters_dte_and_quality():
    rows = _fake_rows(days=5, per_day=1, dte=10)
    rows += _fake_rows(days=5, per_day=1, dte=30)
    rows.append({"ts": "20261115999999", "dte": 10, "atm_iv": 9.9,
                 "quality": "f_noisy", "stale": False})
    res = ivs.iv_percentile(rows, dte=(8, 15))
    assert res["days"] == 5
    assert all(v < 1 for v in res["values"])
    assert res["skipped"].get("f_noisy") == 1


def test_percentile_drop_stale():
    rows = _fake_rows(days=3, per_day=1)
    for r in rows:
        r["stale"] = True
    assert ivs.iv_percentile(rows, drop_stale=True)["days"] == 0
    assert ivs.iv_percentile(rows, drop_stale=False)["days"] == 3


def test_bucket_table_lists_all_buckets():
    rows = _fake_rows(days=6, per_day=1, dte=10)
    rows += _fake_rows(days=6, per_day=1, dte=30)
    tab = ivs.bucket_table(rows, buckets=((8, 15), (26, 40)))
    assert [b["days"] for b in tab["buckets"]] == [6, 6]
    assert "IV 分位" in tab["markdown"]


def test_markdown_mentions_sample_caveat():
    res = ivs.iv_percentile(_fake_rows(days=3, per_day=1))
    assert res["sufficient"] is False
    assert "偏少" in res["markdown"]
    assert "按「日」" in res["markdown"] or "以「日」" in res["markdown"]


def test_sample_tier_labels():
    assert ivs._sample_tier(5) == "偏少"
    assert ivs._sample_tier(12) == "初步"
    assert ivs._sample_tier(30) == "足够"


# ── 常数期限插值 ────────────────────────────────────────

def _ts_rows(ts, pairs):
    return [{"ts": ts, "dte": d, "atm_iv": v, "quality": "ok", "stale": False}
            for d, v in pairs]


def test_constant_maturity_interpolates_between_expiries():
    rows = _ts_rows("20261119100000", [(6.0, 0.16), (34.0, 0.14)])
    cm = ivs.constant_maturity_iv(rows, target_dte=20.0)
    assert len(cm["rows"]) == 1
    # 线性：0.16 + (0.14−0.16)×(20−6)/28 = 0.15
    assert cm["rows"][0]["atm_iv"] == pytest.approx(0.15, abs=1e-9)
    assert cm["rows"][0]["dte_lo"] == 6.0 and cm["rows"][0]["dte_hi"] == 34.0


def test_constant_maturity_hits_exact_expiry():
    rows = _ts_rows("20261119100000", [(30.0, 0.17), (60.0, 0.13)])
    cm = ivs.constant_maturity_iv(rows, target_dte=30.0)
    assert cm["rows"][0]["atm_iv"] == pytest.approx(0.17, abs=1e-12)


def test_constant_maturity_never_extrapolates():
    short = _ts_rows("20261119100000", [(6.0, 0.16), (12.0, 0.15)])   # 全比 30 短
    long_ = _ts_rows("20261120100000", [(60.0, 0.12), (120.0, 0.11)])  # 全比 30 长
    cm = ivs.constant_maturity_iv(short + long_, target_dte=30.0)
    assert cm["rows"] == []
    assert cm["stats"]["no_above"] == 1 and cm["stats"]["no_below"] == 1


def test_constant_maturity_percentile_yields_daily_samples():
    rows = []
    for d in range(1, 8):
        rows += _ts_rows(f"202611{d:02d}100000", [(6.0, 0.16), (34.0, 0.14 + d / 10000.0)])
    cm = ivs.constant_maturity_iv(rows, target_dte=20.0)
    pct = ivs.iv_percentile(cm["rows"], dte=(20.0, 20.0))
    assert pct["days"] == 7
    assert "DTE 20 天" in pct["markdown"]


def test_constant_maturity_skips_failed_quality():
    rows = _ts_rows("20261119100000", [(6.0, 0.16), (34.0, 0.14)])
    rows[0]["quality"] = "f_noisy"
    cm = ivs.constant_maturity_iv(rows, target_dte=20.0)
    assert cm["stats"]["no_below"] == 1     # 短档被剔除 → 不外推
    assert cm["rows"] == []
