"""Standalone backtest runner for TD Sequential strategy.

Usage::

    python -m nanobot_quant.backtest_runner AAPL 2024-07-01 2025-07-01

Results are printed to stdout and saved as JSON to the workspace.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from lumibot.backtesting import YahooDataBacktesting
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

# Default output directory for backtest results
RESULTS_DIR = Path("/tmp/nanobot_quant_backtests")


def run(symbol: str, start: str, end: str, quantity: int = 10) -> dict:
    """Run TD Sequential backtest and return metrics dict."""
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    print(f"\n{'='*60}")
    print(f"  TD Sequential Backtest")
    print(f"  Symbol: {symbol}  |  Period: {start} → {end}")
    print(f"{'='*60}")
    print("  Running... (this may take 30–60s for 1 year of daily data)")
    sys.stdout.flush()

    result = TdSequentialStrategy.run_backtest(
        YahooDataBacktesting,
        start_dt,
        end_dt,
        parameters={"symbol": symbol, "quantity": quantity},
    )

    # ── Extract metrics ──
    total_return = float(result.total_return)
    cagr = float(result.cagr)
    try:
        sharpe = float(result.sharpe_ratio)
    except (AttributeError, TypeError):
        sharpe = 0.0
    try:
        max_dd_pct = float(result.max_drawdown.percentage)
    except (AttributeError, TypeError):
        max_dd_pct = 0.0
    try:
        win_rate = float(result.win_rate)
    except (AttributeError, TypeError):
        win_rate = 0.0

    metrics = {
        "symbol": symbol,
        "start": start,
        "end": end,
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "win_rate_pct": round(win_rate, 2),
        "total_trades": int(getattr(result, "total_trades", 0)),
    }

    # ── Print ──
    print(f"\n{'='*60}")
    print(f"  RESULTS: {symbol}")
    print(f"{'='*60}")
    print(f"  Total Return      : {metrics['total_return_pct']:+.2f}%")
    print(f"  CAGR              : {metrics['cagr_pct']:+.2f}%")
    print(f"  Sharpe Ratio      : {metrics['sharpe_ratio']:.2f}")
    print(f"  Max Drawdown      : {metrics['max_drawdown_pct']:.2f}%")
    print(f"  Win Rate          : {metrics['win_rate_pct']:.1f}%")
    print(f"  Total Trades      : {metrics['total_trades']}")
    print(f"{'='*60}\n")

    # ── Save JSON ──
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{symbol}_{start}_{end}.json"
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {out_path}")

    return metrics


# ── CLI ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: python -m nanobot_quant.backtest_runner SYMBOL START END [QUANTITY]")
        print("Example: python -m nanobot_quant.backtest_runner AAPL 2024-07-01 2025-07-01 10")
        sys.exit(1)

    symbol = sys.argv[1].upper()
    start = sys.argv[2]
    end = sys.argv[3]
    quantity = int(sys.argv[4]) if len(sys.argv) > 4 else 10

    run(symbol, start, end, quantity)


if __name__ == "__main__":
    main()
