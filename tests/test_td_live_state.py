"""TD live 实时状态共享测试（td_live_state + 策略 _record 集成）。"""
import json
from pathlib import Path

import pytest

from nanobot_quant import td_live_state


@pytest.fixture(autouse=True)
def _fresh_state():
    td_live_state.LIVE_STATE["symbols"] = {}
    td_live_state.LIVE_STATE["positions"] = {}
    td_live_state.LIVE_STATE["running"] = False
    yield


def test_set_positions_and_get_state():
    """持仓快照写入/读取（2026-08-22 实时监控持仓小节数据源）。"""
    td_live_state.set_positions("high", {
        "CRCLX": [{
            "symbol": "CRCLX", "slot": 2, "qty": 0.045,
            "entry_price": 87.99, "price": 88.65, "pnl_pct": 0.0075,
        }],
    })
    st = td_live_state.get_state()
    rows = st["positions"]["high"]["CRCLX"]
    assert rows[0]["slot"] == 2
    assert rows[0]["pnl_pct"] == 0.0075
    assert rows[0]["price"] == 88.65


def test_set_account_funds_and_get_state():
    """子账号资金快照写入/读取（2026-08-22 实时监控资金小表数据源）。"""
    td_live_state.set_account_funds("high", [
        {"slot": 1, "account": "gate_bot1", "uid": "59175220",
         "usdt_available": 3.98, "total_asset": 4.02},
        {"slot": 2, "account": "gate_bot2", "uid": "59175258",
         "usdt_available": 0.1, "total_asset": 4.05},
    ])
    st = td_live_state.get_state()
    funds = st["funds"]["high"]
    assert len(funds) == 2
    assert funds[0]["slot"] == 1
    assert funds[0]["account"] == "gate_bot1"
    assert funds[0]["usdt_available"] == 3.98
    assert funds[1]["total_asset"] == 4.05


def test_update_symbol_and_get_state():
    td_live_state.update_symbol("CRCLX", {
        "setup_buy": 3, "setup_sell": 7, "cd_sell": 13,
        "score": 6.2, "price": 66.9, "time": "01:30:00",
    })
    td_live_state.set_loop(True, next_iteration="01:31:00")
    st = td_live_state.get_state()
    assert st["running"] is True
    assert st["next_iteration"] == "01:31:00"
    # B1（2026-08-21）：symbols 按场景嵌套，scene 缺省归入 default
    s = st["symbols"]["default"]["CRCLX"]
    assert s["setup_buy"] == 3
    assert s["setup_sell"] == 7
    assert s["cd_sell"] == 13
    assert s["signal"] == "HOLD"  # 无动作时默认
    assert s["updated_at"]


def test_update_symbol_scene_isolation():
    """B1：同一标的不同场景互不覆盖（页面三场景分区的数据基础）。"""
    td_live_state.update_symbol("SOL", {"setup_buy": 2, "price": 89.1}, scene="low")
    td_live_state.update_symbol("SOL", {"setup_buy": 9, "price": 89.4}, scene="high")
    st = td_live_state.get_state()
    assert st["symbols"]["low"]["SOL"]["setup_buy"] == 2
    assert st["symbols"]["high"]["SOL"]["setup_buy"] == 9
    # 未指定场景 → default，与 high/low 互不干扰（get_state 为快照，需重取）
    td_live_state.update_symbol("SOL", {"setup_buy": 5})
    st = td_live_state.get_state()
    assert st["symbols"]["default"]["SOL"]["setup_buy"] == 5
    assert st["symbols"]["high"]["SOL"]["setup_buy"] == 9


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
    """回测/纸交易（live_mode 未设）不写事件文件、也不更新 LIVE_STATE。

    2026-08-28：回测曾无条件更新 LIVE_STATE（update_symbol），导致回测
    选择的标的覆盖实盘实时监控的标的/信号/持仓显示——现在回测完全隔离。
    """
    from tests.test_batch_strategy import _make_batch_strategy, _make_bm, _bars_with, _buy_closes  # noqa: PLC0415

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)

    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s._record("LONG", "slot=1 qty=0.1")
    assert not ev_file.exists()  # live_mode=False → 不写文件
    # 内存 LIVE_STATE 也不更新（回测不污染实盘监控）
    assert "default" not in td_live_state.get_state().get("symbols", {})


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


def test_record_extra_fields_written_when_live(tmp_path: Path):
    """成交事件携带 slot/qty/direction/status/tx_hash 结构化字段（方案 B）。

    2026-08-11：交易记录区块依赖这些字段呈现买卖成功/失败细节。
    """
    from tests.test_batch_strategy import _make_batch_strategy, _make_bm, _bars_with, _buy_closes  # noqa: PLC0415

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)

    bm = _make_bm(tmp_path)
    s = _make_batch_strategy(bm, _bars_with(_buy_closes()))
    s.parameters = {**s.parameters, "live_mode": True}
    s._record(
        "EXIT", "slot=2 qty=0.021226",
        slot=2, qty=0.021226, price=136.8, direction="sell", status="ok",
        tx_hash="4xKd9aBc...", chain="solana",
    )
    events = td_live_state.load_events(20)
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "EXIT"
    assert e["slot"] == 2
    assert e["qty"] == 0.021226
    assert e["direction"] == "sell"
    assert e["status"] == "ok"
    assert e["tx_hash"] == "4xKd9aBc..."
    assert e["chain"] == "solana"
