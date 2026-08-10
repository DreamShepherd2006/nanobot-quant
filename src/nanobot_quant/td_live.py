"""TD 自主 live 循环管理器（P2 B3）。

在 quant agent 进程内驻留 StrategyExecutor 主循环 daemon 线程：

- ``TdSequentialStrategy``（参数来自 exec_params.json：td_symbol /
  td_sleeptime / quantity_mode / td_quantity / 风控参数）
- ``OnchainOSBroker`` + ``OnchainOSDataSource``（B1 已修 live 兼容；
  data_source 挂到 broker，Strategy 数据访问统一走 broker.data_source）
- WebUI 开关 ``td_enabled`` 启停（exec_params.json）

生命周期语义：
- ``sync_from_params()``：WebUI 保存 / execute_signal 调用时同步——
  td_enabled=True 且未运行 → 启动；False 且运行中 → 停止；
  运行中但参数变化 → 重启（stop 旧循环 → start 新参数）。
- ``status()``：当前循环状态（WebUI 展示）。

安全说明：
- StrategyExecutor 是 daemon Thread（lumibot 自带 stop_event），
  stop() 设置事件后主循环 ``while ... and self.should_continue`` 退出。
- 单例：同一时间只有一个 TD live 循环；旧线程未退出时 start() 拒绝
  重复启动（返回 running 状态），避免双循环重复下单。
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

_lock = threading.Lock()
_runner: "_TdLiveRunner | None" = None


class _TdLiveRunner:
    def __init__(self) -> None:
        self._executor: Any = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_error": None,
            "symbol": None,
            "sleeptime": None,
            "quantity_mode": None,
        }

    # ── 构造 ──────────────────────────────────────────────────────────
    def _build_executor(self, params: dict[str, Any]) -> Any:
        """构造 StrategyExecutor（lumibot 真包延迟导入，测试容器无 lumibot）。"""
        from lumibot.strategies.strategy_executor import StrategyExecutor

        from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker
        from nanobot_quant.data.onchainos_data_source import OnchainOSDataSource
        from nanobot_quant.strategies.td_sequential_strategy import (
            TdSequentialStrategy,
        )
        from nanobot_quant.tokens_store import load_tokens_json

        tokens = load_tokens_json() or []
        broker = OnchainOSBroker(
            tokens_json=tokens,
            slippage=str(params["slippage"]),
            sol_buffer_pct=float(params["sol_buffer_pct"]),
            data_source=OnchainOSDataSource(tokens_json=tokens),
        )
        # lumibot Strategy.__init__ 在 broker=None 时直接 raise
        # ("No broker is set")，必须构造时传入 broker + data_source。
        strategy = TdSequentialStrategy(
            broker=broker,
            data_source=broker.data_source,
        )
        strategy.parameters = dict(
            TdSequentialStrategy.parameters,
            **{
                "symbol": params["td_symbol"],
                "quantity": params["td_quantity"],
                "quantity_mode": params["quantity_mode"],
                "sleeptime": params["td_sleeptime"],
                "max_position_pct": params["max_position_pct"],
                "max_drawdown_pct": params["max_drawdown_pct"],
                "stop_loss_pct": params["stop_loss_pct"],
                # 子钱包分批（真分账 v1.1）：exit_order / take_profit_pct / td_start_slot
                "exit_order": params.get("exit_order", "fifo"),
                "take_profit_pct": float(params.get("take_profit_pct", 0.0) or 0.0),
                "td_start_slot": int(params.get("td_start_slot", 1) or 1),
                "tokens_json": tokens,
            },
        )
        # 批次（子钱包）台账：td_batches > 1 时注入 BatchManager，
        # 策略进入分批模式（BUY 占 slot / SELL 按 exit_order 平批 / 逐批止损止盈）。
        td_batches = int(params.get("td_batches", 1) or 1)
        if td_batches > 1:
            strategy.batch_manager = self._prepare_batches(
                td_batches, params["td_symbol"]
            )
        executor = StrategyExecutor(strategy)
        executor.daemon = True
        return executor

    def _prepare_batches(self, td_batches: int, symbol: str) -> Any:
        """加载/创建批次台账（子钱包映射）。

        batches.json 存在且 symbol 一致 → 复用（重启恢复）；否则从
        wallets.json 取前 N 个子钱包 account_id 新建。不足 N 时按实际
        数量建（下一 BUY 时无可用 slot 即跳过，日志告警）。
        """
        import sys

        from nanobot_quant.batches import BatchManager
        from nanobot_quant.tools.tools_wallet import wallet_accounts

        bm = BatchManager.load()
        if bm is not None and bm.symbol == symbol and bm.slots:
            print(
                f"[DIAG] td_live: batches restored ({symbol}, "
                f"{len(bm.slots)} slots)",
                file=sys.stderr, flush=True,
            )
            return bm
        try:
            acc = wallet_accounts()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[DIAG] td_live: wallet_accounts failed: {exc}",
                file=sys.stderr, flush=True,
            )
            return None
        if acc.get("status") != "ok":
            print(
                f"[DIAG] td_live: wallet_accounts error: "
                f"{acc.get('error')}",
                file=sys.stderr, flush=True,
            )
            return None
        ids = [
            a["account_id"]
            for a in acc.get("data", {}).get("accounts", [])[:td_batches]
        ]
        if not ids:
            print(
                "[DIAG] td_live: no sub-accounts in wallets.json",
                file=sys.stderr, flush=True,
            )
            return None
        if len(ids) < td_batches:
            print(
                f"[DIAG] td_live: only {len(ids)} sub-accounts, "
                f"requested {td_batches} — 请先在 WebUI 批次设置中创建",
                file=sys.stderr, flush=True,
            )
        bm = BatchManager(symbol=symbol, account_ids=ids)
        bm.save()
        print(
            f"[DIAG] td_live: batches created ({symbol}, {len(ids)} slots)",
            file=sys.stderr, flush=True,
        )
        return bm

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self, params: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            if self._thread is not None and self._thread.is_alive():
                # 已运行 → 返回当前状态（参数变更由 sync_from_params 先 stop）。
                # 若线程处于收尾中（stop 后未退出），等待其退出再启动。
                if not self._wait_thread_exit():
                    self._state["last_error"] = (
                        "旧 TD 循环线程未在超时内退出，拒绝启动新循环"
                        "（避免双循环重复下单）"
                    )
                    self._state["running"] = False
                    print(
                        "[DIAG] td_live: old thread did not exit within "
                        "timeout — refusing to start new loop",
                        file=sys.stderr, flush=True,
                    )
                    return self.status()
                # 等待成功后继续向下启动新循环
            try:
                executor = self._build_executor(params)
            except Exception as exc:  # pragma: no cover — lumibot 真包异常
                self._state["last_error"] = str(exc)
                self._state["running"] = False
                print(
                    f"[DIAG] td_live: build executor failed: {exc}",
                    file=sys.stderr, flush=True,
                )
                return self.status()
            self._executor = executor
            t = threading.Thread(target=self._run, daemon=True, name="td-live")
            self._thread = t
            t.start()
            self._state.update(
                running=True,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                last_error=None,
                symbol=params["td_symbol"],
                sleeptime=params["td_sleeptime"],
                quantity_mode=params["quantity_mode"],
            )
            print(
                f"[DIAG] td_live: StrategyExecutor started "
                f"({params['td_symbol']} @ {params['td_sleeptime']}, "
                f"mode={params['quantity_mode']})",
                file=sys.stderr, flush=True,
            )
            return self.status()

    def _run(self) -> None:
        try:
            # lumibot 运行时日志输出到 stdout 会污染 MCP stdio JSON-RPC 通道，
            # 全程重定向 stdout → stderr（P1 已验证方案）。
            _saved_stdout = sys.stdout
            sys.stdout = sys.stderr
            try:
                self._executor.run()  # Thread.run → StrategyExecutor 主循环
            finally:
                sys.stdout = _saved_stdout
        except Exception as exc:  # pragma: no cover — lumibot 真包异常
            self._state["last_error"] = str(exc)
            self._state["running"] = False
            print(
                f"[DIAG] td_live: executor stopped with error: {exc}",
                file=sys.stderr, flush=True,
            )

    def stop(self) -> dict[str, Any]:
        with _lock:
            if self._executor is not None:
                try:
                    self._executor.stop()  # stop_event.set()
                except Exception as exc:  # pragma: no cover
                    self._state["last_error"] = str(exc)
            self._state["running"] = False
            print(
                "[DIAG] td_live: StrategyExecutor stop requested",
                file=sys.stderr, flush=True,
            )
            return self.status()

    def _wait_thread_exit(self, timeout: float = 10.0) -> bool:
        """等待旧循环线程退出（stop 后 lumibot 收尾可能 >0.3s）。

        stop() 只设 stop_event，线程跑完 on_abrupt_closing / scheduler
        清理才真正退出；start() 的 is_alive 守卫若在收尾期间被调用会
        静默拒绝启动，导致参数变更后循环停在 running=False。
        轮询等待线程退出（默认 10s），超时返回 False（调用方 fail-closed，
        不启动新循环以避免双循环重复下单）。
        """
        t = self._thread
        if t is None or not t.is_alive():
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not t.is_alive():
                return True
            time.sleep(0.1)
        return not t.is_alive()

    def status(self) -> dict[str, Any]:
        alive = self._thread is not None and self._thread.is_alive()
        return dict(self._state, thread_alive=alive)

    # ── 参数同步 ──────────────────────────────────────────────────────
    def sync_from_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 exec_params 同步启停（WebUI 保存 / execute_signal 时调用）。

        td_enabled=True：未运行 → 启动；运行中且参数未变 → 保持；
        运行中但标的/周期/模式/风控变化 → 重启（stop → start）。
        td_enabled=False：运行中 → 停止。
        """
        if params.get("td_enabled"):
            running = (
                self._state.get("running")
                or (self._thread is not None and self._thread.is_alive())
            )
            if running:
                changed = any(
                    self._state.get(k) != params.get(pk)
                    for k, pk in (
                        ("symbol", "td_symbol"),
                        ("sleeptime", "td_sleeptime"),
                        ("quantity_mode", "quantity_mode"),
                    )
                )
                if not changed:
                    return self.status()
                # 参数变化 → 重启循环（先停旧的，避免双循环）；
                # 等旧线程真正退出再 start，避免 is_alive 守卫挡回。
                self.stop()
                if not self._wait_thread_exit():
                    self._state["last_error"] = (
                        "旧 TD 循环线程未在超时内退出，拒绝启动新循环"
                        "（避免双循环重复下单）"
                    )
                    self._state["running"] = False
                    print(
                        "[DIAG] td_live: old thread did not exit within "
                        "timeout — refusing to start new loop",
                        file=sys.stderr, flush=True,
                    )
                    return self.status()
            return self.start(params)
        if self._state.get("running") or (
            self._thread is not None and self._thread.is_alive()
        ):
            return self.stop()
        return self.status()


def get_runner() -> _TdLiveRunner:
    global _runner
    with _lock:
        if _runner is None:
            _runner = _TdLiveRunner()
        return _runner


def sync_from_params(params: dict[str, Any] | None = None) -> dict[str, Any]:
    """模块级入口：按 exec_params 同步 TD live 循环（幂等，可反复调用）。"""
    if params is None:
        from nanobot_quant.exec_params import load_exec_params

        params = load_exec_params()
    return get_runner().sync_from_params(params)


def status() -> dict[str, Any]:
    return get_runner().status()
