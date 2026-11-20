"""A 股期权盘口采集：新浪行解析 / 选池 / 时段 / 采样落盘（网络全部打桩）。"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from nanobot_quant.ashare import contracts as ac
from nanobot_quant.ashare import tape

# 2026-11-20 15:00 实抓（收盘后快照）。注意第二个合约的 [2]（卖一）残留了买一价，
# 必须从五档数组取最优档——这正是 parse_opt_line 的设计约束。
REAL_LINES = (
    'var hq_str_CON_OP_10012437="2,0.0662,0.0666,0.0666,163,24162,-19.76,2.8000,0.0754,'
    '0.0660,0.3713,0.0001,0.0690,14,0.0678,1,0.0674,1,0.0670,1,0.0666,163,0.0662,2,'
    '0.0660,50,0.0658,5,0.0656,8,0.0652,1,2026-11-20 00:00:00,0,E 00,EBS,510050,'
    '50ETF购11月2800,55.17,0.0747,0.0331,92178,44359163.00,M,0.0830,C,2026-11-25,5,2,'
    '0.1,-0.0334";\n'
    'var hq_str_CON_OP_10012550="9,0.4975,0.4975,0.5054,1,38906,90.61,6.5000,0.2610,'
    '0.3300,0.8896,0.0001,0.5138,1,0.5123,1,0.5092,2,0.5063,1,0.5054,1,0.4975,9,'
    '0.4971,6,0.4968,10,0.4963,1,0.4950,5,2026-11-20 15:00:00,0,E 00,EBS,510500,'
    '500ETF沽11月6500,70.92,0.5151,0.3300,73410,308929469.00,M,0.2610,P,2026-11-25,5,2,'
    '0.447,0.0505";'
)


# ── 解析 ────────────────────────────────────────────────────

def test_parse_opt_line_basic_fields():
    r = tape.parse_opt_line(REAL_LINES.split("\n")[0])
    assert r["i"] == "10012437.SHO"
    assert r["b"] == 0.0662 and r["a"] == 0.0666        # 买一/卖一（取五档首档）
    assert r["bs"] == 2 and r["as"] == 163
    assert r["iv"] == pytest.approx(0.3713)
    assert r["oi"] == 24162
    assert r["vol"] == 92178
    assert r["e"] == "20261125"
    assert len(r["bids"]) == 5 and len(r["asks"]) == 5
    assert r["bids"][0] == [0.0662, 2] and r["bids"][4] == [0.0652, 1]   # 买一 → 买五
    assert r["asks"][0] == [0.0666, 163] and r["asks"][4] == [0.069, 14]  # 卖一 → 卖五


def test_parse_opt_line_ignores_stale_top_of_book():
    """[2] 是旧值时必须以五档数组为准（500ETF 沽 6500 实测）。"""
    r = tape.parse_opt_line(REAL_LINES.split("\n")[1])
    assert r["b"] == 0.4975
    assert r["a"] == 0.5054          # 而不是 [2] 的 0.4975
    assert r["asks"][0][0] == 0.5054


@pytest.mark.parametrize("line", ["", "var hq_str_CON_OP_x=\"\";", "garbage", "var hq_str_X=\"a,b\";"])
def test_parse_opt_line_rejects_bad_input(line):
    assert tape.parse_opt_line(line) is None


def test_parse_opt_line_missing_quote_becomes_none():
    """无买盘（全 0）→ b 记 None，绝不记 0 价（下游会把它当可成交）。"""
    line = REAL_LINES.split("\n")[0].replace(
        "0.0662,2,0.0660,50,0.0658,5,0.0656,8,0.0652,1", "0,0,0,0,0,0,0,0,0,0")
    r = tape.parse_opt_line(line)
    assert r["b"] is None and r["bs"] == 0


def test_flatten_adds_spread():
    rec = tape.parse_opt_line(REAL_LINES.split("\n")[0])
    rec.update({"u": "510050.SH", "r": "C", "k": 2.8})
    rows = tape.flatten([{"ts": "2026-11-20T07:00:00Z", "ts_ms": 1,
                          "spot": {"510050.SH": 2.9}, "rows": [rec]}])
    f = rows[0]
    assert f["mid"] == pytest.approx(0.0664)
    assert f["spread_pct"] == pytest.approx((0.0666 - 0.0662) / 0.0664, rel=1e-4)


# ── 选池 ────────────────────────────────────────────────────

def _contract(code, undl, right, strike, expire="20261125"):
    return {"code": code, "undl": undl, "right": right, "strike": strike, "expire": expire}


def test_pick_contracts_band_and_per_side_cap():
    items = [_contract("1", "510050.SH", "P", 2.8),
             _contract("2", "510050.SH", "P", 2.7),
             _contract("3", "510050.SH", "P", 2.6),
             _contract("4", "510050.SH", "P", 3.4),      # 3.4/2.9 = +17% → 出带
             _contract("5", "510050.SH", "P", 2.6, "20270623")]  # 第二个到期档
    cfg = {"underlyings": ["510050.SH"], "expiries": 3, "band_pct": 12.0, "max_per_side": 2}
    picked = tape.pick_contracts(items, cfg, {"510050.SH": 2.9}, date(2026, 11, 20))
    codes = [c["code"] for c in picked]
    assert "4" not in codes                     # 超出 ±12%
    # 每个 (到期, 方向) 只取最贴近现价的 2 个
    assert codes.count("1") == 1 and "2" in codes and "3" not in codes
    assert "5" in codes


def test_pick_contracts_expiries_limit():
    items = [_contract("1", "510050.SH", "P", 2.8, "20261125"),
             _contract("2", "510050.SH", "P", 2.8, "20261223"),
             _contract("3", "510050.SH", "P", 2.8, "20270324")]
    cfg = {"underlyings": ["510050.SH"], "expiries": 2, "band_pct": 12.0, "max_per_side": 5}
    picked = tape.pick_contracts(items, cfg, {"510050.SH": 2.9}, date(2026, 11, 20))
    assert [c["code"] for c in picked] == ["1", "2"]   # 第三个到期档被截掉


def test_pick_contracts_without_spot_skips_band():
    items = [_contract("1", "510050.SH", "P", 2.8), _contract("2", "510050.SH", "P", 9.9)]
    cfg = {"underlyings": ["510050.SH"], "expiries": 3, "band_pct": 12.0, "max_per_side": 5}
    picked = tape.pick_contracts(items, cfg, {}, date(2026, 11, 20))
    assert {c["code"] for c in picked} == {"1", "2"}   # 无现货时不做带宽裁剪


# ── 时段 ────────────────────────────────────────────────────

@pytest.mark.parametrize("dt,expect", [
    (datetime(2026, 11, 20, 9, 29, tzinfo=tape.CN), False),
    (datetime(2026, 11, 20, 9, 30, tzinfo=tape.CN), True),
    (datetime(2026, 11, 20, 11, 30, tzinfo=tape.CN), True),
    (datetime(2026, 11, 20, 12, 0, tzinfo=tape.CN), False),
    (datetime(2026, 11, 20, 13, 0, tzinfo=tape.CN), True),
    (datetime(2026, 11, 20, 15, 1, tzinfo=tape.CN), True),
    (datetime(2026, 11, 20, 15, 3, tzinfo=tape.CN), False),
    (datetime(2026, 11, 21, 10, 0, tzinfo=tape.CN), False),   # 周六
])
def test_in_session(dt, expect):
    assert tape.in_session(dt) is expect


# ── 采样（网络打桩） ────────────────────────────────────────

def _patch_env(monkeypatch, tmp_path, rows_line=REAL_LINES.split("\n")[0],
               ts="2026-11-20 07:00:00"):
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tape, "fetch_spot", lambda undls: {"510050.SH": 2.9})
    parsed = tape.parse_opt_line(rows_line)
    parsed["ts"] = ts
    monkeypatch.setattr(tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([dict(parsed)], 0))
    monkeypatch.setattr(ac, "contracts", lambda storage, force=False, today=None: {
        "items": [{"code": "10012437", "name": "50ETF购11月2800",
                   "undl": "510050.SH", "right": "C", "month": 11,
                   "strike": 2.8, "expire": "20261125"}],
        "source": "cache", "fetched_at": 1, "error": ""})
    monkeypatch.setattr(tape, "now_cn",
                        lambda: datetime(2026, 11, 20, 15, 0, tzinfo=tape.CN))


def test_sample_once_persists_and_enriches(tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    rec = tape.sample_once(tape.DEFAULT_TAPE, persist=True,
                           force=True)
    assert rec["skipped"] == ""
    assert rec["picked"] == 1 and len(rec["rows"]) == 1
    row = rec["rows"][0]
    assert row["u"] == "510050.SH" and row["r"] == "C" and row["k"] == 2.8

    p = tape.sample_path(datetime(2026, 11, 20, 15, 0, tzinfo=tape.CN).strftime("%Y%m%d"))
    assert p.exists(), "落盘文件应存在"
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines[-1]["rows"][0]["i"] == "10012437.SHO"
    st = tape.tape_stats(p.stem.split("_")[-1])
    assert st["samples"] == 1 and st["rows"] == 1


def test_sample_once_skips_off_session(tmp_path, monkeypatch):
    _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tape, "now_cn",
                        lambda: datetime(2026, 11, 20, 20, 0, tzinfo=tape.CN))
    rec = tape.sample_once(tape.DEFAULT_TAPE, persist=True)     # 非 force
    assert rec["skipped"] == "off_session"
    assert rec["rows"] == []


def test_sample_once_skips_stale_snapshot(tmp_path, monkeypatch):
    """节假日/周末新浪返回上一交易日快照 → 不落盘。"""
    _patch_env(monkeypatch, tmp_path, ts="2026-11-14 15:00:00")
    rec = tape.sample_once(tape.DEFAULT_TAPE, persist=True)
    assert rec["skipped"] == "stale"
    assert rec["rows"] == []
    assert not tape.sample_path().exists()


def test_save_and_load_config_clamps_and_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(tmp_path))
    cfg = tape.save_tape_config(enabled=True, interval_s=1, expiries=99,
                                band_pct=999, max_per_side=0,
                                underlyings=["510050.SH", "不存在的标的"])
    assert cfg["enabled"] is True
    assert cfg["interval_s"] == tape.INTERVAL_RANGE[0]
    assert cfg["expiries"] == 6
    assert cfg["band_pct"] == 30.0
    assert cfg["max_per_side"] == 1
    assert cfg["underlyings"] == ["510050.SH"]
    assert tape.tape_config() == cfg


def test_save_config_keeps_defaults_when_underlyings_all_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(tmp_path))
    cfg = tape.save_tape_config(underlyings=["NOPE"])
    assert cfg["underlyings"] == tape.DEFAULT_TAPE["underlyings"]
