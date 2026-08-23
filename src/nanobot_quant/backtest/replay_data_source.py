"""Historical replay data source for plan-B backtesting.

方案 B 历史回测（docs/quant-system.md 第二十五章）Step 2：历史推进数据源。

预拉全量历史 K 线（gate_cex 翻页拉全量）→ ``seek(ts)`` 显式定位 → 每次
``get_historical_prices`` 返回 [ts−length+1, ts] 最近窗口——**所有标的一齐
对齐到同一重放时间**。策略看到的形状与实盘完全一致（每轮「最新 length 根
已收盘 bar」），但数据源在历史区间上推进：确定性、无网络轮询。

回测驱动（Step 3）用 ``bar_times``（主标的完整时间轴）逐根推进，跳过前
``length−1`` 个时间点（策略 min_history 窗口要求，数据不足自然 SKIP）。

接口形状对齐 CexDataSource（lumibot ``Strategy.get_historical_prices``
内部经 ``self.broker.data_source.get_historical_prices(...)`` 调用，2026-08-23
从 lumibot v4.5.78 源码确认）：asset.symbol 取数、小写列名（Bars 构造契约）、
``Bars(df, SOURCE, asset)``。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from nanobot_quant.gate_cex_data import fetch_gate_kline_range_paged


def _to_ts(value) -> Optional[int]:
    """回测区间时间戳归一化：None / unix 秒 / datetime / 'YYYY-MM-DD…' → int 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, str):
        dt = datetime.strptime(value.strip(), "%Y-%m-%d")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    raise TypeError(f"无法解析回测时间戳: {value!r}")
from nanobot_quant.gate_credentials import gate_pair

_BAR_MAP = {
    # lumibot 风格键（td_live 场景 timestep）
    "minute": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "hour": "1H",
    "4hour": "4H",
    "day": "1D",
    "week": "1W",
    # Gate 风格键（driver._timestep_for 输出）——两种风格都必须命中
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "1H": "1H",
    "4h": "4H",
    "4H": "4H",
    "1d": "1D",
    "1D": "1D",
    "1w": "1W",
    "1W": "1W",
}

_DEFAULT_BAR = "1D"


