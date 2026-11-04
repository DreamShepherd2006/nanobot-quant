"""OKX CEX data source — retained for future business-quant integration.

现状（2026-08-15 拍板）：OKX CEX 后续会接入执行，但目前业务量化部分
尚未完成，故 kind=research（仅回测/展示，不参与执行）。接入执行时：
注册表条目改 kind=executable + 注册 BrokerSpec（含 data_source="okx_cex"）
+ EXECUTION_CHANNELS/enum_groups 加一项。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from nanobot_quant.gate_credentials import load_tokens_json, okx_ticker
from nanobot_quant.okx_cex_data import (
    fetch_kline as _fetch_kline_okx,
    fetch_kline_range as _fetch_kline_range_okx,
    fetch_order_book,
    fetch_ticker,
)
from nanobot_quant.data_sources.periods import INTERVAL_SECONDS

# `candles` 端点恒返回最近约 300 根（请求 limit 更大也只回这么多）。
_SINGLE_FETCH_MAX = 299


def _interval_seconds(bar: str) -> int:
    sec = INTERVAL_SECONDS.get(bar)
    if not sec:
        raise ValueError(f"unknown bar {bar!r} for paged fetch")
    return sec


def fetch_kline(symbol, bar="1D", limit=120,
                start: Optional[datetime] = None,
                end: Optional[datetime] = None) -> pd.DataFrame:
    """OKX CEX candles for ``symbol`` (instId via ``okx_ticker``)."""
    inst = okx_ticker(symbol, load_tokens_json())
    if start and end:
        # 必须保留**时分秒**：KlineCache 的增量按 [缓存尾, now] 传秒级边界，
        # 截断成 `%Y-%m-%d` 会把区间压成「当天 00:00 → 当天 00:00」——
        # 每轮只拿到当天第一根（或空），缓存从此卡死不动，且因为
        # 「无新 bar」是静默分支，页面数据冻结数小时也不会报错
        # （2026-10-30 实测：4 标的最后 bar 全部停在 00:00 UTC，滞后 3.7h）。
        # 显式区间：上限按本源的 ``max_backfill_days`` 换算（2026-11-03 C103），
        # 不能用全局 10,000 根——那是护栏而非平台限制，OKX 1m 实测 ≥260 天。
        from nanobot_quant.kline_paging import max_bars_for

        return _fetch_kline_range_okx(inst,
                                      start=start.isoformat(),
                                      end=end.isoformat(),
                                      bar=bar,
                                      max_bars=max_bars_for(bar, "okx_cex"))
    if limit > _SINGLE_FETCH_MAX:
        # 单次 `candles` 只回最近 ~300 根 ⇒ 超过时必须走 history-candles
        # 反向分页。2026-11-01 C95 实测：EMA 折价门 n=610 需 2137 根，
        # 单次路径只回 299 根 → KlineCache 每轮全量重建（len < length 恒真）
        # 且策略 `bars=299 < min_history=300` 每轮 SKIP，页面显示 🟢 运行中
        # 而实际一笔不交易。
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(seconds=_interval_seconds(bar) * (limit + 2))
        df = _fetch_kline_range_okx(inst,
                                    start=start_dt.isoformat(),
                                    end=end_dt.isoformat(),
                                    bar=bar)
        return df.tail(limit)
    return _fetch_kline_okx(inst, bar=bar, limit=limit)


def get_price(symbol) -> Optional[float]:
    """Latest OKX CEX last price (``ticker.last``)."""
    t = fetch_ticker(okx_ticker(symbol, load_tokens_json()))
    if not t:
        return None
    try:
        return float(t.get("last") or 0.0)
    except (TypeError, ValueError):
        return None


def order_book(symbol, depth: int = 5) -> Optional[dict]:
    """OKX CEX order-book depth (public endpoint)."""
    return fetch_order_book(okx_ticker(symbol, load_tokens_json()), depth=depth)


def ticker(symbol) -> Optional[dict]:
    """OKX CEX ticker snapshot (native fields, no mapping needed)."""
    t = fetch_ticker(okx_ticker(symbol, load_tokens_json()))
    if not t:
        return None
    return {k: t.get(k) for k in ("last", "bid", "ask", "high24h", "low24h", "vol24h")}
