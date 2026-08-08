"""P1 skeleton tests — execution_mode="loop" (docs/quant-system.md §15.5.1).

覆盖:
- exec_params.execution_mode 默认值 / 枚举校验 / WebUI 保存时保留
- SignalExecutionStrategy 队列消费与结果记录（mock run_from_signals）
- execute_signal 在 loop 模式下入队返回 queued（direct 行为不变由既有测试覆盖）
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


# ── exec_params.execution_mode ───────────────────────────────────────────

def test_execution_mode_default_direct():
    from nanobot_quant.exec_params import DEFAULT_EXEC_PARAMS

    assert DEFAULT_EXEC_PARAMS["execution_mode"] == "direct"


def test_validate_execution_mode_enum():
    from nanobot_quant.exec_params import validate_exec_param

    assert validate_exec_param("execution_mode", "direct") is None
    assert validate_exec_param("execution_mode", "loop") is None
    assert validate_exec_param("execution_mode", "sideways") is not None
    assert validate_exec_param("execution_mode", 1) is not None


def test_load_execution_mode_from_file(tmp_path):
    from nanobot_quant import exec_params as ep

    target = tmp_path / "exec_params.json"
    target.write_text(json.dumps({"execution_mode": "loop"}), encoding="utf-8")
    with mock.patch.object(ep, "exec_params_path", return_value=target):
        loaded = ep.load_exec_params()
    assert loaded["execution_mode"] == "loop"
    # 其余参数仍为默认
    assert loaded["max_position_pct"] == 0.20


def test_save_preserves_execution_mode(tmp_path):
    """WebUI 只提交 5 个数值参数，execution_mode 不得被重置为 direct。"""
    from nanobot_quant import exec_params as ep

    target = tmp_path / "exec_params.json"
    with mock.patch.object(ep, "exec_params_path", return_value=target):
        # 首次保存：带 loop 模式
        res = ep.save_exec_params({"max_position_pct": 0.25, "execution_mode": "loop"})
        assert res["ok"] and res["params"]["execution_mode"] == "loop"
        # 第二次 WebUI 保存（只提交 5 个数值参数）→ loop 保留
        res2 = ep.save_exec_params({"slippage": 0.02})
        assert res2["ok"] and res2["params"]["execution_mode"] == "loop"
        # 非法 mode 拒绝
        res3 = ep.save_exec_params({"execution_mode": "sideways"})
        assert not res3["ok"]


def test_reset_returns_default_mode(tmp_path):
    from nanobot_quant import exec_params as ep

    target = tmp_path / "exec_params.json"
    with mock.patch.object(ep, "exec_params_path", return_value=target):
        ep.save_exec_params({"execution_mode": "loop"})
        res = ep.save_exec_params({"reset": True})
        assert res["ok"] and res["params"]["execution_mode"] == "direct"


# ── SignalExecutionStrategy 队列消费 ──────────────────────────────────────

def test_strategy_consumes_queue_and_records_outcome():
    from nanobot_quant.execution_loop import SignalExecutionStrategy

    s = SignalExecutionStrategy()
    assert s.stats() == {"queued": 0, "processed": 0, "failed": 0}

    order_id = s.enqueue_signal({"ticker": "SOL"}, {"portfolio_value": 100.0})
    assert order_id.startswith("loop-")
    assert s.stats()["queued"] == 1
    assert s.get_outcome(order_id) is None  # 尚未消费

    fake_result = {"ticker": "SOL", "risk_passed": True, "tx_hash": "abc123"}
    with mock.patch(
        "nanobot_quant.pipeline.run_from_signals", return_value=[fake_result]
    ) as rf:
        s._drain()

    rf.assert_called_once()
    assert rf.call_args.args[0] == [{"ticker": "SOL"}]  # signal_list 透传
    assert rf.call_args.kwargs == {"live": True, "portfolio_value": 100.0}
    assert s.get_outcome(order_id) == fake_result
    assert s.stats() == {"queued": 1, "processed": 1, "failed": 0}


def test_strategy_handles_exception_keeps_loop_alive():
    from nanobot_quant.execution_loop import SignalExecutionStrategy

    s = SignalExecutionStrategy()
    order_id = s.enqueue_signal({"ticker": "BAD"}, {})

    with mock.patch(
        "nanobot_quant.pipeline.run_from_signals",
        side_effect=RuntimeError("swap failed"),
    ):
        s._drain()  # 不得抛出

    out = s.get_outcome(order_id)
    assert out is not None and "error" in out and "swap failed" in out["error"]
    assert s.stats()["failed"] == 1


def test_strategy_empty_queue_is_noop():
    from nanobot_quant.execution_loop import SignalExecutionStrategy

    s = SignalExecutionStrategy()
    with mock.patch(
        "nanobot_quant.pipeline.run_from_signals"
    ) as rf:
        s._drain()
    rf.assert_not_called()


# ── loop 周期（loop_interval_seconds → _current_interval）────────────────

def test_current_interval_from_exec_params():
    """_current_interval() 从 exec_params.json 读取循环周期。"""
    from nanobot_quant import execution_loop
    from nanobot_quant import exec_params as ep

    with mock.patch.object(
        ep, "load_exec_params",
        return_value={"loop_interval_seconds": 10},
    ):
        assert execution_loop._current_interval() == 10


def test_current_interval_default_5s():
    from nanobot_quant import execution_loop
    from nanobot_quant import exec_params as ep

    with mock.patch.object(
        ep, "load_exec_params",
        return_value={},
    ):
        assert execution_loop._current_interval() == 5


def test_worker_drains_queue_after_sleep():
    """_worker 每轮 sleep 后 drain 消费队列；周期每次循环重新读取。"""
    from nanobot_quant import execution_loop
    from nanobot_quant import exec_params as ep

    s = execution_loop.SignalExecutionStrategy()
    order_id = s.enqueue_signal({"ticker": "SOL"}, {})

    with mock.patch.object(
        ep, "load_exec_params",
        return_value={"loop_interval_seconds": 30},
    ), mock.patch(
        "nanobot_quant.pipeline.run_from_signals",
        return_value=[{"ticker": "SOL", "risk_passed": True}],
    ), mock.patch(
        "time.sleep", side_effect=[None, KeyboardInterrupt]
    ), mock.patch.object(
        execution_loop, "_current_interval", return_value=30
    ):
        try:
            execution_loop._worker(s)
        except KeyboardInterrupt:
            pass

    assert s.get_outcome(order_id)["risk_passed"] is True
    assert s.stats()["processed"] == 1


# ── execute_signal loop 分叉 ─────────────────────────────────────────────

def test_execute_signal_loop_mode_queues_instead_of_direct_call():
    from nanobot_quant.tools import tools_execute

    signal_json = json.dumps(
        {"ticker": "SOL", "recommendation": "BUY", "confidence": 0.8}
    )
    with mock.patch.object(
        tools_execute, "_read_webui_live", return_value=True
    ), mock.patch.object(
        tools_execute, "_load_tokens", return_value=[]
    ), mock.patch(
        "nanobot_quant.onchainos_cli.resolve_token",
        return_value={"ok": True, "address": "So11111111111111111111111111111111111111112"},
    ), mock.patch(
        "nanobot_quant.exec_params.load_exec_params",
        return_value={"execution_mode": "loop"},
    ), mock.patch(
        "nanobot_quant.execution_loop.enqueue_signal", return_value="loop-123"
    ) as enq, mock.patch(
        "nanobot_quant.pipeline.run_from_signals"
    ) as rf:
        result = tools_execute.execute_signal(signal_json, live=True)

    assert result["queued"] is True
    assert result["mode"] == "loop"
    assert result["order_id"] == "loop-123"
    enq.assert_called_once()
    rf.assert_not_called()  # direct 直调未发生


def test_execute_signal_direct_mode_unchanged():
    """execution_mode=direct（默认）时仍走 run_from_signals 直调。"""
    from nanobot_quant.tools import tools_execute

    signal_json = json.dumps({"ticker": "SOL", "recommendation": "BUY"})
    fake_results = [{"ticker": "SOL", "recommendation": "BUY", "risk_passed": True}]
    with mock.patch.object(
        tools_execute, "_read_webui_live", return_value=True
    ), mock.patch.object(
        tools_execute, "_load_tokens", return_value=[]
    ), mock.patch(
        "nanobot_quant.onchainos_cli.resolve_token",
        return_value={"ok": True, "address": "So11111111111111111111111111111111111111112"},
    ), mock.patch(
        "nanobot_quant.exec_params.load_exec_params",
        return_value={"execution_mode": "direct"},
    ), mock.patch(
        "nanobot_quant.execution_loop.enqueue_signal"
    ) as enq, mock.patch(
        "nanobot_quant.pipeline.run_from_signals", return_value=fake_results
    ) as rf:
        result = tools_execute.execute_signal(signal_json, live=True)

    assert result.get("queued") is not True
    assert rf.called
    enq.assert_not_called()
# ── get_execution_outcome ───────────────────────────────────────────────

def test_get_execution_outcome_pending_then_done():
    from nanobot_quant.tools import tools_execute

    # loop 未运行 → loop_not_running
    with mock.patch(
        "nanobot_quant.execution_loop.loop_status",
        return_value={"running": False, "stats": {}},
    ):
        assert tools_execute.get_execution_outcome("loop-1")["status"] == "loop_not_running"

    # running 但无结果 → pending
    with mock.patch(
        "nanobot_quant.execution_loop.loop_status",
        return_value={"running": True, "stats": {}},
    ), mock.patch(
        "nanobot_quant.execution_loop.get_outcome", return_value=None
    ):
        assert tools_execute.get_execution_outcome("loop-1")["status"] == "pending"

    # running 且有结果 → done
    with mock.patch(
        "nanobot_quant.execution_loop.loop_status",
        return_value={"running": True, "stats": {}},
    ), mock.patch(
        "nanobot_quant.execution_loop.get_outcome",
        return_value={"ticker": "SOL", "risk_passed": True},
    ):
        res = tools_execute.get_execution_outcome("loop-1")
        assert res["status"] == "done" and res["outcome"]["ticker"] == "SOL"


def test_strategy_queue_ready_before_initialize():
    """回归: P1 实测 'SignalExecutionStrategy' object has no attribute
    '_signal_queue' — ensure_loop 返回实例后立即入队，不能等 lumibot
    StrategyExecutor 回调 initialize() 才建队列。"""
    from nanobot_quant.execution_loop import SignalExecutionStrategy

    s = SignalExecutionStrategy()  # 队列天然就绪（__init__ 建立）
    order_id = s.enqueue_signal({"ticker": "SOL"}, {})
    assert order_id.startswith("loop-")
    assert s.stats()["queued"] == 1
    # 消费路径不受影响
    with mock.patch(
        "nanobot_quant.pipeline.run_from_signals", return_value=[{"ticker": "SOL"}]
    ):
        s._drain()
    assert s.stats()["processed"] == 1
    assert s.get_outcome(order_id) == {"ticker": "SOL"}
