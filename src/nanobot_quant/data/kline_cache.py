"""Incremental OHLCV cache (S2, docs/quant-system.md §22.6).

Mounted inside the live DataSources (``CexDataSource`` /
``OnchainOSDataSource``) so ``get_historical_prices`` only re-fetches the
latest 1-2 bars per loop iteration instead of the full window:

- first call (or bar/length change / insufficient rows) → full prefetch
  via ``fetch(symbol, bar, length)``
- subsequent calls → incremental: ``fetch(symbol, bar, 2)`` (latest 2 rows)
  merged by timestamp:
    * older than cache tail → ignored
    * equal to cache tail → overwrite tail row (in-progress bar value
      refresh / finalisation)
    * exactly one bar-period ahead → append
    * anything larger (gap) → full refetch, rebuild cache
- tail trimmed to ``length + keep_extra``; ``get()`` returns a copy of the
  tail ``length`` rows.

Data-source contract unchanged: Gate entries hold only closed candles
(strategy does not drop); OnchainOS entries keep the in-progress bar
(strategy drops the last row as before). The ``fetch`` callback MUST raise
or return non-empty data — this class does not validate row contents.

Memory: ~6.6 KB per symbol (120 rows × 6 float64), negligible vs. the
agent process (measurement 2026-08-20).
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

import pandas as pd

logger = logging.getLogger("nanobot_quant.data.kline_cache")


def _diag(msg: str) -> None:
    """TD 风格 stderr 诊断（gatekeeper 上下文 logger.info 被静默丢弃）。"""
    print(f"[KLINE-CACHE] {msg}", file=sys.stderr, flush=True)

# bar 粒度 → 周期秒数（缺口判定：新 bar ts 必须恰为尾 ts + 该周期）
_BAR_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1H": 3600,
    "4H": 14400,
    "1D": 86400,
    "1W": 604800,
}


class _Entry:
    __slots__ = ("bar", "length", "df")

    def __init__(self, bar: str, length: int, df: pd.DataFrame) -> None:
        self.bar = bar
        self.length = length
        self.df = df


class KlineCache:
    """Per-symbol incremental OHLCV cache for live Lumibot data sources.

    Parameters:
        fetch: ``callable(symbol, bar, limit) -> DataFrame`` — source-specific
            full fetch (same semantics as the registry ``fetch_kline``).
            Must raise on failure and never return empty data.
        keep_extra: how many extra rows beyond the requested length to keep
            after trimming (avoids rebuild on small length fluctuation).
    """

    def __init__(self, fetch: Callable[[str, str, int], pd.DataFrame],
                 keep_extra: int = 8) -> None:
        self._fetch = fetch
        self._keep_extra = max(1, int(keep_extra))
        self._entries: dict[str, _Entry] = {}

    # ── public ────────────────────────────────────────────────────

    def get(self, symbol: str, bar: str, length: int) -> pd.DataFrame:
        """Return the latest ``length`` rows for ``symbol`` (a copy).

        Rebuilds on first use / bar change / insufficient cached rows;
        otherwise applies one incremental fetch and returns the tail.
        """
        length = max(1, int(length))
        e = self._entries.get(symbol)
        if e is None or e.bar != bar or len(e.df) < length:
            e = self._rebuild(symbol, bar, length)
            self._entries[symbol] = e
        else:
            e.length = length
            self._incremental(e, symbol, bar)
        return e.df.iloc[-length:].copy()

    def clear(self) -> None:
        """Drop all cached symbols (e.g. symbol pool changed)."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    # ── internals ─────────────────────────────────────────────────

    def _rebuild(self, symbol: str, bar: str, length: int) -> _Entry:
        df = self._fetch(symbol, bar, length)
        if df is None or df.empty:
            raise RuntimeError(
                f"kline cache rebuild: no data for {symbol} (bar={bar})")
        _diag(f"prefetch symbol={symbol} bar={bar} n={len(df)}")
        return _Entry(bar, length, df.sort_index())

    def _incremental(self, e: _Entry, symbol: str, bar: str) -> None:
        try:
            new = self._fetch(symbol, bar, 2)
        except Exception as exc:  # 增量失败 → 保留缓存，下一轮再试
            _diag(f"incr-fail symbol={symbol}: {exc} (cache kept)")
            return
        if new is None or new.empty:
            return
        new = new.sort_index()
        period = pd.Timedelta(seconds=_BAR_SECONDS.get(bar, 60))
        tail_ts = e.df.index[-1]
        gap = False
        appended = 0
        for ts, row in new.iterrows():
            diff = ts - tail_ts
            if diff < pd.Timedelta(0):
                continue                     # 旧 bar（缓存内已有）
            if diff == pd.Timedelta(0):
                e.df.iloc[-1] = row          # 尾行覆盖（进行中 bar 值更新/定格）
            elif diff == period:
                e.df = pd.concat([e.df, row.to_frame().T])  # 恰 +1 周期 → append
                appended += 1
                tail_ts = ts                 # 更新尾——多根新 bar 按顺序逐根判定
            else:
                gap = True                   # 跳跃 → 缺口 → 全量重拉
                break
        if gap:
            _diag(f"gap symbol={symbol} tail={tail_ts} new={ts} → full refetch")
            rebuilt = self._rebuild(symbol, bar, e.length)
            e.bar, e.length, e.df = rebuilt.bar, rebuilt.length, rebuilt.df
        else:
            if appended:
                _diag(f"incr symbol={symbol} +{appended} cache={len(e.df)}")
            self._trim(e)

    def _trim(self, e: _Entry) -> None:
        cap = e.length + self._keep_extra
        if len(e.df) > cap:
            e.df = e.df.iloc[-cap:]
