"""execute_signal: signal → pipeline (Risk → Portfolio → Order).

Takes a structured TickerSignal (from run_td_sequential or structurize_signal)
and runs it through the full execution pipeline.
"""

from __future__ import annotations

import json
import sys


def execute_signal(ticker_signal_json: str) -> dict:
    """Execute the trading pipeline on structured signal(s).

    Takes a JSON signal (from structurize_signal or run_td_sequential) and
    passes it through Risk → Position Sizing → Order generation.

    Accepts either a single signal object or a list of signals.

    Returns pipeline execution results with risk checks and suggested orders.
    """
    # ── Guard MCP stdio from library import-time logging ─────────
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from nanobot_quant.pipeline import run_from_signals
    finally:
        sys.stdout = _saved_stdout

    try:
        raw = json.loads(ticker_signal_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON input"}

    # Normalise to list of dicts
    signal_list: list[dict] = raw if isinstance(raw, list) else [raw]

    # Validate each dict has required fields
    for s in signal_list:
        if "ticker" not in s:
            return {"error": f"Missing 'ticker' in signal: {s}"}

    ticker_summary = [s.get("ticker", "?") for s in signal_list]
    print(
        f"[DIAG] execute_signal: running pipeline on {ticker_summary}",
        file=sys.stderr, flush=True,
    )

    try:
        results = run_from_signals(signal_list)
        return {"results": results, "count": len(results)}
    except Exception as exc:
        return {"error": f"Pipeline execution failed: {exc}"}
