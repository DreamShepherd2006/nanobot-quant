"""run_backtest: async backtest as an MCP tool (run_id + poll pattern).

A real backtest (CLI kline fetch + Lumibot startup + strategy loop) takes
longer than the 30s MCP tool hard timeout for any non-trivial range
(measured: 5d ≈ 9s, 180d > 30s).  So the tool returns immediately with a
``run_id`` and runs the backtest in a background daemon thread; the result
is persisted to ``{data_root}/legion/backtests/<run_id>.json`` and fetched
with ``get_backtest_result``.  Same contract as ``run_research_chain`` /
``get_chain_result``.

Two engines:
- ``backtest_runner`` (default, zero behaviour change): legacy lumibot
  StrategyExecutor backtest on a single symbol/range.
- ``driver`` (Step 3 replay driver): scene-based replay on Gate CEX
  history, reusing the live strategy decision code (BacktestBroker for
  simulated fills).  Result carries scene/symbols/fills_detail/net_values/
  ROI — the WebUI /config/backtest page consumes this engine.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from uuid import uuid4


def _backtest_log(run_id: str, payload: dict) -> None:
    """Persist backtest outcome to ``{data_root}/legion/backtests``.

    Written to the persistent audit directory (survives Factory Rebuild)
    and mirrored to the MCP server stderr.
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        out_dir = backtests_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — logging must never break the chain
        print(f"[DIAG] _backtest_log failed: {exc}", file=sys.stderr, flush=True)
    print(
        f"[DIAG] run_backtest {run_id}: {json.dumps(payload, ensure_ascii=False)[:800]}",
        file=sys.stderr,
        flush=True,
    )


