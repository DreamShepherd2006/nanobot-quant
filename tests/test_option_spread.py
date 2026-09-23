"""`analysis.option_spread`（期权盘口价差画像，只读）单元测试 —— 全程不触网。

覆盖：分桶、逐行指标（BS 双边反解往返）、跳过原因分类、覆盖率门、
合成 tape 的端到端汇总、markdown 渲染、CLI 冒烟，以及**只读边界**
（源码内不得出现下单/参数写入等交易路径符号）。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot_quant import option_tape as tp
from nanobot_quant.analysis import option_spread as os_
from nanobot_quant.bs_pricing import bs_price

FAM = "SOL-USD_UM"
SPOT = 116.67
EXP_DAY = "260926"                       # 固定到期日 → 测试可复现


def _exp_ms(day: str = EXP_DAY) -> int:
    dt = datetime.strptime(day + "0800", "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _row(inst, bid, ask, dv="-0.15", mv="0.70"):
    return {"i": inst, "b": bid, "a": ask, "bs": "30", "as": "20", "mv": mv, "dv": dv}


def _rec(ts_ms, rows, spot=SPOT):
    return {"ts": datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_ms": ts_ms, "cfg": {}, "spot": {FAM: spot}, "rows": rows}


def _iv_two_sided(inst_strike, right, t_years, sig_bid, sig_ask, spot=SPOT):
    """按 IV 双边生成 bid/ask 报价（真实做市商结构）。"""
    return _row(inst_strike, round(bs_price(spot, _strike_of(inst_strike), t_years,
                                           sig_bid, right=right), 6),
                round(bs_price(spot, _strike_of(inst_strike), t_years,
                               sig_ask, right=right), 6))


def _strike_of(inst: str) -> float:
    return float(inst.split("-")[-2])


def _flat_metrics(inst="SOL-USD_UM-260926-110-P", ts_ms=None, sig_bid=0.575,
                  sig_ask=0.725, right="P", spot=SPOT):
    ts_ms = ts_ms or (_exp_ms() - int(3 * 86_400_000))
    t = (_exp_ms() - ts_ms) / 86_400_000 / 365
    r = _iv_two_sided(inst, right, t, sig_bid, sig_ask, spot=spot)
    return os_.row_metrics(dict(
        family=FAM, inst=inst, right=right, ts_ms=ts_ms, expiry_ms=_exp_ms(),
        strike=_strike_of(inst), spot=spot, bid=r["b"], ask=r["a"],
        delta="-0.15", mark_vol="0.65"))


@pytest.fixture
def _iso(tmp_path, monkeypatch):
    """隔离 tape 落盘目录（与 test_option_tape 同一手法）。"""
    monkeypatch.setattr(tp, "tape_dir", lambda: tmp_path / tp.TAPE_DIR_NAME)
    return tmp_path


def _write_day(tmp_path, day: str, recs: list) -> Path:
    p = tmp_path / tp.TAPE_DIR_NAME / f"tape_{day}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def _days(n: int) -> list:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)][::-1]


# ── 分桶 ─────────────────────────────────────────────────

def test_buckets():
    assert os_.delta_bucket(0.02) == "|Δ|≤0.05"
    assert os_.delta_bucket(-0.12) == "0.05–0.15"      # 取绝对值
    assert os_.delta_bucket(None) == "—"
    assert os_.dte_bucket(0.5) == "≤1d"
    assert os_.dte_bucket(2.0) == "1–3d"
    assert os_.dte_bucket(30.0) == ">14d"
    assert os_.dte_bucket(None) == "—"


# ── 逐行指标 ─────────────────────────────────────────────

def test_row_metrics_recovers_iv_spread():
    """IV 双边 15 点 → 反解出的 Δσ ≈ 15 点，且现模型等价量远小于它。"""
    m, reason = _flat_metrics(sig_bid=0.575, sig_ask=0.725)
    assert reason == "" and m is not None
    assert 14.0 < m["iv_spread_pts"] < 16.0
    assert 0 < m["model_iv_spread_pts"] < 2.0        # ±0.5% 价格价差 ≈ 0.2 点
    assert m["price_spread_pct"] > 50.0              # 5-7% 虚值档价差占权利金很宽
    assert m["dte_days"] == pytest.approx(3.0, abs=0.01)


def test_row_metrics_skip_reasons():
    ts = _exp_ms() - 86_400_000
    base = dict(family=FAM, inst="SOL-USD_UM-260926-110-P", right="P", ts_ms=ts,
                expiry_ms=_exp_ms(), strike=110.0, spot=SPOT)
    assert os_.row_metrics(dict(base, bid=0.0, ask=0.5))[1] == "no_quote"
    assert os_.row_metrics(dict(base, bid=0.5, ask=0.5))[1] == "no_quote"
    assert os_.row_metrics(dict(base, bid=0.4, ask=0.5, spot=0))[1] == "no_spot"
    assert os_.row_metrics(dict(base, bid=0.4, ask=0.5, strike=None))[1] == "no_strike"
    assert os_.row_metrics(dict(base, bid=0.4, ask=0.5, ts_ms=None))[1] == "no_expiry"
    # 已到期
    assert os_.row_metrics(dict(base, bid=0.4, ask=0.5, ts_ms=_exp_ms()))[1] == "expired"
    # 权利金 > 行权价（put 无解）→ IV 反解失败
    assert os_.row_metrics(dict(base, bid=200.0, ask=210.0))[1] == "no_iv"


def test_delta_fallback_uses_bs_when_tape_lacks_greeks():
    """OKX bulk ticker 不带 greeks → |Δ| 由中价 IV 经 BS 反算（与回测同一路径）。"""
    m, reason = _flat_metrics()
    assert reason == "" and m["delta_src"] == "tape"
    ts = _exp_ms() - int(3 * 86_400_000)
    t = (_exp_ms() - ts) / 86_400_000 / 365
    r = _iv_two_sided("SOL-USD_UM-260926-110-P", "P", t, 0.575, 0.725)
    m2, reason2 = os_.row_metrics(dict(
        family=FAM, inst="SOL-USD_UM-260926-110-P", right="P", ts_ms=ts,
        expiry_ms=_exp_ms(), strike=110.0, spot=SPOT, bid=r["b"], ask=r["a"]))
    assert reason2 == "" and m2["delta_src"] == "bs"
    assert -0.6 < m2["delta"] < 0.0                  # put delta 恒为负
    assert os_.delta_bucket(m2["delta"]) != "—"      # 能进桶（此前恒为 —）


# ── 汇总 ─────────────────────────────────────────────────

def _synth_days(tmp_path, n_days=2, per_day=3, sig_spread=0.15):
    days = _days(n_days)
    for i, day in enumerate(days):
        ts = _exp_ms() - int((3 + i) * 86_400_000)
        rows = []
        for k, strike in enumerate((110, 112, 114)):
            inst = f"{FAM}-260926-{strike}-P"
            t = (_exp_ms() - ts) / 86_400_000 / 365
            rows.append(_iv_two_sided(inst, "P", t, 0.65 - sig_spread / 2,
                                      0.65 + sig_spread / 2))
        _write_day(tmp_path, day, [_rec(ts, rows)])
    return days


def test_summarize_end_to_end(_iso):
    days = _synth_days(_iso, n_days=2, per_day=3)
    res = os_.summarize(days=2, min_samples=3, progress=lambda *_: None)
    assert res["ok"] is True
    assert res["coverage"]["files"] == days
    assert res["coverage"]["used"] == 6
    assert res["coverage_ok"] is True
    assert res["families"] == [FAM]
    ov = res["overall"]["iv_spread_pts"]
    assert 14.0 < ov["median"] < 16.0
    labels = [g["label"] for g in res["by_delta"]]
    assert labels                                   # 分桶表非空
    assert any("×" in g["label"] for g in res["by_family_delta"])
    md = res["markdown"]
    assert "覆盖率" in md and "现模型" in md and "Δσ" in md


def test_coverage_reports_delta_source(_iso):
    days = _days(1)
    ts = _exp_ms() - int(3 * 86_400_000)
    t = (_exp_ms() - ts) / 86_400_000 / 365
    row = _row(f"{FAM}-260926-110-P", *[ _r for _r in
               (round(bs_price(SPOT, 110.0, t, 0.65 - 0.075, right="P"), 6),
                round(bs_price(SPOT, 110.0, t, 0.65 + 0.075, right="P"), 6))],
               dv=None)
    _write_day(_iso, days[0], [_rec(ts, [row])])
    res = os_.summarize(days=1, min_samples=1, progress=lambda *_: None)
    assert res["coverage"]["delta_src"] == {"bs": 1}
    assert res["coverage_ok"] is True
    assert "delta 来源" in res["markdown"]
    assert "BS 反算" in res["markdown"]


def test_summarize_filters_family(_iso):
    days = _synth_days(_iso, n_days=1)
    assert days
    res = os_.summarize(days=1, families=["BTC-USD_UM"], min_samples=1,
                        progress=lambda *_: None)
    assert res["coverage"]["used"] == 0
    assert res["coverage_ok"] is False
    assert "BTC-USD_UM" in res["markdown"]


def test_summarize_missing_files_gives_clear_report(_iso):
    res = os_.summarize(days=2, progress=lambda *_: None)
    assert res["ok"] is True and res["coverage"]["used"] == 0
    assert len(res["coverage"]["missing_files"]) == 2
    assert res["coverage_ok"] is False
    assert "覆盖率不足" in res["markdown"]
    assert any("盘口采集" in n for n in res["notes"])


def test_coverage_gate_marks_report(_iso):
    _synth_days(_iso, n_days=1)
    res = os_.summarize(days=1, min_samples=999, progress=lambda *_: None)
    assert res["coverage_ok"] is False
    assert "覆盖率不足" in res["markdown"]
    assert "不构成结论" in res["markdown"]


def test_days_clamped(_iso):
    res = os_.summarize(days=999, min_samples=1, progress=lambda *_: None)
    assert res["days"] == os_.MAX_DAYS


def test_cli_smoke(_iso, capsys):
    _synth_days(_iso, n_days=1)
    assert os_.main(["--days", "1", "--min-samples", "1"]) == 0
    out = capsys.readouterr().out
    assert "期权盘口价差画像" in out
    assert os_.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and "coverage" in payload


# ── 只读边界 ─────────────────────────────────────────────

def test_source_is_structurally_read_only():
    src = (Path(os_.__file__)).read_text(encoding="utf-8")
    for forbidden in ("set_order", "set_cancel_order", "save_option_params",
                      "execute_signal", "okx_options_broker", "portfolio"):
        assert forbidden not in src, f"只读工具不应引用 {forbidden}"
