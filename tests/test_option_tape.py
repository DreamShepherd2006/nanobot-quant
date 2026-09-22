"""option_tape（A 项：期权盘口只读采集器）单元测试 —— 全程不触网。

覆盖：参数校验/夹取、标的挑选（到期/带宽/每侧上限/已到期剔除）、
报价 0 → null、按天落盘 + 读取 + 统计、flatten 口径、runner 启停与配置变更、
以及**只读边界**（源码内不得出现下单/持仓调用）。
"""

from pathlib import Path

import pytest

from nanobot_quant import option_tape as tp
from nanobot_quant import okx_options_data as od
from nanobot_quant import okx_options_trade as ot


def _tk(inst, bid="1.2", ask="1.3", bsz="30", asz="20", mv="0.7", dv="-0.2"):
    return {"instId": inst, "bidPx": bid, "askPx": ask, "bidSz": bsz,
            "askSz": asz, "markVol": mv, "delta": dv}


@pytest.fixture
def _iso(tmp_path, monkeypatch):
    """隔离落盘目录 + 参数文件 + runner 全局状态。"""
    monkeypatch.setattr(tp, "tape_dir", lambda: tmp_path / tp.TAPE_DIR_NAME)
    monkeypatch.setattr(ot, "params_path", lambda: tmp_path / "okx_options_params.json")
    r = tp._runner()
    tp.stop()
    r._thread = None
    r._stop_event.clear()
    r._state["totals"] = {}
    r._state["last_error"] = None
    r._logged_start = False
    yield tmp_path
    tp.stop()


# ── 参数 ─────────────────────────────────────────────────

def test_config_default_when_absent(_iso):
    cfg = tp.tape_config()
    assert cfg["enabled"] is False           # 默认不擅自开采集
    assert cfg["families"] == ["SOL-USD_UM"]
    assert cfg["interval_s"] == 60


def test_config_clamps_and_filters(_iso):
    ot.save_option_params(tape={
        "enabled": 1, "interval_s": 5,          # < 下限 10
        "families": ["SOL-USD_UM", "NOT-A-FAMILY", ""],
        "expiries": 99, "band_pct": 999, "max_per_side": 0,
        "depth": "yes", "depth_levels": -3,
    })
    cfg = tp.tape_config()
    assert cfg["enabled"] is True
    assert cfg["interval_s"] == 10
    assert cfg["families"] == ["SOL-USD_UM"]  # 非法家族被剔除
    assert cfg["expiries"] == 10
    assert cfg["band_pct"] == 50.0
    assert cfg["max_per_side"] == 1
    assert cfg["depth"] is True
    assert cfg["depth_levels"] == 1


def test_config_bad_types_fall_back(_iso):
    ot.save_option_params(tape={"interval_s": "abc", "band_pct": None,
                                "expiries": "x", "families": 123})
    cfg = tp.tape_config()
    assert cfg["interval_s"] == 60 and cfg["expiries"] == 3
    assert cfg["band_pct"] == 12.0
    assert cfg["families"] == ["SOL-USD_UM"]


def test_save_tape_config_does_not_touch_live(_iso):
    ot.save_option_params(live={"enabled": True, "interval_s": 90})
    tp.save_tape_config(enabled=True, interval_s=30)
    p = ot.load_option_params()
    assert p["tape"]["enabled"] is True and p["tape"]["interval_s"] == 30
    assert p["live"]["interval_s"] == 90      # live 段原样保留


# ── 标的挑选 ──────────────────────────────────────────────

