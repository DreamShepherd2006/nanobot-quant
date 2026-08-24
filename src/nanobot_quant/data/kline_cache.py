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


def _now_for(tail_ts: pd.Timestamp) -> pd.Timestamp:
    """UTC now，时区与缓存尾对齐（缓存 index 可能 naive UTC 或 tz-aware）。

    独立函数便于测试注入（模拟调用方跳过多轮后的 wall clock）。
    """
    now = pd.Timestamp.now(tz="UTC")
    if tail_ts.tz is None:
        now = now.tz_localize(None)
    return now

# bar 粒度 → 周期秒数：统一注册表（方案 C，16 周期全量，含 3m/2H/6H/8H/12H/3D/7D/30D）。
# 缺口判定：新 bar ts 必须恰为尾 ts + 该周期。
# 2026-08-24 回归：旧硬编码表缺 3m → fallback 60s → 3m bar 间隔 180s 被
# 误判 gap → 每轮全量重拉（S2 增量失效）。缺失键显式 KeyError（fail-closed，
# 不静默回退 60s——静默 fallback 正是该 bug 的温床）。
from nanobot_quant.data_sources.periods import INTERVAL_SECONDS as _PERIOD_SECONDS


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
        tail_ts = e.df.index[-1]
        now = _now_for(tail_ts)
        # 未知周期显式 KeyError（fail-closed），放在 try 外避免被吞成
        # incr-fail 无限重试——旧 fallback 60s 曾致 3m 每轮 gap 全量重拉。
        period = pd.Timedelta(seconds=_PERIOD_SECONDS[bar])
        try:
            # 自适应增量窗口：调用方可能跳过若干周期才访问一次（S3a 多场景
            # 调度：mid=5m 每 5 轮才访问 1m 缓存，期间产生多根新 bar）。
            # 固定 limit=2 在跳过多轮时会把第 3 根起误判为缺口 → 每轮
            # 全量重拉（120 倍退化）。按距缓存尾的时间差估算需要拉多少根：
            #   need = elapsed_bars + 2  （+2 = 1 根进行中容差 + 1 根余量）
            # elapsed 为 0（同周期内重复访问）时退化为固定 limit=2。
            elapsed = max(0, int((now - tail_ts) / period))
            need = min(max(elapsed + 2, 2), 1000)
            new = self._fetch(symbol, bar, need)
        except Exception as exc:  # 增量失败 → 保留缓存，下一轮再试
            _diag(f"incr-fail symbol={symbol}: {exc} (cache kept)")
            return
        if new is None or new.empty:
            return
        new = new.sort_index()
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
