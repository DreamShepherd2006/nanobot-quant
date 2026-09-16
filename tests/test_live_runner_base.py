"""LiveRunnerBase 单测 —— daemon 生命周期 / 优雅停止 / sync 幂等 / 事件文件。"""

from __future__ import annotations

import json
import threading
import time

from nanobot_quant.live_runner_base import LiveRunnerBase


class _Stub(LiveRunnerBase):
    """最小子类：单轮只计数，可注入 sleep / 异常。"""

    LOG_PREFIX = "[TEST-RUNNER]"
    EVENTS_NAME = "test_events.jsonl"

    def __init__(self, tmp_path):
        super().__init__()
        self._tmp = tmp_path
        self.rounds = 0
        self.cfg = {"enabled": True, "interval_s": 10}
        self.round_sleep = 0.0
        self.raise_on_round = False
        self._first_round = threading.Event()

    # ── 钩子 ──
    def load_config(self):
        return dict(self.cfg)

    def config_changed(self, old, new):
        return old.get("interval_s") != new.get("interval_s")

    def do_round(self):
        self.rounds += 1
        self._first_round.set()
        if self.round_sleep:
            time.sleep(self.round_sleep)
        if self.raise_on_round:
            raise RuntimeError("boom")
        self._bump("rounds")
        return {"n": self.rounds}

    def storage_dir(self):
        return self._tmp


def test_start_runs_rounds(tmp_path):
    r = _Stub(tmp_path)
    assert r.start()["started"] is True
    assert r._first_round.wait(5.0), "首轮未执行"
    assert r.rounds >= 1
    assert r.status()["running"] is True
    r.stop()


def test_status_shape(tmp_path):
    r = _Stub(tmp_path)
    st = r.status()
    for k in ("running", "started_at", "last_run", "last_error", "last_result",
              "total_rounds", "totals", "interval_s", "thread_alive"):
        assert k in st, k


def test_round_exception_does_not_kill_loop(tmp_path):
    r = _Stub(tmp_path)
    r.raise_on_round = True
    r.start()
    assert r._first_round.wait(5.0)
    time.sleep(0.5)
    # 异常被兜住：记 last_error，线程仍在跑
    assert "boom" in (r.status()["last_error"] or "")
    assert r.status()["thread_alive"] is True
    r.stop()


def test_stop_waits_for_current_round(tmp_path):
    """优雅停止：不打断进行中的业务轮。"""
    r = _Stub(tmp_path)
    r.round_sleep = 1.5
    r.start()
    assert r._first_round.wait(5.0)
    t0 = time.time()
    r.stop()
    elapsed = time.time() - t0
    # 必须等到当前轮结束（≥ 剩余 sleep），而不是立刻返回
    assert elapsed >= 0.8, f"stop 未等待当前轮（elapsed={elapsed:.2f}）"
    assert r.status()["running"] is False


def test_sync_start_then_stop(tmp_path):
    r = _Stub(tmp_path)
    out = r.sync()
    assert out.get("started") is True
    assert r._first_round.wait(5.0)
    # 关掉 → 应停
    r.cfg["enabled"] = False
    out2 = r.sync()
    assert out2.get("stopped") is True
    assert r.status()["running"] is False


def test_sync_restarts_on_config_change(tmp_path):
    r = _Stub(tmp_path)
    r.start()
    assert r._first_round.wait(5.0)
    old_thread = r._thread
    r.cfg["interval_s"] = 30
    out = r.sync()
    assert out.get("started") is True
    assert r._thread is not old_thread, "配置变化应重启线程"
    r.stop()


def test_sync_idempotent_when_unchanged(tmp_path):
    r = _Stub(tmp_path)
    r.start()
    assert r._first_round.wait(5.0)
    out = r.sync()
    assert out.get("unchanged") is True
    r.stop()


def test_events_append_and_read(tmp_path):
    r = _Stub(tmp_path)
    r._append_event({"kind": "a", "n": 1})
    r._append_event({"kind": "b", "n": 2})
    events = r.load_events(limit=10)
    assert [e["kind"] for e in events] == ["a", "b"]
    assert all("ts" in e for e in events)
    # 落盘为 JSONL
    lines = (tmp_path / "test_events.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "a"


def test_load_events_missing_file(tmp_path):
    r = _Stub(tmp_path)
    assert r.load_events() == []


def test_bump_totals(tmp_path):
    r = _Stub(tmp_path)
    r._bump("x")
    r._bump("x", 3)
    assert r.status()["totals"]["x"] == 4


def test_interval_clamped(tmp_path):
    r = _Stub(tmp_path)
    assert r._interval_s({"interval_s": 1}) == 10        # 下限
    assert r._interval_s({"interval_s": 99999}) == 3600  # 上限
    assert r._interval_s({}) == 60                       # 默认
    assert r._interval_s({"interval_s": "bad"}) == 60    # 容错


def test_log_goes_to_stderr(tmp_path, capsys):
    r = _Stub(tmp_path)
    r._log("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.err
    assert captured.out == "", "绝不能 print 到 stdout（launch.sh 会 eval）"


def test_double_start_is_idempotent(tmp_path):
    r = _Stub(tmp_path)
    r.start()
    assert r._first_round.wait(5.0)
    out = r.start()
    assert out.get("started") is False
    r.stop()
