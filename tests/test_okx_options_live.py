"""okx_options_live 单元测试（C23 S3 到期巡检 daemon，不触网）。

mock ot.settle_expired_puts / 凭证存储路径，验证：
单轮巡检 run_once（事件落盘 + LIVE_STATE）、异常不外抛、
配置存取与规范化、线程启停/重启（sync）、事件文件尾部读取。
"""

import time

import pytest

from nanobot_quant import okx_options_live as ol
from nanobot_quant import okx_options_trade as ot


@pytest.fixture
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "params_path", lambda: tmp_path / "okx_options_params.json")
    monkeypatch.setattr(ol, "_storage_dir", lambda: tmp_path)
    # 重置模块级全局状态（事件文件/线程隔离由 monkeypatch+stop 负责，_state 需显式复位）
    ol.stop()
    ol._state.update(running=False, last_run=None, last_settled=[],
                     last_error="", total_settled=0)
    ol._thread = None
    ol._stop.clear()
    yield tmp_path
    ol.stop()  # 保证测试结束无 daemon 线程泄漏
    ol._state.update(running=False, last_run=None, last_settled=[],
                     last_error="", total_settled=0)


# ── 配置 ─────────────────────────────────────────────────

def test_live_config_default(_iso):
    cfg = ol.live_config()
    assert cfg == {"enabled": False, "interval_s": ol.DEFAULT_INTERVAL_S}


def test_save_live_config_persists_and_clamps(_iso):
    ol.save_live_config(enabled=True, interval_s=30)
    assert ol.live_config() == {"enabled": True, "interval_s": 30}
    # 越界值 clamp
    ol.save_live_config(interval_s=999999)
    assert ol.live_config()["interval_s"] == ol.MAX_INTERVAL_S
    ol.save_live_config(interval_s=1)
    assert ol.live_config()["interval_s"] == ol.MIN_INTERVAL_S
    # 非法类型忽略
    ol.save_live_config(interval_s="abc")
    assert ol.live_config()["interval_s"] == ol.MIN_INTERVAL_S
    # 只改 interval 不动 enabled
    ol.save_live_config(interval_s=45)
    assert ol.live_config() == {"enabled": True, "interval_s": 45}
    # collateral 字段保留
    assert ot.load_option_params().get("collateral_ratio_pct") == ot.DEFAULT_COLLATERAL_RATIO_PCT


# ── 单轮巡检 run_once ────────────────────────────────────

def _settled_one(**kw):
    d = {"id": "evt1", "inst_id": "SOL-USD_UM-260906-101-P",
         "status": ot.STATUS_SETTLED_OTM, "settle_px": 105.0,
         "settle_pnl": 0.007, "note": ""}
    d.update(kw)
    return d


def test_run_once_appends_event_and_state(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one()])
    res = ol.run_once()
    assert res["error"] == ""
    assert res["settled"][0]["status"] == ot.STATUS_SETTLED_OTM
    st = ol.live_state()
    assert st["last_run"] and st["total_settled"] == 1
    evs = ol.load_events(10)
    assert len(evs) == 1
    assert evs[0]["type"] == "settle"
    assert evs[0]["inst_id"] == "SOL-USD_UM-260906-101-P"
    assert evs[0]["status"] == ot.STATUS_SETTLED_OTM
    assert evs[0]["settle_px"] == pytest.approx(105.0)


def test_run_once_no_settle_no_event(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [])
    res = ol.run_once()
    assert res == {"settled": [], "error": ""}
    assert ol.load_events(10) == []
    assert ol.live_state()["total_settled"] == 0


def test_run_once_error_swallowed_to_state(_iso, monkeypatch):
    def boom():
        raise RuntimeError("OKX 500")
    monkeypatch.setattr(ot, "settle_expired_puts", boom)
    res = ol.run_once()  # 不外抛
    assert res["error"].startswith("RuntimeError: OKX 500")
    assert ol.live_state()["last_error"].startswith("RuntimeError:")
    assert ol.live_state()["total_settled"] == 0