def test_pick_instruments_expiry_band_cap_and_expired():
    now_ms = 1_700_000_000_000
    spot = 100.0
    rows = [
        _tk("SOL-USD_UM-261231-100-P"),      # 第 3 个到期（103/128 档）—— 取决于排序
        _tk("SOL-USD_UM-261231-101-P"),
        _tk("SOL-USD_UM-270101-100-P"),      # 更远
        _tk("SOL-USD_UM-270101-100-C"),
        _tk("SOL-USD_UM-270101-130-P"),      # 带宽外（+30% > 12%）
        _tk("BTC-USD_UM-270101-100-P"),      # 别的家族
        _tk("SOL-USD_UM-199901-100-P"),      # 早过期
        _tk("NOT-A-BOND"),
    ]
    cfg = dict(tp.DEFAULT_TAPE, expiries=1, band_pct=12.0, max_per_side=2)
    picked = tp.pick_instruments(rows, "SOL-USD_UM", spot, cfg, now_ms)
    insts = [r["i"] for r in picked]
    # 最近到期档 = 261231；仅该档；带宽内；每侧上限 2
    assert insts == sorted(["SOL-USD_UM-261231-100-P", "SOL-USD_UM-261231-101-P"])
    assert all("BTC" not in i and "199901" not in i for i in insts)


def test_pick_instruments_spot_missing_keeps_all_in_band_off():
    now_ms = 1_700_000_000_000
    rows = [_tk("SOL-USD_UM-270101-500-P"), _tk("SOL-USD_UM-270101-1-C")]
    cfg = dict(tp.DEFAULT_TAPE, expiries=1, max_per_side=5)
    picked = tp.pick_instruments(rows, "SOL-USD_UM", None, cfg, now_ms)
    assert len(picked) == 2                   # spot 缺失 → 不做带宽裁剪（可容错）


def test_quote_zero_and_blank_become_null():
    now_ms = 1_700_000_000_000
    rows = [_tk("SOL-USD_UM-270101-100-P", bid="0", ask="", bsz="0", asz=""),
            _tk("SOL-USD_UM-270101-101-P", bid="1.5", ask="1.6")]
    cfg = dict(tp.DEFAULT_TAPE, expiries=1, max_per_side=5)
    picked = {r["i"]: r for r in tp.pick_instruments(rows, "SOL-USD_UM", 100.0, cfg, now_ms)}
    no_q = picked["SOL-USD_UM-270101-100-P"]
    assert no_q["b"] is None and no_q["a"] is None
    assert no_q["bs"] is None and no_q["as"] is None
    ok = picked["SOL-USD_UM-270101-101-P"]
    assert ok["b"] == 1.5 and ok["a"] == 1.6


# ── 采样 / 落盘 / 读取 ────────────────────────────────────

def _stub_market(monkeypatch, rows):
    class _M:
        def get_tickers(self, instType="OPTION"):
            assert instType == "OPTION"
            return rows
    monkeypatch.setattr(tp.okx_sdk, "market", lambda: _M())
    monkeypatch.setattr(tp.okx_sdk, "check", lambda payload: payload)


def _stub_spot(monkeypatch, mapping):
    monkeypatch.setattr(od, "spot_price", lambda fam: mapping.get(fam))


def test_sample_once_appends_daily_file_and_flatten(_iso, monkeypatch):
    _stub_market(monkeypatch, [_tk("SOL-USD_UM-270101-100-P", bid="2.0", ask="2.4"),
                               _tk("SOL-USD_UM-270101-100-C", bid="3.0", ask="3.2")])
    _stub_spot(monkeypatch, {"SOL-USD_UM": 100.0})
    rec = tp.sample_once(persist=True)
    assert rec["spot"] == {"SOL-USD_UM": 100.0}
    assert len(rec["rows"]) == 2
    p = tp.sample_path()
    assert p.exists() and p.name.startswith("tape_")
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    recs = tp.load_tape()
    assert len(recs) == 1 and recs[0]["ts_ms"] == rec["ts_ms"]
    flat = tp.flatten(recs)
    assert len(flat) == 2
    put = [r for r in flat if r["right"] == "P"][0]
    assert put["family"] == "SOL-USD_UM" and put["strike"] == 100.0
    assert put["mid"] == 2.2
    assert put["spread_pct"] == pytest.approx((2.4 - 2.0) / 2.2)
    assert put["mark_vol"] == 0.7 and put["delta"] == -0.2


