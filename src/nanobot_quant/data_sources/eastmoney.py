"""EastMoney (东财) stock data source — primary real-stock feed.

push2his.eastmoney.com 免 key、数据中心 IP 稳定。klt 周期码与时间戳
语义（A股 Asia/Shanghai、美股日线 America/New_York、美股分钟线
Asia/Shanghai）见 _fetch_kline 内注释（2026-08-05 实测确认）。

kind=research：股票源只服务分析页展示与回测，不参与执行。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# EastMoney klt codes (no 4H — 东财无 240m).
_EM_KLTS = {"1m": "1", "5m": "5", "15m": "15", "1H": "60", "1D": "101", "1W": "102"}
_SPAN = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "1D": 86400, "1W": 604800}


def stock_secid(ticker: str) -> str:
    """Map a symbol to an EastMoney secid.

    6-digit numeric codes are treated as A-shares (SSE ``1.`` / SZSE
    ``0.``); anything else is treated as a US symbol (``105.`` NYSE).
    5-prefix codes are SSE ETF (510/511/560/561/588 etc.); SZSE ETF use
    1/2/3-prefix (e.g. 159xxx) so they keep the ``0.`` branch.
    """
    if ticker.isdigit() and len(ticker) == 6:
        return f"1.{ticker}" if ticker.startswith(("6", "9", "5")) else f"0.{ticker}"
    return f"105.{ticker}"


def fetch_kline(ticker: str, bar: str = "1D", limit: int = 60,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """EastMoney kline API → normalised DataFrame.

    Response klines: "date,open,close,high,low,volume" (fields2
    f51..f56). US symbols use secid=105.<SYMBOL>; 6-digit codes are
    A-shares (secid 1./0.). 4H has no klt code.
    """
    klt = _EM_KLTS.get(bar)
    if klt is None:
        raise ValueError(f"股票数据源暂不支持 {bar} 周期（支持 1m/5m/15m/1H/1D/1W）")
    if start is None:
        now = end or datetime.now()
        span = _SPAN.get(bar, 86400) * max(limit, 10) * 2
        start = now - timedelta(seconds=span)
        end = now
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={stock_secid(ticker)}&fields1=f1,f2,f3,f4,f5&"
        "fields2=f51,f52,f53,f54,f55,f56&"
        f"klt={klt}&fqt=1&beg={start.strftime('%Y%m%d')}&end={end.strftime('%Y%m%d')}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError(f"东财无数据: {ticker}")
    rows = []
    for line in klines:
        p = line.split(",")
        rows.append({"time": p[0], "open": float(p[1]), "close": float(p[2]),
                     "high": float(p[3]), "low": float(p[4]), "volume": float(p[5])})
    df = pd.DataFrame(rows).set_index("time")
    df.index = pd.to_datetime(df.index)
    # EastMoney timestamp semantics (verified 2026-08-05 against live API):
    #   A-share (secid 1./0.)      → Asia/Shanghai
    #   US daily   (klt=101)       → America/New_York (dates are US trading days)
    #   US intraday (klt=5/15/60)  → Asia/Shanghai (US 16:00 close = 04:00 Beijing)
    if ticker.isdigit() and len(ticker) == 6:
        em_tz = "Asia/Shanghai"
    else:
        # _EM_KLTS values are strings ("101", "60", ...)
        em_tz = "America/New_York" if klt == "101" else "Asia/Shanghai"
    df.index = df.index.tz_localize(em_tz)
    df.index.name = "time"
    df = df[["open", "high", "low", "close", "volume"]]
    if limit and len(df) > limit:
        df = df.tail(limit)
    return df
