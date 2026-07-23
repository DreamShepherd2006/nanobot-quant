"""Standalone backtest runner for TD Sequential strategy.

Usage::

    python -m nanobot_quant.backtest_runner AAPL 2024-07-01 2025-07-01
    python -m nanobot_quant.backtest_runner --batch AAPL,TSLA,NVDA 2024-01-01 2025-01-01

Results are printed to stdout and saved as JSON.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from lumibot.backtesting import YahooDataBacktesting
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

RESULTS_DIR = Path("/tmp/nanobot_quant_backtests")


def run(
    symbol: str,
    start: str,
    end: str,
    quantity: int = 10,
    max_position_pct: float = 0.20,
    max_drawdown_pct: float = 0.15,
) -> dict:
    """Run TD Sequential backtest and return metrics dict."""
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)

    print(f"\n{'='*60}")
    print(f"  {symbol} | {start} → {end}")
    print(f"{'='*60}")
    sys.stdout.flush()

    out = TdSequentialStrategy.run_backtest(
        YahooDataBacktesting, start_dt, end_dt,
        parameters={
            "symbol": symbol, "quantity": quantity,
            "max_position_pct": max_position_pct,
            "max_drawdown_pct": max_drawdown_pct,
        },
    )

    if isinstance(out, tuple):
        result, _strategy = out
    else:
        result = out

    if not isinstance(result, dict):
        return {"error": f"unexpected result type: {type(result)}"}

    md = result.get("max_drawdown", {})
    max_dd = float(md.get("drawdown", 0)) if isinstance(md, dict) else 0.0

    # ── Trade stats from CSV ──
    trade_stats = _extract_trade_stats(symbol)

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
        "total_trades": trade_stats.get("total_trades", 0),
        "win_count": trade_stats.get("win_count", 0),
        "loss_count": trade_stats.get("loss_count", 0),
        "win_rate_pct": trade_stats.get("win_rate_pct", 0.0),
        "total_pnl": round(trade_stats.get("total_pnl", 0.0), 2),
        "avg_win": round(trade_stats.get("avg_win", 0.0), 2),
        "avg_loss": round(trade_stats.get("avg_loss", 0.0), 2),
    }

    # ── Save ──
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{symbol}_{start}_{end}.json"
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    return metrics


def batch_run(
    symbols: list[str],
    start: str,
    end: str,
    quantity: int = 10,
    max_position_pct: float = 0.20,
    max_drawdown_pct: float = 0.15,
) -> list[dict]:
    """Run backtest for multiple symbols and return ranked results."""
    results: list[dict] = []
    for sym in symbols:
        try:
            m = run(sym, start, end, quantity, max_position_pct, max_drawdown_pct)
            results.append(m)
        except Exception as exc:
            results.append({"symbol": sym, "error": str(exc)})
    return results


def format_report(metrics: dict | list[dict]) -> str:
    """Return a formatted ASCII table string for one or more results."""
    if isinstance(metrics, dict):
        metrics = [metrics]

    lines = []
    header = (
        f"{'Symbol':<7} {'Return':>8} {'Sharpe':>7} {'MaxDD':>7} "
        f"{'Trades':>7} {'Win%':>7} {'AvgW':>8} {'AvgL':>8}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for m in metrics:
        if "error" in m:
            lines.append(f"{m['symbol']:<7} {'ERROR: ' + m['error'][:40]}")
            continue
        lines.append(
            f"{m['symbol']:<7} {m['total_return_pct']:>+7.1f}% "
            f"{m['sharpe_ratio']:>7.2f} {m['max_drawdown_pct']:>6.1f}% "
            f"{m.get('total_trades', 0):>7} {m.get('win_rate_pct', 0):>6.1f}% "
            f"{m.get('avg_win', 0):>7.2f} {m.get('avg_loss', 0):>7.2f}"
        )
    return "\n".join(lines)


# ── helpers ────────────────────────────────────────────────────────

def _extract_trade_stats(symbol: str) -> dict:
    """Parse the latest lumibot trades CSV to compute win rate, avg win/loss.

    Lumibot CSV format has two rows per trade: one ``new``, one ``fill``.
    We extract fill rows only, pair buy→sell by time order, and compute P&L.
    """
    logs_dir = Path("logs")
    if not logs_dir.exists():
        return {}

    csv_files = sorted(
        logs_dir.glob("TdSequentialStrategy_*_trades.csv"),
        key=os.path.getmtime, reverse=True,
    )
    if not csv_files:
        return {}

    try:
        with open(csv_files[0], newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fills = [r for r in reader if r.get("status") == "fill"]
    except Exception:
        return {}

    if not fills:
        return {}

    buys = [r for r in fills if r.get("side") == "buy"]
    sells = [r for r in fills if r.get("side") == "sell"]

    # Pair buy→sell in FIFO order (simple: our strategy holds one position)
    paired_pnls: list[float] = []
    for sell in sells:
        sqty = int(float(sell.get("filled_quantity", 0)))
        sprice = float(sell.get("price", 0))
        # match against oldest buys first
        while sqty > 0 and buys:
            buy = buys[0]
            bprice = float(buy.get("price", 0))
            bqty = int(float(buy.get("filled_quantity", 0)))
            matched_qty = min(sqty, bqty)
            pnl = (sprice - bprice) * matched_qty
            paired_pnls.append(pnl)
            sqty -= matched_qty
            remaining = bqty - matched_qty
            if remaining <= 0:
                buys.pop(0)
            else:
                buys[0]["filled_quantity"] = str(remaining)

    wins = [p for p in paired_pnls if p > 0]
    losses = [p for p in paired_pnls if p <= 0]
    total = len(paired_pnls)

    return {
        "total_trades": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / total * 100, 1) if total else 0.0,
        "total_pnl": round(sum(paired_pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


# ── CLI ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: python -m nanobot_quant.backtest_runner SYMBOL START END [QUANTITY]")
        print("       python -m nanobot_quant.backtest_runner --batch SYM1,SYM2,... START END")
        sys.exit(1)

    if sys.argv[1] == "--batch":
        symbols = [s.strip().upper() for s in sys.argv[2].split(",")]
        start, end = sys.argv[3], sys.argv[4]
        results = batch_run(symbols, start, end)
        print(f"\n{format_report(results)}")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        batch_path = RESULTS_DIR / "batch_{}_{}.json".format(
            start.replace("-", ""), end.replace("-", ""),
        )
        with open(batch_path, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nSaved: {batch_path}")
    else:
        symbol = sys.argv[1].upper()
        start, end = sys.argv[2], sys.argv[3]
        quantity = int(sys.argv[4]) if len(sys.argv) > 4 else 10
        result = run(symbol, start, end, quantity)
        if "error" not in result:
            print(f"\n{format_report(result)}")


if __name__ == "__main__":
    main()
