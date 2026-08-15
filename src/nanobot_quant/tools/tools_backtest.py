"""run_backtest: wrap backtest_runner.run() as an MCP tool.

One-shot full-pipeline backtest: resolve → data → TD → strategy → Lumibot → results.
"""

from __future__ import annotations

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
    # ── Guard MCP stdio from library import-time logging AND the
    #    backtest runner's own print() progress lines (CLI-facing).
    #    Everything must go to stderr while inside an MCP tool call;
    #    the result is returned as a value, never via stdout.
    _saved_stdout = sys.stdout
    try:
        from nanobot_quant.backtest_runner import run as _backtest_run

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
