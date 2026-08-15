"""run_backtest: wrap backtest_runner.run() as an MCP tool.

One-shot full-pipeline backtest: resolve → data → TD → strategy → Lumibot → results.
"""

from __future__ import annotations

import os
import sys


def run_backtest(
    symbol: str,
    start: str,
    end: str,
    quantity: int = 10,
    source: str = "onchainos",
) -> dict:
    """Run a full backtest on a token symbol.

    Args:
        symbol: Token symbol, e.g. "SOL" or "CRCLx"
        start: Start date, e.g. "2026-04-01"
        end: End date, e.g. "2026-07-29"
        quantity: Trade quantity (default 10)
        source: Data source: "onchainos" or "yfinance"

    Returns backtest metrics: total_return_pct, cagr_pct, sharpe_ratio,
    total_trades, win_count, loss_count, etc.
    """
    # ── Guard MCP stdio from library output ──────────────────────────
    # 1) env toggles must be set BEFORE any lumibot import (constants are
    #    read at import time): kill the \r progress bar (it merges with the
    #    JSON-RPC response line and loses it) and silence INFO logs.
    # 2) clear stdout-bound handlers on the whole lumibot logger tree
    #    (sub-loggers register their own StreamHandler at import time).
    # 3) redirect the runner's own print() progress lines (CLI-facing) to
    #    stderr while executing; the result is returned as a value.
    _saved_stdout = sys.stdout
    try:
        os.environ.setdefault("LUMIBOT_TELEMETRY", "0")
        os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "0")
        os.environ.setdefault("BACKTESTING_QUIET_LOGS", "true")

        from nanobot_quant.backtest_runner import run as _backtest_run
        from nanobot_quant.tools.tools_execute import _silence_lumibot_loggers

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
            return result
        except Exception as exc:
            return {"error": f"Backtest failed: {exc}"}
        finally:
            sys.stdout = _saved_stdout
    finally:
        sys.stdout = _saved_stdout
