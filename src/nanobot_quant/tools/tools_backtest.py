"""run_backtest: async backtest as an MCP tool (run_id + poll pattern).

A real backtest (CLI kline fetch + Lumibot startup + strategy loop) takes
longer than the 30s MCP tool hard timeout for any non-trivial range
(measured: 5d ≈ 9s, 180d > 30s).  So the tool returns immediately with a
``run_id`` and runs the backtest in a background daemon thread; the result
is persisted to ``{data_root}/legion/backtests/<run_id>.json`` and fetched
with ``get_backtest_result``.  Same contract as ``run_research_chain`` /
``get_chain_result``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
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


def _auto_backtest(
    run_id: str,
    symbol: str,
    start: str,
    end: str,
    quantity: int,
    source: str,
) -> None:
    """Background thread: run the full backtest and persist the outcome.

    Guards the MCP stdio channel exactly like the sync path used to:
    1) env toggles BEFORE any lumibot import (constants read at import
       time) — kill the \\r progress bar and silence INFO logs;
    2) clear stdout-bound handlers on the lumibot logger tree (sub-loggers
       register their own StreamHandler at import time);
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

        from nanobot_quant.backtest_runner import run as _backtest_run

        _silence_lumibot_loggers()

        sys.stdout = sys.stderr
        try:
            result = _backtest_run(
                symbol=symbol,
                start=start,
                end=end,
                quantity=quantity,
                source=source,
            )
            _backtest_log(run_id, {"status": "done", "run_id": run_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            _backtest_log(run_id, {"status": "error", "run_id": run_id, "error": str(exc)})
        finally:
            sys.stdout = _saved_stdout
    finally:
        sys.stdout = _saved_stdout


def run_backtest(
    symbol: str,
    start: str,
    end: str,
    quantity: int = 10,
    source: str = "onchainos",
) -> dict:
    """Start a full backtest on a token symbol in the background.

    Args:
        symbol: Token symbol, e.g. "SOL/USDC" or "CRCLX/USDC"
        start: Start date, e.g. "2026-01-01"
        end: End date, e.g. "2026-07-05"
        quantity: Trade quantity (default 10)
        source: Data source registry name: "onchainos", "okx_cex", "yfinance"
                (alias "yahoo"), or "gate_cex" (not implemented for
                backtest — returns a clear error, fail-closed).

    Returns:
        dict with status=started and run_id. The backtest runs in a
        background thread (a real run exceeds the 30s MCP tool timeout);
        poll ``get_backtest_result(run_id)`` for the metrics.
    """
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    threading.Thread(
        target=_auto_backtest,
        args=(run_id, symbol, start, end, quantity, source),
        daemon=True,
    ).start()
    return {
        "status": "started",
        "run_id": run_id,
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