def _run_guarded(run_id: str, prep, run) -> None:
    """Background thread: guard MCP stdio, run the backtest, persist outcome.

    Guards the MCP stdio channel exactly like the sync path used to:
    1) env toggles BEFORE any lumibot import (constants read at import
       time) — kill the \\r progress bar and silence INFO logs;
    2) clear stdout-bound handlers on the lumibot logger tree (sub-loggers
       register their own StreamHandler at import time) — ``prep`` imports
       the lumibot-dependent modules first, then silence runs;
    3) redirect the runner's own print() progress lines (CLI-facing) to
       stderr; the result is persisted as a value, never via stdout.
    """
    _saved_stdout = sys.stdout
    try:
        os.environ.setdefault("LUMIBOT_TELEMETRY", "0")
        os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "0")
        os.environ.setdefault("BACKTESTING_QUIET_LOGS", "true")

        from nanobot_quant.tools.tools_execute import (
            _redirect_lumibot_console_to_stderr,
            _silence_lumibot_loggers,
        )

        # "LumiBot vX starting" is logged AT import time via a stdout-bound
        # StreamHandler — pre-register a stderr console handler so the banner
        # reuses it and never touches stdout. Then silence the tree.
        _redirect_lumibot_console_to_stderr()
        prep()
        _silence_lumibot_loggers()

        sys.stdout = sys.stderr
        try:
            result = run()
            _backtest_log(run_id, {"status": "done", "run_id": run_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            _backtest_log(run_id, {"status": "error", "run_id": run_id, "error": str(exc)})
        finally:
            sys.stdout = _saved_stdout
    finally:
        sys.stdout = _saved_stdout


def _auto_backtest(
    run_id: str,
    symbol: str,
    start: str,
    end: str,
    quantity: int,
    source: str,
) -> None:
    """Legacy engine (backtest_runner): single-symbol range backtest."""

    def _prep() -> None:
        # Import the lumibot-dependent module inside the guard so its
        # import-time stdout handlers are silenced afterwards.
        from nanobot_quant.backtest_runner import run as _backtest_run  # noqa: F401

    def _run():
        from nanobot_quant.backtest_runner import run as _backtest_run

        return _backtest_run(
            symbol=symbol,
            start=start,
            end=end,
            quantity=quantity,
            source=source,
        )

    _run_guarded(run_id, prep=_prep, run=_run)


def _parse_ts(value: str | None):
    """start/end → datetime。无时区输入明确按 UTC
    （页面提交已由前端把本地时间转成 UTC ISO）。"""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _auto_backtest_driver(
    run_id: str,
    scene: str,
    symbols: list[str] | None,
    start: str | None,
    end: str | None,
    initial_quote: float,
    batches: int | None,
    slippage: float | None,
    fixed_amount: float | None,
    overrides: dict | None = None,
) -> None:
    """New engine (backtest.driver): scene-based replay on Gate CEX history.

    Reuses the live strategy decision code (same StrategyExecutor scene
    construction, BacktestBroker for simulated fills).  Same run_id +
    poll contract as the legacy engine.
    """
    from datetime import datetime

    def _prep() -> None:
        from nanobot_quant.backtest.driver import BacktestDriver  # noqa: F401

    def _run():
        from nanobot_quant.backtest.driver import BacktestDriver
        from nanobot_quant.onchainos_cli import backtests_dir

        # 进度文件 = 结果文件（<run_id>.json）：运行期间 driver 写
        # {status: running, progress}，返回后 _backtest_log 覆写 done/error
        d = BacktestDriver(
            scene=scene,
            symbols=symbols,
            start_ts=_parse_ts(start),
            end_ts=_parse_ts(end),
            initial_quote=initial_quote,
            batches=batches,
            slippage=slippage,
            fixed_amount=fixed_amount,
            overrides=overrides,
            progress_path=backtests_dir() / f"{run_id}.json",
        )
        return d.run()

    _run_guarded(run_id, prep=_prep, run=_run)


def run_backtest(
    symbol: str | None = None,
    start: str | None = None,
    end: str | None = None,
    quantity: int = 10,
    source: str = "onchainos",
    engine: str = "backtest_runner",
    scene: str = "mid",
    symbols: list[str] | None = None,
    initial_quote: float = 100.0,
    batches: int | None = None,
    slippage: float | None = None,
    fixed_amount: float | None = None,
    overrides: dict | None = None,
) -> dict:
    """Start a full backtest in the background (run_id + poll contract).

    Args:
        symbol: Token symbol, e.g. "SOL/USDC" or "CRCLX/USDC" (legacy engine)
        start: Start date, e.g. "2026-01-01"
        end: End date, e.g. "2026-07-05"
        quantity: Trade quantity (legacy engine, default 10)
        source: Data source registry name for the legacy engine:
                "onchainos", "okx_cex", "yfinance" (alias "yahoo"), or
                "gate_cex" (not implemented — returns a clear error).
        engine: "backtest_runner" (legacy lumibot engine, default — zero
                behaviour change) or "driver" (Step 3 replay driver:
                scene-based, Gate CEX history, same decision code as live).
        scene: Scene name (high/mid/low) — engine="driver" only.
        symbols: Override the scene symbol pool — engine="driver" only.
        initial_quote: Per-slot simulated starting USDT — engine="driver".
        batches: Override scene batch count — engine="driver" only.
        slippage: Override global slippage — engine="driver" only.
        fixed_amount: Override per-trade fixed USDT amount (quantity_mode=
                "fixed_amount") — engine="driver" only; None = scene config.
        overrides: Extra strategy-parameter overrides for this backtest only
                (e.g. {"sell_only_profit_high": 0.005, "exit_setup": 10}).
                Keys absent from the dict fall back to scene config, then
                global exec_params, then class defaults. Never written back
                to exec_params.json — 2026-08-30 拍板.

    Returns:
        dict with status=started and run_id. The backtest runs in a
        background thread (a real run exceeds the 30s MCP tool timeout);
        poll ``get_backtest_result(run_id)`` for the outcome.
    """
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    if engine == "driver":
        syms = list(symbols) if symbols else ([symbol] if symbol else None)
        threading.Thread(
            target=_auto_backtest_driver,
            args=(run_id, scene, syms, start, end, initial_quote, batches, slippage, fixed_amount, overrides),
            daemon=True,
        ).start()
    else:
        if not symbol:
            return {
                "error": "engine='backtest_runner' requires symbol",
                "hint": "Pass symbol (e.g. 'SOL/USDC'), or use engine='driver' with scene.",
            }
        threading.Thread(
            target=_auto_backtest,
            args=(run_id, symbol, start, end, quantity, source),
            daemon=True,
        ).start()
    return {
        "status": "started",
        "run_id": run_id,
        "engine": engine,
        "message": (
            "Backtest started in background. Poll get_backtest_result("
            f"run_id=\"{run_id}\") for the outcome — a non-trivial range "
            "exceeds the 30s MCP tool timeout, so results are written to "
            "{data_root}/legion/backtests/<run_id>.json."
        ),
    }


def get_backtest_result(run_id: str) -> dict:
    """Return the persisted outcome of a background backtest ``run_id``.

    Reads ``{data_root}/legion/backtests/<run_id>.json``.  Returns a hint
    when the file is not there yet (still running / never started).
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        p = backtests_dir() / f"{run_id}.json"
        if not p.is_file():
            return {
                "error": f"no backtest result for run_id={run_id}",
                "hint": "The backtest may still be running, or the run_id is wrong.",
            }
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read backtest result for {run_id}: {exc}"}
