"""Export TD Setup sequence for manual comparison against 同花顺「九转序列」.

同花顺 (THS) displays only the Setup 1-9 count on the daily chart, so this
tool exports exactly that: per-bar ``setup_buy`` / ``setup_sell`` counts
(1-9, DeMark cycle) plus a marker on bars where the count reaches 9.

Usage:
    # yfinance data (US equities — THS 美股/国际版 can show the same chart)
    python -m nanobot_quant.calibration.export_sequence \\
        --ticker AAPL --days 250 --out /tmp/aapl_td

    # or from an existing CSV (columns: Date,Open,High,Low,Close,Volume)
    python -m nanobot_quant.calibration.export_sequence \\
        --csv /path/bars.csv --out /tmp/aapl_td

Outputs ``<out>.csv`` and ``<out>.html``.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from nanobot_quant.strategies.td_sequential import _DeMarkEngine
from nanobot_quant.td_params import DEFAULT_TD_PARAMS

_COL_MAP = {"date": "Date", "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"}


def load_bars_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={k: v for k, v in _COL_MAP.items() if k in df.columns})
    for col in ("Open", "High", "Low", "Close"):
        if col not in df.columns:
            raise SystemExit(f"CSV missing required column: {col} (have {list(df.columns)})")
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
    df["Volume"] = df.get("Volume", pd.Series([1_000_000] * len(df), index=df.index))
    return df


def fetch_yfinance(ticker: str, days: int) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("yfinance not installed — pip install yfinance, or use --csv") from exc
    df = yf.download(ticker, period=f"{days}d", interval="1d", auto_adjust=False,
                     progress=False)
    if df.empty:
        raise SystemExit(f"yfinance returned no data for {ticker}")
    df = df.droplevel(level="Ticker", axis=1) if hasattr(df.columns, "levels") and "Ticker" in df.columns.get_level_values(0) else df
    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def build_sequence(df: pd.DataFrame) -> pd.DataFrame:
    engine = _DeMarkEngine(df, dict(DEFAULT_TD_PARAMS))
    out = engine.run_all()
    seq = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in out.index],
        "close": out["Close"].round(4),
        "setup_buy": out["buy_setup_count"].astype(int),
        "setup_sell": out["sell_setup_count"].astype(int),
    })
    note = []
    for _, row in seq.iterrows():
        if row["setup_buy"] == 9:
            note.append("⭕ 买9")
        elif row["setup_sell"] == 9:
            note.append("⭕ 卖9")
        else:
            note.append("")
    seq["note"] = note
    return seq


def to_csv(seq: pd.DataFrame, out: str) -> None:
    seq.to_csv(f"{out}.csv", index=False, encoding="utf-8-sig")
    print(f"CSV  → {out}.csv ({len(seq)} rows)")


def to_html(seq: pd.DataFrame, out: str, ticker: str) -> None:
    rows = []
    for _, r in seq.iterrows():
        buy = f'<span class="num buy">{r["setup_buy"]}</span>' if r["setup_buy"] else ""
        sell = f'<span class="num sell">{r["setup_sell"]}</span>' if r["setup_sell"] else ""
        cls = ' class="nine"' if r["note"] else ""
        rows.append(
            f'<tr{cls}><td>{r["date"]}</td><td>{r["close"]}</td>'
            f"<td>{buy}</td><td>{sell}</td><td>{r['note']}</td></tr>"
        )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>TD Setup 序列 — {ticker}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 24px; }}
  h1 {{ font-size: 18px; }} p.hint {{ color: #666; font-size: 13px; }}
  table {{ border-collapse: collapse; font-size: 13px; margin-top: 12px; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: center; }}
  th {{ background: #f5f5f5; }}
  tr.nine {{ background: #fff7e6; }}
  .num {{ display: inline-block; min-width: 22px; padding: 1px 4px; border-radius: 4px; }}
  .buy {{ background: #fdecea; color: #c0392b; }}
  .sell {{ background: #e8f5e9; color: #2e7d32; }}
</style></head><body>
<h1>TD Setup 序列 — {ticker}（日线，同花顺「九转序列」对照口径）</h1>
<p class="hint">setup 数字 1-9（DeMark 循环）；标 ⭕ 行为计数到达 9（买9/卖9）。对照同花顺同标的日线叠加「九转序列」指标。</p>
<table><thead><tr><th>日期</th><th>收盘</th><th>买 Setup</th><th>卖 Setup</th><th>标注</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    with open(f"{out}.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML → {out}.html")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="导出 TD Setup 序列（同花顺九转对照）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ticker", help="美股代码（yfinance），如 AAPL / TSLA / SPY")
    src.add_argument("--csv", help="本地 K 线 CSV（Date,Open,High,Low,Close[,Volume]）")
    ap.add_argument("--days", type=int, default=250, help="yfinance 拉取天数（默认 250）")
    ap.add_argument("--out", required=True, help="输出前缀（生成 <out>.csv 与 <out>.html）")
    args = ap.parse_args(argv)

    df = fetch_yfinance(args.ticker, args.days) if args.ticker else load_bars_csv(args.csv)
    seq = build_sequence(df)
    to_csv(seq, args.out)
    to_html(seq, args.out, args.ticker or args.csv)


if __name__ == "__main__":
    main(sys.argv[1:])