def test_load_events_tail_limit(_iso, monkeypatch):
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one(id=f"e{i}") for i in range(3)])
    ol.run_once()
    ol.run_once()
    # 2 轮 × 3 笔 = 6 条事件
    assert len(ol.load_events(50)) == 6
    # tail limit 只取最近 N 条（倒序）
    tail = ol.load_events(2)
    assert len(tail) == 2


def _settled_itm_one(**kw):
    d = {"id": "evt-itm", "inst_id": "SOL-USD_UM-260907-106-P",
         "status": ot.STATUS_SETTLED_ITM, "settle_px": 104.445181,
         "settle_pnl": -0.0644, "settle_payout": 0.1555, "note": ""}
    d.update(kw)
    return d


def test_load_events_enrich_covered(_iso, monkeypatch):
    """settled_itm 判定行 enrich covered：台账存在同现货对 filled spot_cover 即已补买。"""
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [_settled_itm_one()])
    monkeypatch.setattr(ot, "load_ledger", lambda: [
        {"kind": "spot_cover", "inst_id": "SOL-USD", "status": "filled"}])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and evs[0]["status"] == ot.STATUS_SETTLED_ITM
    assert evs[0]["covered"] is True


def test_load_events_itm_without_cover_not_covered(_iso, monkeypatch):
    """无对应现货补买时 settled_itm 行 covered=False（前端保留「补买」入口）。"""
    monkeypatch.setattr(ot, "settle_expired_puts", lambda: [_settled_itm_one()])
    monkeypatch.setattr(ot, "load_ledger", lambda: [])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and evs[0]["status"] == ot.STATUS_SETTLED_ITM
    assert evs[0].get("covered") is False


def test_load_events_otm_untouched(_iso, monkeypatch):
    """OTM 事件不加 covered 字段（无补买概念）。"""
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one(status=ot.STATUS_SETTLED_OTM)])
    ol.run_once()
    evs = ol.load_events(10)
    assert evs and "covered" not in evs[0]


# ── 线程生命周期 sync ────────────────────────────────────

def test_sync_start_stop(_iso, monkeypatch):
    # 缩短心跳避免测试挂起——直接 patch interval 后启动
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    st = ol.sync()
    assert st["running"] is True
    # 同一配置再 sync = 幂等不重启
    assert ol.sync()["running"] is True
    ol.save_live_config(enabled=False)
    st = ol.sync()
    assert st["running"] is False
    assert ol.live_state()["running"] is False


def test_sync_interval_change_restarts(_iso, monkeypatch):
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    assert ol.sync()["running"] is True
    ol.save_live_config(interval_s=15)
    st = ol.sync()
    assert st["running"] is True
    assert st["config"]["interval_s"] == 15
    ol.save_live_config(enabled=False)
    ol.sync()
    assert ol.live_state()["running"] is False


def test_daemon_runs_periodic_settle(_iso, monkeypatch):
    """真实线程跑一轮：mock settle 返回一笔 → 事件文件落盘。"""
    monkeypatch.setattr(ot, "settle_expired_puts",
                        lambda: [_settled_one()])
    monkeypatch.setattr(ol, "MIN_INTERVAL_S", 1)
    ol.save_live_config(enabled=True, interval_s=1)
    ol.sync()
    assert ol.live_state()["running"] is True
    # 等线程跑完至少一轮（interval=1s，最多等 5s）
    deadline = time.time() + 5
    while ol.live_state()["total_settled"] < 1 and time.time() < deadline:
        time.sleep(0.1)
    assert ol.live_state()["total_settled"] >= 1
    assert len(ol.load_events(10)) >= 1
    ol.stop()
    assert ol.live_state()["running"] is False


def test_stop_idempotent(_iso):
    ol.stop()
    ol.stop()
    assert ol.live_state()["running"] is False