def test_sample_once_persist_false_writes_nothing(_iso, monkeypatch):
    _stub_market(monkeypatch, [_tk("SOL-USD_UM-270101-100-P")])
    _stub_spot(monkeypatch, {"SOL-USD_UM": 100.0})
    tp.sample_once(persist=False)
    assert not tp.sample_path().exists()
    assert tp.load_tape() == []


def test_tape_stats_and_limit(_iso, monkeypatch):
    _stub_market(monkeypatch, [_tk("SOL-USD_UM-270101-100-P"),
                               _tk("SOL-USD_UM-270101-100-C")])
    _stub_spot(monkeypatch, {"SOL-USD_UM": 100.0})
    tp.sample_once(persist=True)
    tp.sample_once(persist=True)
    st = tp.tape_stats()
    assert st["samples"] == 2 and st["rows"] == 4 and st["bytes"] > 0
    assert st["first_ts"] and st["last_ts"]
    assert len(tp.load_tape(limit=1)) == 1
    assert tp.tape_stats(day="19990101")["samples"] == 0


def test_flatten_skips_unparseable_rows(_iso):
    recs = [{"ts": "t", "ts_ms": 1, "spot": {}, "rows": [
        {"i": "NOT-AN-OPTION", "b": 1, "a": 2},
        {"i": "SOL-USD_UM-270101-100-P", "b": 1, "a": 2}]}]
    flat = tp.flatten(recs)
    assert len(flat) == 1 and flat[0]["inst"].endswith("-P")


def test_load_tape_survives_broken_line(_iso):
    p = tp.sample_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"ts":"a","rows":[]}\n{broken json\n{"ts":"b","rows":[]}\n',
                 encoding="utf-8")
    assert [r["ts"] for r in tp.load_tape()] == ["a", "b"]


# ── Runner ────────────────────────────────────────────────

def test_runner_sync_start_stop(_iso, monkeypatch):
    monkeypatch.setattr(tp, "sample_once",
                        lambda cfg=None, persist=True: {"ts": "t", "rows": []})
    ot.save_option_params(tape={"enabled": False})
    tp.sync()
    assert tp.state()["running"] is False        # 未启用 → 不启动
    ot.save_option_params(tape={"enabled": True, "interval_s": 10})
    tp.sync()
    assert tp.state()["running"] is True
    tp.stop()
    assert tp.state()["running"] is False


def test_runner_config_changed_restarts(_iso, monkeypatch):
    calls = []
    monkeypatch.setattr(tp, "sample_once",
                        lambda cfg=None, persist=True: calls.append(1) or {"ts": "t", "rows": []})
    ot.save_option_params(tape={"enabled": True, "interval_s": 10})
    tp.sync()
    first = tp._runner()._thread
    assert first is not None and first.is_alive()
    ot.save_option_params(tape={"enabled": True, "interval_s": 11})
    tp.sync()                                    # 配置变化 → 重启线程
    assert tp._runner()._thread is not first
    tp.stop()
    assert calls                                 # 至少跑过一轮


def test_state_reports_totals_and_stats(_iso, monkeypatch):
    monkeypatch.setattr(tp, "sample_once",
                        lambda cfg=None, persist=True: {"ts": "t", "rows": [1, 2, 3]})
    ot.save_option_params(tape={"enabled": True, "interval_s": 10})
    tp.sync()
    import time
    for _ in range(50):
        if tp.state()["total_rows"] >= 3:
            break
        time.sleep(0.1)
    st = tp.state()
    assert st["total_samples"] >= 1 and st["total_rows"] >= 3
    assert st["stats"]["day"]


# ── 只读边界（结构性保证，不是注释）──────────────────────

def test_read_only_boundary_source_has_no_order_or_position_calls():
    src = Path(tp.__file__).read_text(encoding="utf-8")
    for forbidden in ("set_order", "set_cancel_order", "get_positions",
                      "set_margin_balance", "get_orders_pending", "trade_for",
                      "account_for", "settle_expired_puts"):
        assert forbidden not in src, f"采集器不得出现 {forbidden}（只读边界）"
