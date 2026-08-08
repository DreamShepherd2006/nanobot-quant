"""Live execution loop — execution_mode="loop" (docs/quant-system.md §15.5.1).

双执行模式:
- direct (默认): execute_signal → pipeline.run_from_signals 同步直调（现状，零变化）
- loop (可选): 信号入队 → 立即返回 {queued, order_id} → 本模块惰性启动的
  SignalExecutionStrategy 在 lumibot StrategyExecutor 主循环内异步消费队列，
  对每个信号调用与 direct 完全相同的 run_from_signals(live=True) 路径。

因此风控门控（resolve_token 终门 / RiskEngine / exec_params）与 direct 完全一致，
行为等价，自研部分仅新增「队列 + 循环骨架」，不含任何交易逻辑。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any

from lumibot.strategies.strategy import Strategy


class SignalExecutionStrategy(Strategy):
    """Queue-driven lumibot Strategy for live execution.

    生命周期（由 StrategyExecutor 主循环驱动）:
    - initialize(): 建立线程安全信号队列, 每 5 秒迭代一次
    - on_trading_iteration(): 消费队列中的全部信号 → run_from_signals(live=True)
    - get_outcome()/stats(): 供外部查询执行结果（异步语义）
    """

    def initialize(self) -> None:
        from .exec_params import load_exec_params

        interval = int(load_exec_params().get("loop_interval_seconds", 5))
        self.sleeptime = f"{interval}s"
        self._signal_queue: queue.Queue[tuple[str, Any, dict]] = queue.Queue()
        self._outcomes: dict[str, dict] = {}
        self._stats = {"queued": 0, "processed": 0, "failed": 0}

    # ── 外部注入接口（MCP execute_signal loop 分支调用） ────────────────

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

    # ── StrategyExecutor 主循环回调 ──────────────────────────────────────

    def on_trading_iteration(self) -> None:
        """按 loop_interval_seconds 周期消费队列（24/7 连续市场下持续运行）。

        每次迭代刷新 self.sleeptime：lumibot StrategyExecutor 在每轮迭代
        开始时读取 strategy.sleeptime 决定下一次调度间隔，因此 WebUI 修改
        循环周期后下一轮迭代即生效（无需重启循环）。
        """
        from .exec_params import load_exec_params

        interval = int(load_exec_params().get("loop_interval_seconds", 5))
        self.sleeptime = f"{interval}s"
        while True:
            try:
                order_id, signal, kwargs = self._signal_queue.get_nowait()
            except queue.Empty:
                return
            self._process(order_id, signal, kwargs)

    def _process(self, order_id: str, signal: Any, kwargs: dict) -> None:
        """单个信号的执行：复用 run_from_signals(live=True) 全链路。

        与 direct 模式共用同一执行路径（风控/代币门控/exec_params 行为完全一致）。
        """
        from nanobot_quant.pipeline import run_from_signals

        try:
            results = run_from_signals([signal], live=True, **kwargs)
            self._outcomes[order_id] = results[0] if results else {"error": "no result"}
            self._stats["processed"] += 1
        except Exception as exc:  # noqa: BLE001 — 单信号失败不得杀死循环
            print(
                f"[DIAG] execution_loop: order {order_id} failed: {exc}",
                file=sys.stderr, flush=True,
            )
            self._outcomes[order_id] = {"error": str(exc)}
            self._stats["failed"] += 1


# ── 模块级单例：惰性启动 StrategyExecutor 循环（daemon 线程） ────────────

_loop_lock = threading.Lock()
_loop_strategy: SignalExecutionStrategy | None = None
_loop_thread: threading.Thread | None = None


def ensure_loop() -> SignalExecutionStrategy:
    """首次调用时启动常驻循环（daemon 线程），后续调用返回已有实例。

    惰性启动（文档 15.6 方案 A）：无信号时不占资源；循环随 agent 进程共存亡。
    """
    global _loop_strategy, _loop_thread
    with _loop_lock:
        if _loop_strategy is not None and _loop_thread is not None and _loop_thread.is_alive():
            return _loop_strategy
        from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker
        from nanobot_quant.exec_params import load_exec_params

        exec_params = load_exec_params()
        interval = int(exec_params.get("loop_interval_seconds", 5))
        # 循环骨架 broker：仅用于满足 StrategyExecutor 运行（market=24/7 连续市场）；
        # 实际下单仍由 run_from_signals 内部构造的同参 broker 完成（行为与 direct 一致）。
        broker = OnchainOSBroker(
            tokens_json=[],
            slippage=str(exec_params.get("slippage", "0.01")),
            sol_buffer_pct=float(exec_params.get("sol_buffer_pct", 0.05)),
        )
        strategy = SignalExecutionStrategy(broker=broker, name="quant-signal-execution")
        _loop_strategy = strategy
        _loop_thread = threading.Thread(
            target=strategy.run_live,
            name="quant-execution-loop",
            daemon=True,
        )
        _loop_thread.start()
        print(
            f"[DIAG] execution_loop: StrategyExecutor loop started (daemon, {interval}s iteration)",
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
