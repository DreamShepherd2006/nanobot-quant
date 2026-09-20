"""Sina (新浪财经) stock data source — A-share feed that works from cloud IPs.

为什么需要它：东财（``eastmoney.py``）在数据中心 IP 上会被直接断连
（2026-09-20 在 HF Space 与 Nightly 容器双双复现，换 UA/Referer/入口
都无效），而 yfinance 对 A 股的分钟数据只给约 15–20 个交易日。新浪
两个接口都不封数据中心 IP，且深度好得多：

    实测（2026-09-20，容器内直连）：
      - 5m  datalen=5001 → 5001 根  ≈ 5 个月（硬上限，超过返回 5001）
      - 1D  datalen=5001 → 5001 根  最早 2005-11
      - 1D  datalen=8000 → 6008 根  最早 2001-08（硬上限 6008）

支持周期（实测）：``5m / 15m / 30m / 1H / 1D``。
**不支持 1m 与 2H**（``scale=1`` / ``scale=120`` 返回 ``null``）。

时间戳语义：新浪返回的是**北京时间**（Asia/Shanghai）naive 字符串
（分钟 ``YYYY-MM-DD HH:MM:SS``、日线 ``YYYY-MM-DD``），这里统一
``tz_localize("Asia/Shanghai")``，与东财 A 股分支保持一致。

kind=research：股票源只服务分析页展示与回测，不参与执行。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

# 实测支持矩阵（见模块 docstring）；scale=1 / 120 返回 null，不声明。
_SINA_SCALES = {"5m": "5", "15m": "15", "30m": "30", "1H": "60", "1D": "240"}
_SPAN = {"5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "1D": 86400}

# 单次 datalen 的硬上限：实测 5001 根分钟 / 6008 根日线。
# 传更大值不报错、只返回上限根数，所以这里就地截断并留痕（见 _warn_clip）。
_SINA_MAX_BARS = 6008

_KLINE_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")
_QUOTE_URL = "https://hq.sinajs.cn/list="

# 两个接口都要求 Referer，否则 403。
_SINA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


def _warn_clip(msg: str) -> None:
    """Clipping must leave a visible trace, never degrade silently."""
    print(f"[SINA] {msg}", file=sys.stderr, flush=True)


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=_SINA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def sina_symbol(ticker: str) -> str:
    """6-digit A-share code → Sina's ``sh``/``sz`` prefixed symbol.

    SSE codes start with 6/9 (stocks) or 5 (ETF); everything else numeric
    is treated as SZSE.  Non-A-share symbols are rejected — 新浪 A 股接口
    只覆盖沪深两市，美股/港股请用 eastmoney / yfinance。
    """
    t = (ticker or "").strip()
    if t.isdigit() and len(t) == 6:
        return ("sh" if t.startswith(("6", "9", "5")) else "sz") + t
    raise ValueError(f"新浪源仅支持 6 位沪深 A 股代码，收到 {ticker!r}")


def fetch_kline(ticker: str, bar: str = "1D", limit: int = 60,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """Sina kline API → normalised DataFrame (lowercase ohlcv, tz Asia/Shanghai).

    ``limit`` caps how many bars are returned.  When ``start`` is given the
    fetch size is derived from the requested span (Sina has no beg/end
    parameters — only "latest N bars"), then trimmed locally to
    ``[start, end]``.
    """
    if bar not in _SINA_SCALES:
        raise ValueError(
            f"新浪数据源不支持 {bar} 周期（支持 {'/'.join(_SINA_SCALES)}；1m/2H 接口返回空）"
        )
    scale = _SINA_SCALES[bar]
    span = _SPAN[bar]

    if start is not None:
        ref = end or datetime.now()
        if ref.tzinfo is not None:
            ref = ref.replace(tzinfo=None)
        if start.tzinfo is not None:
            start = start.replace(tzinfo=None)
        need = int((ref - start).total_seconds() // span) + 10
    else:
        need = max(int(limit) * 2, 300)       # 多拉一倍，便于本地裁剪
    if need > _SINA_MAX_BARS:
        _warn_clip(f"{ticker}/{bar}: 需要 {need} 根 > 接口上限 {_SINA_MAX_BARS}，"
                   f"只取最近 {_SINA_MAX_BARS} 根（更早的历史拿不到）")
        need = _SINA_MAX_BARS

    url = f"{_KLINE_URL}?symbol={sina_symbol(ticker)}&scale={scale}&ma=no&datalen={need}"
    raw = _get(url)
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"新浪返回非 JSON: {raw[:120]!r}") from exc
    if not arr:
        raise RuntimeError(f"新浪无数据: {ticker} {bar}（接口返回空）")

    df = pd.DataFrame(arr)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # volume 字段名实测为 "volume"，个别周期缺失时补 0（不静默丢列）
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    else:
        df["volume"] = 0.0
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        raise RuntimeError(f"新浪无有效 K 线: {ticker} {bar}")
    df.index = pd.to_datetime(df["day"])
    df.index = df.index.tz_localize("Asia/Shanghai")
    df.index.name = "time"
    df = df[["open", "high", "low", "close", "volume"]]

    tz = "Asia/Shanghai"
    if start is not None:
        df = df[df.index >= pd.Timestamp(start.replace(tzinfo=None), tz=tz)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end.replace(tzinfo=None), tz=tz)]
    if limit and len(df) > limit:
        df = df.tail(limit)
    return df


def get_price(ticker: str) -> float:
    """Latest A-share price from ``hq.sinajs.cn`` (0.0 on failure, fail-closed).

    Response is GBK-encoded: ``var hq_str_sh600519="名称,今开,昨收,现价,...";``
    """
    try:
        url = _QUOTE_URL + sina_symbol(ticker)
        req = urllib.request.Request(url, headers=_SINA_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("gbk", "replace")
        for line in text.strip().splitlines():
            if '"' not in line:
                continue
            body = line.split('"')[1]
            parts = body.split(",")
            if len(parts) > 3:
                px = float(parts[3])
                if px > 0:
                    return px
                # 现价可能为 0（停牌/盘前）→ 退到昨收
                prev = float(parts[2])
                if prev > 0:
                    return prev
        return 0.0
    except Exception:  # noqa: BLE001 — 取价失败按 0.0 返回，调用方 fail-closed
        return 0.0
