"""TD live 实时状态共享测试（td_live_state + 策略 _record 集成）。"""
import json
from pathlib import Path

import pytest

from nanobot_quant import td_live_state


@pytest.fixture(autouse=True)
def _fresh_state():
    td_live_state.LIVE_STATE["symbols"] = {}
    td_live_state.LIVE_STATE["running"] = False
    yield


def test_update_symbol_and_get_state():
    td_live_state.update_symbol("CRCLX", {
        "setup_buy": 3, "setup_sell": 7, "cd_sell": 13,
        "score": 6.2, "price": 66.9, "time": "01:30:00",
    })
    td_live_state.set_loop(True, next_iteration="01:31:00")
    st = td_live_state.get_state()
    assert st["running"] is True
    assert st["next_iteration"] == "01:31:00"
    s = st["symbols"]["CRCLX"]
    assert s["setup_buy"] == 3
    assert s["setup_sell"] == 7
    assert s["cd_sell"] == 13
    assert s["signal"] == "HOLD"  # 无动作时默认
    assert s["updated_at"]


def test_append_and_load_events(tmp_path: Path):
    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)
    td_live_state.append_event({
        "symbol": "CRCLX", "event": "LONG", "note": "slot=2 qty=0.0422",
        "price": 67.21, "score": 8.1,
    })
    td_live_state.append_event({
        "symbol": "SOL", "event": "EXIT_SKIP", "note": "链上余额为 0",
    })
    events = td_live_state.load_events(20)
    assert len(events) == 2
    assert events[0]["event"] == "LONG"
    assert events[0]["symbol"] == "CRCLX"
    assert events[1]["event"] == "EXIT_SKIP"
    # 文件确实落盘（重启可恢复）
    lines = ev_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "LONG"


def test_load_events_limit(tmp_path: Path):
    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)
    for i in range(5):
        td_live_state.append_event({"symbol": "X", "event": f"E{i}", "note": ""})
    events = td_live_state.load_events(3)
    assert [e["event"] for e in events] == ["E2", "E3", "E4"]


def test_events_path_fallback():
    # 无 /data、/mnt/workspace 时回退 home 路径（不抛异常）
    p = td_live_state.events_path()
    assert isinstance(p, Path)


def test_record_not_written_when_not_live(tmp_path: Path):
    """回测/纸交易（live_mode 未设）只更新内存、不写事件文件。"""
    from tests.test_batch_strategy import _make_batch_strategy, _make_bm, _bars_with, _buy_closes  # noqa: PLC0415

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)

    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s._record("LONG", "slot=1 qty=0.1")
    assert not ev_file.exists()  # live_mode=False → 不写文件
    # 但内存状态已更新（按策略自身 symbol 键）
    assert td_live_state.get_state()["symbols"][s.symbol]["signal"] == "LONG"


def test_record_written_when_live(tmp_path: Path):
    """live_mode=True 时信号事件写入文件。"""
    from tests.test_batch_strategy import _make_batch_strategy, _make_bm, _bars_with, _buy_closes  # noqa: PLC0415

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)

    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.parameters = {**s.parameters, "live_mode": True}
    s._record("LONG", "slot=1 qty=0.1")
    events = td_live_state.load_events(20)
    assert len(events) == 1
    assert events[0]["event"] == "LONG"
