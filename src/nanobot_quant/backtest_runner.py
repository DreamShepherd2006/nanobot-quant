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

    out = TdSequentialStrategy.run_backtest(
        YahooDataBacktesting,
        start_dt,
        end_dt,
        parameters={"symbol": symbol, "quantity": quantity},
    )

    # ── Unpack: lumibot returns (metrics_dict, strategy_instance) ──
    if isinstance(out, tuple):
        result, _strategy = out
    else:
        result = out

    if not isinstance(result, dict):
        print(f"ERROR: unexpected result type: {type(result)}")
        print(f"Content: {result}")
        return {"error": f"unexpected result type: {type(result)}"}

    # ── Extract metrics from dict (lumibot v4.5.78 keys) ──
    md = result.get("max_drawdown", {})
    max_dd = float(md.get("drawdown", 0)) if isinstance(md, dict) else 0.0

    metrics = {
        "symbol": symbol,
        "start": start,
        "end": end,
        "total_return_pct": round(float(result.get("total_return", 0)) * 100, 2),
        "cagr_pct": round(float(result.get("cagr", 0)) * 100, 2),
        "sharpe_ratio": round(float(result.get("sharpe", 0)), 2),
        "volatility_pct": round(float(result.get("volatility", 0)) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "romad": round(float(result.get("romad", 0)), 2),
        "_raw_keys": sorted(result.keys()),
    }

    # ── Print ──
    print(f"\n{'='*60}")
    print(f"  RESULTS: {symbol}")
    print(f"{'='*60}")
    print(f"  Total Return      : {metrics['total_return_pct']:+.2f}%")
    print(f"  CAGR              : {metrics['cagr_pct']:+.2f}%")
    print(f"  Sharpe            : {metrics['sharpe_ratio']:.2f}")
    print(f"  Volatility        : {metrics['volatility_pct']:.2f}%")
    print(f"  Max Drawdown      : {metrics['max_drawdown_pct']:.2f}%")
    print(f"  RoMaD             : {metrics['romad']:.2f}")
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
