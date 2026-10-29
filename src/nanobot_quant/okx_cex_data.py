"""OKX CEX market data adapter.

Fetches candlestick data from OKX v5 REST API (`/api/v5/market/candles`)
and returns pandas DataFrames compatible with TD Sequential calculations.

Public endpoints — no API key required for market data.

Usage::

    from nanobot_quant.okx_cex_data import fetch_kline

    df = fetch_kline("XSPCX")               # 1D bars for tokenized SpaceX
    df = fetch_kline("BTC", bar="4H")       # 4-hour bars for BTC
    df = fetch_kline("XMETA", limit=250)    # 250 bars for Meta
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

from nanobot_quant.kline_paging import KlineRangeTooLarge, max_bars_for, paginate_backwards

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.okx.com"
_CANDLES_PATH = "/api/v5/market/candles"
# 历史 K 线专用端点：只有它能向前翻页（``candles`` 恒返回最近 300 根，
# 无论传什么游标）。用法：``after=<本批最早 ts>`` = 往更早翻。
# **实证（2026-09-20）**：旧实现用 ``candles`` + ``before`` 只能拿到首屏
# （请求 90 天 15m 实际只回 242 根 = 3 天）。
_HISTORY_PATH = "/api/v5/market/history-candles"

# Rate control: OKX allows ~20 req / 2 s on public endpoints.
# We use a conservative 150 ms inter-request delay.
_RATE_DELAY = 0.150

# Mapping from our bar names to OKX bar values.
# OKX accepts: 1m, 3m, 5m, 15m, 30m, 1H, 2H, 4H, 6H, 12H, 1D, 1W, 1M, 3M
_BAR_MAP: dict[str, str] = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "1H",
    "2H": "2H",
    "4H": "4H",
    "6H": "6H",
    "12H": "12H",
    "1D": "1D",
    "1W": "1W",
    "1M": "1M",
    "3M": "3M",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_inst_id(ticker: str, quote: str = "USDT") -> str:
    """Convert a ticker to OKX instrument ID format.

    Stock tickers: prefix with X if not already prefixed.
    Crypto tickers: suffix with the quote currency.

    ``quote`` 默认 USDT（与历史行为一致，研究源与执行源同源）；CEX 执行
    broker 可传 USDC（取决于子账号资金，P3 实盘时确认）。

    >>> to_inst_id("AAPL")
    'XAAPL-USDT'
    >>> to_inst_id("XSPCX")
    'XSPCX-USDT'
    >>> to_inst_id("BTC")
    'BTC-USDT'
    >>> to_inst_id("XAUT-USDT")   # 完整 instId 原样返回
    'XAUT-USDT'
    """
    q = (quote or "USDT").upper().strip()
    ticker = ticker.upper().strip()
    # 已是完整 instId（含分隔符）→ 原样返回。否则 X 前缀的现货对会被二次加
    # 后缀：XAUT-USDT → XAUT-USDT-USDT，OKX 报 code=51001 Instrument doesn't exist
    # （SOL-USDT 之类的非 X 开头对恰好绕过，所以这个坑只在 X 开头的对暴露）。
    if "-" in ticker:
        return ticker
    if ticker.startswith("X"):
        # Already an X-prefixed tokenized stock
        return f"{ticker}-{q}"

    # If it looks like a stock ticker (1-5 letters, not a common crypto)
    # we prefix with X. Common crypto tickers are left as-is.
    _COMMON_CRYPTO = frozenset({
        "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX",
        "DOT", "LINK", "MATIC", "UNI", "SHIB", "LTC", "ETC",
        "ATOM", "FIL", "APT", "ARB", "OP", "SUI", "PEPE", "WIF",
        "BONK", "JUP", "TIA", "SEI", "STRK", "ZK", "NOT",
    })
    if ticker in _COMMON_CRYPTO or ticker.endswith("USDT"):
        return ticker if "-" in ticker else f"{ticker}-{q}"

    # Treat as stock: prefix X, suffix quote
    return f"X{ticker}-{q}"


#: 向后兼容别名（`tests/test_okx_cex_api.py` 引用旧名；P2 起对外用 `to_inst_id`）
_to_inst_id = to_inst_id


# ── instId 解析（以交易所清单为准，不靠启发式猜）─────────────────────────
_spot_ids_cache: tuple[float, frozenset[str]] | None = None
_SPOT_IDS_TTL = 600.0  # 秒


def spot_inst_ids(session: Optional[requests.Session] = None) -> Optional[frozenset[str]]:
    """全部 SPOT instId 集合（600s 进程级缓存；失败返回 None）。

    2026-10-29 根因：`to_inst_id` 靠「常见币白名单 + X 前缀股票」启发猜 instId，
    白名单外的币（SOL/BNB/AAVE）被当股票 → `XSOL-USDT` → OKX code=51001，
    TD 逐轮 DATA ERROR。启发式注定漏，故改为**查交易所清单判定**（SPOT 一次
    全量、免签名、免费），清单拉不到才退回旧启发（fail-soft 但有痕迹）。
    """
    global _spot_ids_cache
    now = time.time()
    if _spot_ids_cache and now - _spot_ids_cache[0] < _SPOT_IDS_TTL:
        return _spot_ids_cache[1]
    try:
        _sess = session or requests
        resp = _sess.get(f"{_BASE_URL}/api/v5/public/instruments",
                         params={"instType": "SPOT"}, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            logger.warning("spot instruments: code=%s msg=%s",
                           payload.get("code"), payload.get("msg"))
            return None
        ids = frozenset(str(d.get("instId") or "") for d in payload.get("data") or [])
        if not ids:
            return None
        _spot_ids_cache = (now, ids)
        return ids
    except Exception as exc:  # noqa: BLE001 — 清单是优化，失败不应打死取数
        logger.warning("spot instruments fetch failed — %s", exc)
        return None


def resolve_inst_id(ticker: str, quote: str = "USDT",
                    session: Optional[requests.Session] = None,
                    strict: bool = False) -> str:
    """ticker → 真实存在的 OKX SPOT instId（清单优先，启发式兜底）。

    >>> resolve_inst_id("BTC")      # 'BTC-USDT'
    >>> resolve_inst_id("SOL")      # 'SOL-USDT'（白名单漏洞修正）
    >>> resolve_inst_id("SPCX")     # 'XSPCX-USDT'（代币化股票）
    """
    t = (ticker or "").upper().strip()
    q = (quote or "USDT").upper().strip()
    if "-" in t:                     # 已是完整 instId
        if strict:
            ids0 = spot_inst_ids(session=session)
            if ids0 and t not in ids0:
                return ""
        return t
    ids = spot_inst_ids(session=session)
    if ids:
        # 候选顺序：给定计价币 → USDC 兜底（OKX 现货同一币常有 XXX-USDT 与
        # XXX-USDC 两组对，用户实测 SOL/ETH 两组都在）→ 代币化股票的 X 前缀。
        cands = [f"{t}-{q}"]
        if q != "USDC":
            cands.append(f"{t}-USDC")
        cands.append(f"X{t}-{q}")
        if q != "USDC":
            cands.append(f"X{t}-USDC")
        for cand in cands:
            if cand in ids:
                return cand
        logger.warning("resolve_inst_id(%s): none of %s on OKX SPOT", t, cands)
        if strict:
            return ""
    return to_inst_id(t, quote=q)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_kline(
    ticker: str,
    bar: str = "1D",
    limit: int = 100,
    *,
    session: Optional[requests.Session] = None,
) -> pd.DataFrame:
    """Fetch OHLCV candlestick data from OKX CEX.

    Parameters
    ----------
    ticker:
        Ticker symbol.  For tokenized stocks use e.g. ``"XSPCX"`` or plain
        ``"AAPL"`` (auto-prefixed to ``"XAAPL-USDT"``).  For crypto use
        ``"BTC"``, ``"ETH"``, etc.
    bar:
        Bar size.  One of ``1m``, ``3m``, ``5m``, ``15m``, ``30m``,
        ``1H``, ``2H``, ``4H``, ``6H``, ``12H``, ``1D``, ``1W``, ``1M``,
        ``3M``.  Default ``"1D"``.
    limit:
        Number of bars to request (max ~300).  Default 100.
    session:
        Optional ``requests.Session`` for connection pooling.

    Returns
    -------
    pandas.DataFrame
        Columns: ``Open``, ``High``, ``Low``, ``Close``, ``Volume``.
        Index: ``DatetimeIndex`` (UTC).
        Returns an empty DataFrame when no data is available.

    Raises
    ------
    requests.RequestException
        On HTTP / network errors.
    ValueError
        On invalid parameters.
    """
    # ── validate ──
    bar_okx = _BAR_MAP.get(bar)
    if bar_okx is None:
        raise ValueError(
            f"Unsupported bar size '{bar}'. "
            f"Supported: {', '.join(sorted(_BAR_MAP))}"
        )
    # 留 1 根给进行中 bar：OKX candles 最后一根常为 confirm=0（进行中），
    # 过滤后若只请求 limit 根就会少 1（实测 got=179 < min_history=180 →
    # 每轮 SKIP）。与 gate_cex_data「请求 limit+1、再丢进行中」同构。
    limit = max(1, min(limit, 299))
    inst_id = resolve_inst_id(ticker)

    # ── rate-limit ──
    time.sleep(_RATE_DELAY)

    # ── request ──
    _sess = session or requests
    url = f"{_BASE_URL}{_CANDLES_PATH}"
    params: dict = {
        "instId": inst_id,
        "bar": bar_okx,
        "limit": limit + 1,
    }

    resp = _sess.get(url, params=params, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != "0":
        msg = payload.get("msg", "unknown error")
        raise requests.RequestException(
            f"OKX API error (code={payload.get('code')}): {msg}"
        )

    data = payload.get("data", [])
    if not data:
        logger.warning("fetch_kline(%s, %s): no data", inst_id, bar)
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    # ── parse ──
    # Each candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    rows: list[dict] = []
    for entry in data:
        try:
            if not _is_confirmed(entry):      # 丢弃进行中 bar（confirm="0"）
                continue
            ts = datetime.fromtimestamp(int(entry[0]) / 1000, tz=timezone.utc)
            rows.append({
                "ts": ts,
                "Open": float(entry[1]),
                "High": float(entry[2]),
                "Low": float(entry[3]),
                "Close": float(entry[4]),
                "Volume": float(entry[5]),
            })
        except (IndexError, ValueError, TypeError) as exc:
            logger.debug("Skipping malformed candle entry: %s — %s", entry, exc)
            continue

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    df.set_index("ts", inplace=True)
    df.sort_index(inplace=True)

    logger.info(
        "fetch_kline(%s, %s): %d bars, %s → %s",
        inst_id, bar, len(df),
        df.index[0].strftime("%Y-%m-%d") if len(df) else "N/A",
        df.index[-1].strftime("%Y-%m-%d") if len(df) else "N/A",
    )

    return df


def fetch_ticker(ticker: str, *, session: Optional[requests.Session] = None) -> dict:
    """Fetch latest ticker data (last price, 24h stats).

    Parameters
    ----------
    ticker:
        Ticker symbol (auto-prefixed for stocks).
    session:
        Optional ``requests.Session``.

    Returns
    -------
    dict
        Keys: ``last``, ``open24h``, ``high24h``, ``low24h``, ``vol24h``.
    """
    inst_id = resolve_inst_id(ticker)
    time.sleep(_RATE_DELAY)

    _sess = session or requests
    resp = _sess.get(
        f"{_BASE_URL}/api/v5/market/ticker",
        params={"instId": inst_id},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("code") != "0" or not payload.get("data"):
        return {}

    d = payload["data"][0]
    return {
        "last": float(d.get("last", 0)),
        "open24h": float(d.get("open24h", 0)),
        "high24h": float(d.get("high24h", 0)),
        "low24h": float(d.get("low24h", 0)),
        "vol24h": float(d.get("vol24h", 0)),
    }


_ORDER_BOOK_PATH = "/api/v5/market/books"
_ORDER_BOOK_LITE_PATH = "/api/v5/market/books-lite"


def fetch_order_book(
    ticker: str,
    *,
    depth: int = 5,
    session: Optional[requests.Session] = None,
) -> dict | None:
    """Fetch current order book depth from OKX.

    Uses the public market/books endpoint (no auth).  Returns a dict with
    *best_bid* / *best_ask* / *spread_pct* / *bids* / *asks* or ``None``
    on failure.

    Parameters
    ----------
    ticker:
        Ticker symbol (e.g. ``"BTC"`` → ``"BTC-USDT"``).
    depth:
        Number of bid/ask levels to return (max 400, default 5).
    session:
        Optional ``requests.Session``.
    """
    inst_id = resolve_inst_id(ticker)
    _sess = session or requests
    url = f"{_BASE_URL}{_ORDER_BOOK_PATH}"
    url_lite = f"{_BASE_URL}{_ORDER_BOOK_LITE_PATH}"

    try:
        time.sleep(_RATE_DELAY)
        # Try full books endpoint first; fall back to books-lite
        resp = _sess.get(
            url,
            params={"instId": inst_id, "sz": depth},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException:
        try:
            time.sleep(_RATE_DELAY)
            resp = _sess.get(
                url_lite,
                params={"instId": inst_id, "sz": depth},
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            logger.warning("fetch_order_book(%s): request failed — %s", inst_id, exc)
            return None

    if payload.get("code") != "0":
        logger.warning(
            "fetch_order_book(%s): API error %s — %s",
            inst_id, payload.get("code"), payload.get("msg", "?"),
        )
        return None

    data_list = payload.get("data", [])
    if not data_list:
        return None

    book = data_list[0]  # single book entry for the instId
    bids = []
    asks = []
    for entry in book.get("bids", []):
        try:
            bids.append((float(entry[0]), float(entry[1])))
        except (IndexError, ValueError, TypeError):
            continue
    for entry in book.get("asks", []):
        try:
            asks.append((float(entry[0]), float(entry[1])))
        except (IndexError, ValueError, TypeError):
            continue

    if not bids or not asks:
        logger.warning("fetch_order_book(%s): empty book", inst_id)
        return None

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    spread_pct = ((best_ask - best_bid) / mid) * 100 if mid > 0 else 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": round(spread_pct, 4),
        "bid_depth": round(sum(q for _, q in bids), 2),
        "ask_depth": round(sum(q for _, q in asks), 2),
        "bids": bids[:depth],
        "asks": asks[:depth],
    }


def _is_confirmed(entry: list) -> bool:
    """OKX candle 第 9 个字段 confirm："1" 已收盘 / "0" 进行中。

    字段缺失时视为已收盘（兼容旧 mock 与非 OKX 形状的响应）。TD 只用已收盘
    bar（与 Gate 数据源的 closed 过滤同契约），否则最后一根半成品 bar 会让
    setup/countdown 计数抖动。
    """
    if len(entry) >= 9:
        return str(entry[8]) == "1"
    return True


def fetch_kline_range(
    ticker: str,
    start: str,
    end: str,
    bar: str = "1D",
    *,
    session: Optional[requests.Session] = None,
    max_bars: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch OHLCV data for a date range via pagination.

    OKX returns at most 300 candles per request.  This function paginates
    backwards from ``end`` to ``start`` to build a continuous range.

    Parameters
    ----------
    ticker:
        Ticker symbol (auto-prefixed for stocks, e.g. ``"AAPL"`` → ``"XAAPL-USDT"``).
    start:
        Start date as ISO string (``"2024-01-01"``).
    end:
        End date as ISO string (``"2024-12-31"``).
    bar:
        Bar size.  Default ``"1D"``.
    session:
        Optional ``requests.Session``.
    max_bars:
        单次区间根数上限（默认 :data:`MAX_BARS_PER_REQUEST` = 10,000）。
        超限抛 :class:`KlineRangeTooLarge` —— **显式报错，不静默截断**
        （OKX 没有 Gate 那种 400 深度闸门，1m 90 天要 433 页/177 秒）。

    Returns
    -------
    pandas.DataFrame
        Same format as :func:`fetch_kline`.  Sorted oldest→newest.
    """
    bar_okx = _BAR_MAP.get(bar)
    if bar_okx is None:
        raise ValueError(f"Unsupported bar size: {bar!r}")

    inst_id = resolve_inst_id(ticker)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # 限幅先于取数（fail fast）：按区间长度估算根数，超上限立即报错，
    # 不让用户等到 177 秒后才发现失败。
    cap = int(max_bars) if max_bars is not None else max_bars_for(bar)
    # 周期秒数从周期注册表单一来源读取；延迟 import 避开包初始化环路
    # （data_sources/__init__ 会反向 import 本模块做注册）。
    from nanobot_quant.data_sources.periods import INTERVAL_SECONDS

    bar_seconds = INTERVAL_SECONDS.get(bar)
    if bar_seconds and cap > 0:
        expected = (end_ms - start_ms) / (bar_seconds * 1000) + 2
        if expected > cap:
            span_days = (end_ms - start_ms) / 86_400_000
            cap_days = cap * bar_seconds / 86_400
            raise KlineRangeTooLarge(
                f"OKX CEX 单次最多 {cap:,} 根 {bar}（约 {cap_days:.1f} 天），"
                f"当前区间 {span_days:.1f} 天超过上限——请缩小区间或换更大周期"
            )

    _sess = session or requests
    # 必须用 history-candles：`candles` 端点恒返回最近 300 根、游标无效。
    url = f"{_BASE_URL}{_HISTORY_PATH}"
    # after 语义：返回「早于该 ts」的数据（不含端点）⇒ 首游标取 end+1ms
    state = {"cursor": end_ms + 1}

    def _fetch_page(cursor_ms: int) -> list:
        time.sleep(_RATE_DELAY)
        params: dict = {
            "instId": inst_id,
            "bar": bar_okx,
            "limit": 300,
            "after": int(cursor_ms),
        }
        resp = _sess.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != "0":
            raise requests.RequestException(
                f"OKX API error (code={payload.get('code')}): "
                f"{payload.get('msg', 'unknown error')}"
            )
        state["cursor"] = int(cursor_ms)
        return payload.get("data") or []

    def _next_cursor(oldest_ms: int) -> Optional[int]:
        # 游标回退到本页最早 ts；无法前进则停（防死循环）
        return oldest_ms if oldest_ms < state["cursor"] else None

    pages = paginate_backwards(
        _fetch_page,
        initial_cursor=end_ms + 1,
        start_ms=start_ms,
        page_limit=300,
        next_cursor=_next_cursor,
        label=f"okx {inst_id} {bar}",
        ts_of=lambda row: int(row[0]),
        max_bars=cap,
    )

    rows: list[dict] = []
    for batch in pages:
        for entry in batch:
            try:
                if not _is_confirmed(entry):
                    continue
                ts_ms = int(entry[0])
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts < start_dt or ts > end_dt:
                    continue
                rows.append({
                    "ts": ts,
                    "Open": float(entry[1]),
                    "High": float(entry[2]),
                    "Low": float(entry[3]),
                    "Close": float(entry[4]),
                    "Volume": float(entry[5]),
                })
            except (IndexError, ValueError, TypeError) as exc:
                logger.debug("Skipping malformed candle: %s — %s", entry, exc)
                continue

    if not rows:
        logger.warning("fetch_kline_range(%s): no data %s→%s", inst_id, start, end)
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    result = pd.DataFrame(rows).set_index("ts")
    result.sort_index(inplace=True)
    result = result[~result.index.duplicated(keep="first")]

    # Clip to requested date range
    result = result[(result.index >= start_dt) & (result.index <= end_dt)]

    logger.info(
        "fetch_kline_range(%s, %s): %d bars, %s → %s",
        inst_id, bar, len(result),
        result.index[0].strftime("%Y-%m-%d") if len(result) else "N/A",
        result.index[-1].strftime("%Y-%m-%d") if len(result) else "N/A",
    )

    return result
