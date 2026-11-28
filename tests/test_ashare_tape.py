"""A 股期权盘口采集：新浪行解析 / 选池 / 时段 / 采样落盘（网络全部打桩）。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

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


# ── v2：合约自身报价时间 / 合约乘数 ──────────────────────

def test_flatten_keeps_contract_quote_ts_and_age():
    """``sample_ts`` 与 ``quote_ts`` 是两个字段：新浪 [32] 是合约自己的报价时间。"""
    rec = tape.parse_opt_line(REAL_LINES.split("\n")[1])   # [32] = 2026-11-20 15:00:00
    rec.update({"u": "510500.SH", "r": "P", "k": 6.5})
    ts_ms = int(datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc).timestamp() * 1000)
    f = tape.flatten([{"v": 2, "ts": "2026-11-20T07:00:00Z", "ts_ms": ts_ms,
                       "spot": {"510500.SH": 6.4}, "rows": [rec]}])[0]
    assert f["sample_ts"] == "2026-11-20T07:00:00Z" and f["ts"] == f["sample_ts"]
    assert f["quote_ts"] == "2026-11-20T07:00:00Z"          # CN 15:00 = UTC 07:00
    assert f["quote_ts_raw"] == "2026-11-20 15:00:00"
    assert f["quote_age_s"] == 0


def test_flatten_quote_ts_null_never_filled_with_sample_ts():
    """报价时间缺失/非法 → ``quote_ts=null``，绝不用采样时间顶替。"""
    for bad in ("", None, "15:00:00", "not-a-time"):
        rec = tape.parse_opt_line(REAL_LINES.split("\n")[0])
        rec.update({"u": "510050.SH", "r": "C", "k": 2.8, "ts": bad})
        f = tape.flatten([{"v": 2, "ts": "2026-11-20T07:00:00Z", "ts_ms": 1795160400000,
                           "spot": {"510050.SH": 2.9}, "rows": [rec]}])[0]
        assert f["quote_ts"] is None, bad
        assert f["quote_age_s"] is None, bad
        assert f["sample_ts"] == "2026-11-20T07:00:00Z"        # 采样时间仍在


def test_flatten_legacy_record_is_versioned_and_not_guessed():
    """旧文件没有 ``v``/乘数字段 → v=1 + ``mult_src=legacy``，不当成 missing 混同。"""
    rec = tape.parse_opt_line(REAL_LINES.split("\n")[0])
    rec.update({"u": "510050.SH", "r": "C", "k": 2.8})
    f = tape.flatten([{"ts": "2026-11-20T07:00:00Z", "ts_ms": 1,
                       "spot": {}, "rows": [rec]}])[0]
    assert f["v"] == 1
    assert f["mult"] is None and f["mult_src"] == "legacy"


def _write_units(tmp_path, rows):
    """在 ``{storage}/../ashare_data/qmt-option-1m/<run>/contracts_all.csv`` 放一份合约表。"""
    import csv as _csv
    d = tmp_path / "ashare_data" / "qmt-option-1m" / "run_test"
    d.mkdir(parents=True)
    p = d / "contracts_all.csv"
    cols = ["undl", "code", "month", "type", "strike", "expire", "mult",
            "tick", "open_date", "margin_unit", "settle_px", "opt_unit"]
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    return p


def test_sample_once_attaches_mult_from_contracts_all(tmp_path, monkeypatch):
    """乘数按合约代码 join 合约表 —— 同一到期档 10000/10180 不得混。"""
    storage = tmp_path / "credentials"
    storage.mkdir()
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(storage))
    monkeypatch.setattr(tape, "fetch_spot", lambda undls: {"510050.SH": 2.9})
    parsed = tape.parse_opt_line(REAL_LINES.split("\n")[0])
    monkeypatch.setattr(tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([dict(parsed)], 0))
    monkeypatch.setattr(ac, "contracts", lambda storage, force=False, today=None: {
        "items": [{"code": "10012437", "name": "x", "undl": "510050.SH",
                    "right": "C", "month": 11, "strike": 2.8, "expire": "20261125"}],
        "source": "cache", "fetched_at": 1, "error": ""})
    monkeypatch.setattr(tape, "now_cn",
                        lambda: datetime(2026, 11, 20, 15, 0, tzinfo=tape.CN))
    monkeypatch.setattr(tape, "_UNIT_CACHE", {"key": None, "units": None, "source": ""})
    _write_units(tmp_path, [{"undl": "510050.SH", "code": "10012437.SHO",
                             "month": 202612, "type": "C", "strike": 2.8,
                             "expire": 20261223, "mult": 10180, "tick": 0.0001,
                             "margin_unit": 4091.8, "opt_unit": 10000.0}])

    rec = tape.sample_once(tape.DEFAULT_TAPE, persist=False, force=True)
    assert rec["v"] == 2
    assert rec["mult_src"] == "run_test/contracts_all.csv"
    assert rec["mult_missing"] == 0
    row = rec["rows"][0]
    assert row["m"] == 10180 and row["ou"] == 10000.0 and row["ms"] == "contracts_all"
    assert row["mu"] == pytest.approx(4091.8)
    f = tape.flatten([rec])[0]
    assert f["mult"] == 10180 and f["mult_src"] == "contracts_all"
    assert f["opt_unit"] == 10000.0 and f["margin_unit"] == pytest.approx(4091.8)


def test_mult_missing_is_marked_never_default_10000(tmp_path, monkeypatch):
    """匹配不到合约表 → ``mult=null`` + ``missing`` 并计数，不静默用 10000。"""
    storage = tmp_path / "credentials"
    storage.mkdir()
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(storage))
    monkeypatch.setattr(tape, "fetch_spot", lambda undls: {"510050.SH": 2.9})
    parsed = tape.parse_opt_line(REAL_LINES.split("\n")[0])
    monkeypatch.setattr(tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([dict(parsed)], 0))
    monkeypatch.setattr(ac, "contracts", lambda storage, force=False, today=None: {
        "items": [{"code": "10012437", "name": "x", "undl": "510050.SH",
                    "right": "C", "month": 11, "strike": 2.8, "expire": "20261125"}],
        "source": "cache", "fetched_at": 1, "error": ""})
    monkeypatch.setattr(tape, "now_cn",
                        lambda: datetime(2026, 11, 20, 15, 0, tzinfo=tape.CN))
    monkeypatch.setattr(tape, "_UNIT_CACHE", {"key": None, "units": None, "source": ""})

    rec = tape.sample_once(tape.DEFAULT_TAPE, persist=False, force=True)
    assert rec["mult_error"] == "no_contracts_all"
    assert rec["mult_missing"] == 1
    row = rec["rows"][0]
    assert row["m"] is None and row["ou"] is None and row["ms"] == "missing"
    f = tape.flatten([rec])[0]
    assert f["mult"] is None and f["mult_src"] == "missing"


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


def test_pool_excludes_588080(tmp_path, monkeypatch):
    """588080 流动性太薄（整天零成交）⇒ 永不入采集池，即使手改配置文件。"""
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(tmp_path))
    assert "588080.SH" not in tape.POOL_CODES
    assert len(tape.POOL_CODES) == 4
    cfg = tape.save_tape_config(underlyings=["510050.SH", "588080.SH"])
    assert cfg["underlyings"] == ["510050.SH"]


def test_pool_underlyings_have_display_names():
    names = dict(tape.POOL_UNDERLYINGS)
    assert names["510050.SH"] == "上证50ETF"
    assert all(v for v in names.values())


# ── v2 诊断可见性（2026-11-28：乘数源选错 + handler 吞诊断字段） ─────────────────

def test_contract_unit_sources_prefers_newest_run_by_name(tmp_path, monkeypatch):
    """取源按 run 目录名（含时间戳）降序，**不看 mtime**。

    实测事故：run_20261120 那份缺 opt_unit/margin_unit 列的老表 mtime 反而最新
    ⇒ ou/mu 全 null、ms 却是 contracts_all（看起来“挂上了”其实字段全丢）。
    """
    import csv as _csv
    import os

    storage = tmp_path / "credentials"
    storage.mkdir()
    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(storage))
    root = tmp_path / "ashare_data" / "qmt-option-1m"

    def _mk(run, cols, mtime):
        d = root / run
        d.mkdir(parents=True)
        p = d / "contracts_all.csv"
        with open(p, "w", encoding="utf-8", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({k: "" for k in cols})
        os.utime(p, (mtime, mtime))
        return p

    _mk("run_20261120_012436", ["undl", "code", "mult"], 4_000_000_000)        # 老表 / mtime 最新
    _mk("run_20261127_220003_p1120",
        ["undl", "code", "mult", "opt_unit", "margin_unit"], 1_000_000_000)

    src = tape.contract_unit_sources()
    assert src, "应能找到 contracts_all.csv"
    assert src[0].parent.name == "run_20261127_220003_p1120"


def test_quote_meta_never_backfills_sample_time():
    """解析不了行情行返回 (None, None)；能解析则按 CN 时区换算成 UTC 并算陈旧秒。"""
    qt, age = tape.quote_meta("2026-11-27 00:00:00", 1_700_000_000_000)
    assert qt == "2026-11-26T16:00:00Z"            # 北京时间 00:00 = UTC 前一日 16:00
    assert age == 1_700_000_000 - int(
        datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc).timestamp())
    assert tape.quote_meta("", 1_700_000_000_000) == (None, None)
    assert tape.quote_meta("garbage", None) == (None, None)


def test_sample_endpoint_exposes_diagnostics(tmp_path, monkeypatch):
    """采样端点必须把 v / mult_src / mult_missing / qt / qa 真正下发。

    历史缺陷：sample_once 算好了这些键，handler 只挑部分字段 ⇒ 页面无法判断
    装了哪版、选了哪份合约表，只能靠推断（静默降级不可接受）。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nanobot_quant import ashare_handlers as ah

    monkeypatch.setattr(tape.creg, "_get_storage_dir", lambda: str(tmp_path))
    rec = {
        "v": 2,
        "ts": "2026-11-20T07:00:00Z",
        "ts_ms": 1_700_000_000_000,
        "spot": {"510050.SH": 2.9},
        "picked": 1,
        "rows": [{"i": "10011425.SHO", "ts": "2026-11-27 00:00:00",
                  "m": 10000, "ou": 10000.0, "mu": 4091.8,
                  "ms": "contracts_all"}],
        "mult_src": "run_20261127_220003_p1120/contracts_all.csv",
        "mult_missing": 0,
        "mult_error": "",
        "skipped": "",
        "error": "",
    }
    monkeypatch.setattr(tape, "sample_now", lambda persist=False: rec)
    monkeypatch.setattr(ah, "_authorized", lambda req, gk: (None, True))

    app = FastAPI()
    ah.register_ashare_routes(app, object())
    j = TestClient(app).post("/config/ashare/sample", json={"persist": False}).json()
    assert j["v"] == 2
    assert j["mult_src"] == "run_20261127_220003_p1120/contracts_all.csv"
    assert j["mult_missing"] == 0 and j["mult_error"] == ""
    row = j["preview"][0]
    assert row["qt"] == "2026-11-26T16:00:00Z" and row["qa"] is not None
