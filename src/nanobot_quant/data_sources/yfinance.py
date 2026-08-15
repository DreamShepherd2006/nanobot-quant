"""yfinance stock data source — fallback feed + fundamentals.

Yahoo rate-limits datacenter IPs (429), so EastMoney is the primary stock
source; this source is the fallback and the fundamentals provider.

kind=research：股票源只服务分析页展示与回测，不参与执行。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

# yfinance interval map — no 4h in either source.
_YF_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "60m", "1D": "1d", "1W": "1wk"}
_SPAN = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "1D": 86400, "1W": 604800}


def yf_symbol(ticker: str) -> str:
    """Map a symbol to yfinance format: 6-digit codes get .SS/.SZ suffix."""
    if ticker.isdigit() and len(ticker) == 6:
        return f"{ticker}.SS" if ticker.startswith(("6", "9", "5")) else f"{ticker}.SZ"
    return ticker


def fetch_kline(ticker: str, bar: str = "1D", limit: int = 60,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """yfinance kline → OnchainOS-shaped DataFrame.

    ``end`` is exclusive for yfinance; +1 day covers the requested end.
    Minute bars (1m/5m/15m/1H) with start/end on the same day would return
    an empty range — the +1 day extension fixes that, extra rows are
    trimmed by ``tail(limit)``.
    """
    interval = _YF_INTERVALS.get(bar)
    if interval is None:
        raise ValueError(f"股票数据源暂不支持 {bar} 周期（支持 1m/5m/15m/1H/1D/1W）")
    if start is None:
        now = end or datetime.now()
        span = _SPAN.get(bar, 86400) * max(limit, 10) * 2
        start = now - timedelta(seconds=span)
        end = now
    end = end + timedelta(days=1)
    df = yf.download(
        yf_symbol(ticker),
        start=start.strftime("%Y-%m-%d") if hasattr(start, "strftime") else start,
        end=end.strftime("%Y-%m-%d") if hasattr(end, "strftime") else end,
        interval=interval,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"yfinance 无数据: {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance ≥1.x wraps columns as (Close, NVDA), (High, NVDA) …
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    df = df[cols].dropna(subset=["close"])
    # Keep the exchange tz (e.g. America/New_York, Asia/Shanghai for
    # .SS/.SZ) — the display layer tz_convert()s to local/UTC.
    df.index.name = "time"
    if limit and len(df) > limit:
        df = df.tail(limit)
    return df
