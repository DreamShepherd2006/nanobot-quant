"""Gate.io spot candlesticks — data side of the CEX execution channel.

Execution (CexBroker) and signal data both come from Gate (same-exchange),
so tokenized assets that exist only on Gate (e.g. CRCLX_USDT) work
end-to-end. Public REST: GET /api/v4/spot/candlesticks (no API key).

Gate row format (array, ascending by ts):
    [ts(sec), quote_volume, close, high, low, open, base_volume, closed]

The trailing ``closed`` flag marks the in-progress bar (false) — it is
dropped so signals are always computed on closed bars (same rule as the
on-chain DEX live path, docs/quant-system.md 方案 C).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

_API = "https://api.gateio.ws/api/v4/spot/candlesticks"

# 黑名单：symbol -> 原因。Gate 无此交易对/已下架的币（如 MU、VSC）首次查询失败后
# 记录，后续查询直接短路（不再每轮发请求刷屏）。TD 循环重启时由 td_live 调用
# clear_blacklist() 清除——用户自行处理后重启循环即重新探测。
_BLACKLIST: dict[str, str] = {}


def blacklist_reason(symbol: str) -> Optional[str]:
    """黑名单原因；不在黑名单返回 None。"""
    return _BLACKLIST.get(str(symbol).upper())


def mark_blacklisted(symbol: str, reason: str) -> None:
    """记录黑名单并打印一次原因（stderr，gatekeeper 可见）。"""
    key = str(symbol).upper()
    if key in _BLACKLIST:
        return
    _BLACKLIST[key] = reason
    print(
        f"[DIAG] CEX blacklist {key}: {reason} — 停止查询，重启 TD 循环后重新探测",
        file=sys.stderr, flush=True,
    )


def clear_blacklist() -> None:
    """清空黑名单（TD 循环重启时调用，重新探测所有标的）。"""
    _BLACKLIST.clear()


def _symbol_of(pair: str) -> str:
    """Gate pair（如 CRCLX_USDT）→ base symbol（CRCLX）。"""
    return str(pair).split("_")[0].upper()

# td-table / lumibot bar names -> Gate interval
_BAR_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1h", "4H": "4h", "1D": "1d", "1W": "7d",
}

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _map_bar(bar) -> str:
    return _BAR_MAP.get(str(bar), "1d")


def _request(pair: str, interval: str, limit: int,
             from_ts: Optional[int] = None, to_ts: Optional[int] = None) -> list:
    sym = _symbol_of(pair)
    reason = blacklist_reason(sym)
    if reason:
        raise RuntimeError(f"{sym} 已停止查询（{reason}）——重启 TD 循环后重新探测")
    params = {
        "currency_pair": pair,
        "interval": interval,
        "limit": max(1, min(int(limit), 1000)),
    }
    if from_ts:
        params["from"] = int(from_ts)
    if to_ts:
        params["to"] = int(to_ts)
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # Gate 无此交易对（如 MU_USDT）——永久性错误，进黑名单停止查询
            label = "HTTP 400"
            try:
                body = json.loads(e.read().decode() or "{}")
                label = body.get("label") or body.get("message") or label
            except (ValueError, OSError):
                pass
            mark_blacklisted(sym, f"Gate 无此交易对/已下架 ({label})")
        raise


def rows_to_df(rows: list) -> pd.DataFrame:
    """Gate candlestick rows -> OnchainOS-shaped DataFrame (UTC index).

    Drops in-progress bars (``closed == false``) and malformed rows.
    Column order: Open/High/Low/Close/Volume.
    """
    recs = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 8:
            continue
        if str(r[7]).lower() == "false":
            continue  # in-progress bar — not closed
        try:
            recs.append({
                "Open": float(r[5]),
                "High": float(r[3]),
                "Low": float(r[4]),
                "Close": float(r[2]),
                "Volume": float(r[6]),
                "_ts": datetime.fromtimestamp(int(r[0]), tz=timezone.utc),
            })
        except (ValueError, TypeError):
            continue
    if not recs:
        return pd.DataFrame(columns=_COLUMNS)
    df = pd.DataFrame(recs).set_index("_ts")
    df.index.name = None
    return df[_COLUMNS]


def fetch_gate_kline(pair: str, bar: str = "1D", limit: int = 120) -> pd.DataFrame:
    """Latest ``limit`` closed candles for a Gate spot pair (e.g. CRCLX_USDT).

    Gate 的 limit 语义 = 返回 limit 根（含最后一根进行中 closed=false）——
    请求 limit+1，rows_to_df 过滤进行中后正好返回 limit 根已收盘
    （2026-08-17 A 修复第三部分：requested=120 got=119 差 1 根永久 SKIP）。
    """
    rows = _request(pair, _map_bar(bar), limit + 1)
    return rows_to_df(rows)


def fetch_gate_kline_range(pair: str, start_ts: int, end_ts: int,
                           bar: str = "1D") -> pd.DataFrame:
    """Closed candles in [start_ts, end_ts] (unix seconds) for a Gate pair."""
    rows = _request(pair, _map_bar(bar), 1000, from_ts=start_ts, to_ts=end_ts)
    return rows_to_df(rows)


def fetch_gate_ticker(pair: str) -> Optional[dict]:
    """GET /spot/tickers?currency_pair= -> latest ticker dict (public, no key).

    Falls back to the list endpoint shape used by the broker: returns the
    first entry, whose ``last`` field is the last traded price.
    """
    sym = _symbol_of(pair)
    if blacklist_reason(sym):
        return None  # 黑名单内——不再查询
    url = ("https://api.gateio.ws/api/v4/spot/tickers?currency_pair="
           + urllib.parse.quote(pair))
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "[]")
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # 已下架币（如 VSC delisted）——永久性错误，进黑名单停止查询
            label = "HTTP 400"
            try:
                body = json.loads(e.read().decode() or "{}")
                label = body.get("label") or body.get("message") or label
            except (ValueError, OSError):
                pass
            mark_blacklisted(sym, f"Gate 已下架/无行情 ({label})")
        return None
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict) and data:
        return data
    return None


def fetch_gate_order_book(pair: str, depth: int = 5) -> Optional[dict]:
    """GET /spot/order_book?currency_pair=&limit= -> depth summary (public).

    Returns ``{"best_bid", "best_ask", "spread_pct", "bids", "asks"}``
    (same shape as the OKX CEX order book used for VT grounding) or
    ``None`` on failure.  ``bids``/``asks`` are ``[price, amount]`` lists.
    """
    url = ("https://api.gateio.ws/api/v4/spot/order_book?"
           + urllib.parse.urlencode({
               "currency_pair": pair,
               "limit": max(1, min(int(depth), 100)),
           }))
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode() or "{}")
    except (urllib.error.HTTPError, OSError, ValueError):
        return None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    try:
        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None
    except (TypeError, ValueError, IndexError):
        best_bid = best_ask = None
    spread_pct = None
    if best_bid and best_ask:
        spread_pct = (best_ask - best_bid) / best_bid * 100.0
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread_pct,
        "bids": [[float(x), float(y)] for x, y in bids],
        "asks": [[float(x), float(y)] for x, y in asks],
    }
