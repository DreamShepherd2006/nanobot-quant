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

_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def _map_bar(bar) -> str:
    """统一周期名 → Gate interval（从 gate_cex spec 读，fail-closed）。

    2026-08-24 方案 C：周期由数据源注册表声明（DataSourceSpec.interval_map），
    不再各自硬编码映射表。不支持的周期抛 KeyError，不静默回退到日线。
    """
    from nanobot_quant.data_sources import get_data_source

    return get_data_source("gate_cex").interval_for(str(bar))


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
            label = "HTTP 400"
            try:
                body = json.loads(e.read().decode() or "{}")
                label = body.get("label") or body.get("message") or label
            except (ValueError, OSError):
                pass
            label_l = str(label).lower()
            # 历史深度上限/参数边界（如 1m 最多最近 ~10000 根，Gate 返回
            # label=INVALID_PARAM_VALUE / "Maximum ... points ago"）是参数限制
            # 而非交易对问题——不黑名单，由调用方（翻页拉全量）捕获后截断；
            # 只有真正的「无交易对/已下架」（INVALID_CURRENCY_PAIR 等）才黑名单。
            if ("too long ago" in label_l or "maximum" in label_l
                    or "points ago" in label_l
                    or label_l == "invalid_param_value"):
                raise
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


def fetch_gate_kline_range_paged(pair: str, start_ts: int, end_ts: int,
                                 bar: str = "1D") -> pd.DataFrame:
    """Page backwards through history to fetch all closed candles in
    [start_ts, end_ts] (unix seconds) for a Gate pair.

    每批最多 1000 根；服务端升序返回 ``to`` 之前的最近 limit 根，翻页用
    ``to = 上一批最早一根 − interval`` 继续向前（2026-08-23 实测确认无缝衔接）。

    深度限制：1m 粒度服务端只允许最近 ~10000 根（"Maximum 10000 points
    ago"，实测第 10 批触发）——触发时截断保留已拉批次，不报错；调用方
    从返回 DataFrame 的 index 起点即可知实际深度。其他 400（无交易对/已下架）
    由 ``_request`` 黑名单逻辑处理（"too long ago" 不再误入黑名单）。
    """
    interval = _map_bar(bar)
    # 统一周期名 → 秒（单一事实源 = periods.INTERVAL_SECONDS，2026-08-24 方案 C；
    # 之前本地表缺新周期会静默落到 86400 导致翻页大缺口）
    from nanobot_quant.data_sources.periods import INTERVAL_SECONDS

    step = INTERVAL_SECONDS[str(bar)]
    page_to = int(end_ts)
    batches: list = []
    guard = 0
    while page_to > int(start_ts) and guard < 500:
        guard += 1
        try:
            rows = _request(pair, interval, 1000, to_ts=page_to)
        except urllib.error.HTTPError as e:
            # 深度上限 400（已由 _request 判别为非黑名单原因）——截断停止
            if e.code == 400:
                break
            raise
        if not rows:
            break
        batches.append(rows)
        oldest = int(rows[0][0])
        if oldest <= int(start_ts):
            break
        page_to = oldest - step
    if not batches:
        return pd.DataFrame(columns=_COLUMNS)
    frames = [rows_to_df(b) for b in batches]
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    start_dt = datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
    end_dt = datetime.fromtimestamp(int(end_ts), tz=timezone.utc)
    return df[(df.index >= start_dt) & (df.index <= end_dt)]


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