class ReplayDataSource:
    """Deterministic historical replay data source (per-scene instance).

    Parameters:
        symbols: 场景标的池（symbol 名，如 ["CRCLX", "SPCX"]）。
        timestep: 场景周期（"1m"/"5min"/"15min"/"hour"/...，映射 Gate bar）。
        start_ts / end_ts: 回测区间（unix 秒；默认自动取 Gate 可拉的最深范围）。
        length: 策略窗口长度（默认 120 = min_history，驱动跳过前 length−1 根）。
        tokens_json: tokens.json 条目（gate_symbol 映射）。
        fetcher: 注入用 callable(pair, start_ts, end_ts, bar) -> DataFrame。
    """

    SOURCE = "backtest"

    # 历史数据全为已收盘 bar——无「进行中 bar」概念，策略层不需要 drop
    # （drop_in_progress 契约，对齐 gate_cex 的 rows_to_df 已过滤语义）。
    drops_in_progress_bars = False

    def __init__(
        self,
        symbols: list[str],
        timestep: str = "15min",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        length: int = 120,
        tokens_json: Optional[list[dict]] = None,
        fetcher: Optional[Callable[[str, int, int, str], pd.DataFrame]] = None,
    ):
        self._symbols = list(symbols)
        self._timestep = str(timestep)
        self._bar = _BAR_MAP.get(str(timestep or "").lower().removeprefix("bar:"),
                                 _DEFAULT_BAR)
        self._start_ts = _to_ts(start_ts)
        self._end_ts = _to_ts(end_ts)
        self._length = max(1, int(length))
        self._tokens_json = tokens_json or []
        self._fetcher = fetcher or self._default_fetch
        self._frames: dict[str, pd.DataFrame] = {}   # symbol -> 小写列 + naive/UTC index
        self._current_ts: Optional[datetime] = None
        self._bar_times: list = []                   # 主标的完整时间轴

    # ── 数据预拉 ──────────────────────────────────────────────

    def _default_fetch(self, pair: str, start_ts: int, end_ts: int,
                       bar: str) -> pd.DataFrame:
        return fetch_gate_kline_range_paged(pair, start_ts, end_ts, bar)

    def prefetch(self) -> None:
        """拉全量历史：每 symbol 一个 DataFrame（小写列，升序 UTC index）。

        主标的（第一个）的完整时间轴存为 ``bar_times``（驱动循环用）。
        """
        end_ts = self._end_ts or int(time.time())
        for symbol in self._symbols:
            pair = gate_pair(symbol, self._tokens_json)
            try:
                df = self._fetcher(pair, self._start_ts or 0, end_ts, self._bar)
            except Exception as exc:  # noqa: BLE001
                print(f"[BACKTEST] prefetch {symbol} failed: {exc}",
                      flush=True)
                df = pd.DataFrame()
            if df is None or df.empty:
                df = pd.DataFrame()
            df = df.rename(columns=str.lower).sort_index()
            self._frames[symbol] = df
        main = self._symbols[0] if self._symbols else None
        self._bar_times = list(self._frames[main].index) if main else []

    @property
    def bar_times(self) -> list:
        """主标的完整时间轴（tz-aware UTC datetime，升序）——驱动逐根推进。"""
        return self._bar_times

    @property
    def start_idx(self) -> int:
        """第一个有完整 length 窗口的时间索引（策略 min_history 要求）。"""
        return max(0, self._length - 1)

    def seek(self, ts) -> None:
        """定位当前重放时间——所有标的窗口尾对齐到 ``ts``。"""
        self._current_ts = ts

    # ── lumibot DataSource 接口 ──────────────────────────────

    def get_historical_prices(
        self,
        asset,
        length,
        timestep: str = "",
        timeshift=None,
        exchange=None,
        include_after_hours: bool = True,
        quote=None,
        return_polars: bool = False,
    ):
        """窗口 = [current_ts − length + 1, current_ts]（该 symbol 的最近 N 根）。

        形状与实盘一致：每轮策略拿到「最新 length 根已收盘 bar」。数据不足
        length 根时返回短窗口（策略 min_history 检查自然 SKIP，与实盘一致）。
        """
        symbol = asset.symbol
        df = self._frames.get(symbol)
        if df is None or df.empty or self._current_ts is None:
            empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            from lumibot.entities import Bars
            return Bars(empty, self.SOURCE, asset)
        window = df.loc[: self._current_ts].tail(max(1, int(length)))
        from lumibot.entities import Bars
        return Bars(window, self.SOURCE, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        return self.price_of(asset.symbol)

    def get_datetime(self, adjust_for_delay: bool = True):
        """当前重放时间（lumibot Broker/Strategy 契约，tz-aware UTC）。

        lumibot v4.5.78 以 ``get_datetime(adjust_for_delay=True)`` 调用；
        回测是离线重放，无数据延迟概念，忽略该参数。Broker.__init__ 会用
        返回值的 tzinfo 初始化时区；回放中返回当前游标，未定位/未拉数时
        回退最后 bar 时间或当前 UTC。
        """
        if self._current_ts is not None:
            return self._current_ts
        if self._bar_times:
            return self._bar_times[-1]
        return datetime.now(timezone.utc)

    def get_timestamp(self):
        return time.time()

    def get_timestep(self):
        return self._timestep

    # ── 回测驱动辅助 ─────────────────────────────────────────

    def price_of(self, symbol: str) -> float:
        """当前重放时间该 symbol 的收盘价（BacktestBroker 撮合用）。

        无数据/无持仓时间 → 0.0（fail-closed：broker 拒绝以 0 价成交）。
        """
        df = self._frames.get(symbol)
        if df is None or df.empty or self._current_ts is None:
            return 0.0
        tail = df.loc[: self._current_ts]
        if tail.empty:
            return 0.0
        try:
            return float(tail["close"].iloc[-1])
        except (KeyError, IndexError, ValueError, TypeError):
            return 0.0

    def net_value(self, cash_by_symbol: dict[str, float],
                  positions: dict[str, float]) -> float:
        """组合净值 = cash + Σ(qty × price_of)——驱动每 bar 记录用。"""
        total = float(sum(cash_by_symbol.values()))
        for cur, qty in positions.items():
            total += qty * self.price_of(cur)
        return total
