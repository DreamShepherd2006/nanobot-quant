"""Live execution loop — execution_mode="loop" (docs/quant-system.md §15.5.1).

双执行模式:
- direct (默认): execute_signal → pipeline.run_from_signals 同步直调（现状，零变化）
- loop (可选): 信号入队 → 立即返回 {queued, order_id} → 本模块惰性启动的
  daemon 线程按 loop_interval_seconds 周期消费队列，对每个信号调用与
  direct 完全相同的 run_from_signals(live=True) 路径。

因此风控门控（resolve_token 终门 / RiskEngine / exec_params）与 direct 完全一致，
行为等价，自研部分仅新增「队列 + 循环骨架」，不含任何交易逻辑。

实现说明（v2，去掉 lumibot StrategyExecutor）:
pipeline.run_from_signals(live=True) 完全自包含（内部自行构造 OnchainOSBroker
与 Lumibot Order，直调 _submit_order），不需要 lumibot 主循环。早期版本用
SignalExecutionStrategy(Strategy) + run_live 驱动，实测暴露两个问题：
1. StrategyExecutor 主循环需要 broker.data_source.get_datetime()，骨架 broker
   无真实 DataSource → _DummyDataSource 崩溃（on_bot_crash，策略死掉）；
2. lumibot 主循环日志持续写 stdout，污染 MCP JSON-RPC stdio 通道。
v2 改为纯 Python daemon 线程 + time.sleep 调度，零 lumibot 依赖，两个问题
一并消除。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any


class SignalExecutionStrategy:
    """队列驱动的实盘信号执行器（纯 Python，不依赖 lumibot）。

    - enqueue_signal(): 入队，立即返回 order_id（异步语义）
    - _drain(): 消费当前队列中的全部信号 → run_from_signals(live=True)
    - get_outcome()/stats(): 供外部查询执行结果
    """

    def __init__(self) -> None:
        self._signal_queue: queue.Queue[tuple[str, Any, dict]] = queue.Queue()
        self._outcomes: dict[str, dict] = {}
        self._stats = {"queued": 0, "processed": 0, "failed": 0}

    def enqueue_signal(self, signal: Any, kwargs: dict | None = None) -> str:
        """入队一个信号（或信号列表），立即返回 order_id（异步语义）。"""
        order_id = f"loop-{time.time_ns()}"
        self._signal_queue.put((order_id, signal, kwargs or {}))
        self._stats["queued"] += 1
        return order_id

    def get_outcome(self, order_id: str) -> dict | None:
        """按 order_id 查询执行结果；未完成/不存在返回 None。"""
        return self._outcomes.get(order_id)

    def stats(self) -> dict:
        return dict(self._stats)

    def _drain(self) -> None:
        """消费当前队列中的全部信号（daemon 线程每周期调用一次）。"""
        while True:
            try:
                order_id, signal, kwargs = self._signal_queue.get_nowait()
            except queue.Empty:
                return
            self._process(order_id, signal, kwargs)

    def _process(self, order_id: str, signal: Any, kwargs: dict) -> None:
        """单个信号的执行：复用 run_from_signals(live=True) 全链路。

        与 direct 模式共用同一执行路径（风控/代币门控/exec_params 行为完全一致）。
        run_from_signals 内部 lumibot 日志走 stdout → 重定向保护 MCP JSON-RPC。
        """
        from nanobot_quant.pipeline import run_from_signals

        # execute_signal 入队的是 signal_list（list）；direct 路径/测试也可能
        # 传单个 dict —— 归一化后透传，避免 [[list]] 嵌套导致
        # 'list' object has no attribute 'ticker'。
        signal_list = signal if isinstance(signal, list) else [signal]
        _saved_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            results = run_from_signals(signal_list, live=True, **kwargs)
            self._outcomes[order_id] = results[0] if results else {"error": "no result"}
            self._stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 — 单信号失败不得杀死循环
            print(
                f"[DIAG] execution_loop: order {order_id} failed: {exc}",
                file=sys.stderr, flush=True,
            )
            self._outcomes[order_id] = {"error": str(exc)}
            self._stats["failed"] += 1
        finally:
            sys.stdout = _saved_stdout


# ── 模块级单例：惰性启动 daemon 循环线程 ──────────────────────────────────

_loop_lock = threading.Lock()
_loop_strategy: SignalExecutionStrategy | None = None
_loop_thread: threading.Thread | None = None


def _current_interval() -> int:
    """读取当前循环周期（秒）。每次循环读取 → WebUI 修改即时生效。"""
    from .exec_params import load_exec_params

    return int(load_exec_params().get("loop_interval_seconds", 5))


def _worker(strategy: SignalExecutionStrategy) -> None:
    """daemon 循环：每 interval 秒醒来消费队列；周期每次循环读取。"""
    while True:
        time.sleep(_current_interval())
        strategy._drain()


def ensure_loop() -> SignalExecutionStrategy:
    """首次调用时启动常驻循环（daemon 线程），后续调用返回已有实例。

    惰性启动（文档 15.6 方案 A）：无信号时不占资源；循环随 agent 进程共存亡。
    """
    global _loop_strategy, _loop_thread
    with _loop_lock:
        if _loop_strategy is not None and _loop_thread is not None and _loop_thread.is_alive():
            return _loop_strategy
        strategy = SignalExecutionStrategy()
        _loop_strategy = strategy
        _loop_thread = threading.Thread(
            target=_worker,
            args=(strategy,),
            name="quant-execution-loop",
            daemon=True,
        )
        _loop_thread.start()
        print(
            f"[DIAG] execution_loop: daemon loop started ({_current_interval()}s iteration)",
            file=sys.stderr, flush=True,
        )
        return strategy


def enqueue_signal(signal: Any, kwargs: dict | None = None) -> str:
    """loop 模式入队入口（execute_signal 调用），立即返回 order_id。"""
    return ensure_loop().enqueue_signal(signal, kwargs)


def get_outcome(order_id: str) -> dict | None:
    if _loop_strategy is None:
        return None
    return _loop_strategy.get_outcome(order_id)


def loop_status() -> dict:
    """运行状态（供诊断/WebUI 展示）。"""
    if _loop_strategy is None:
        return {"running": False, "stats": {"queued": 0, "processed": 0, "failed": 0}}
    return {
        "running": bool(_loop_thread and _loop_thread.is_alive()),
        "outcomes": len(_loop_strategy._outcomes),
        "stats": _loop_strategy.stats(),
    }
