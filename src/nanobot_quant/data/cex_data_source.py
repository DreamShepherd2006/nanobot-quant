"""Lumibot DataSource backed by CEX spot candles (Gate / OKX CEX).

Execution and signal data are same-exchange per channel: Gate 通道 → ``gate_cex``；
OKX CEX 通道 → ``okx_cex``（2026-10-29 P3「注册 spec 即接入」）。
同一实现按 ``source`` 参数分派，不另写第二套（docs/quant-system.md §18）。
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from lumibot.data_sources import DataSource

from nanobot_quant.data.kline_cache import KlineCache
from nanobot_quant.data_sources import get_data_source
from nanobot_quant.data_sources.periods import lumibot_bar_map

logger = logging.getLogger("nanobot_quant.data.cex")

# 动态映射：lumibot 风格键 + gate_cex spec 声明周期（16 项，大小写双键），
# 新周期（3m/2H/6H/8H/12H/3D/7D/30D）随 spec.bars 自动覆盖（2026-08-24 方案 C）。
_BAR_MAP = lumibot_bar_map("gate_cex")

_DEFAULT_BAR = "1D"


class CexDataSource(DataSource):
    """Lumibot DataSource wrapper over CEX spot candles.

    默认 ``SOURCE="gate_cex"``（Gate 通道，行为不变）；``source="okx_cex"`` 时
    同一实现接 OKX CEX 现货——两个通道的 K 线形态/列名/「已收盘才算」契约一致
    （都经数据源注册表，且都在 fetcher 层丢弃进行中 bar），故复用同一份实现，
    不另写第二套（用户原则：已验证的方法直接复用，差异走参数）［2026-10-29 P3］。
    """

    SOURCE = "gate_cex"

    # 数据源契约：gate_cex_data.rows_to_df 已丢弃进行中 bar（closed=false，
    # 仅返回已收盘 K 线）。策略层据此不再多拉 1 根/再丢弃，避免双重丢弃
    # 导致 bars=119 < min_history 永久 SKIP（2026-08-17 A 修复第二部分）。
    # okx_cex_data.fetch_kline 同样过滤 confirm="0"（2026-10-29 核对）。
    drops_in_progress_bars = True

    def __init__(self, tokens_json: Optional[list[dict]] = None,
                 use_cache: bool = True, source: Optional[str] = None,
                 **kwargs):
        super().__init__(**kwargs)
        self._tokens_json = tokens_json or []
        self._use_cache = use_cache
        # 执行通道数据源：与 CHANNEL_DATA_SOURCE 同源，由 broker 工厂显式传入
        self._source = source or self.SOURCE
        self._bar_map = lumibot_bar_map(self._source)
        self._cache = KlineCache(
            self._fetch_kline, fetch_range=self._fetch_kline_range
        ) if use_cache else None

    def _fetch_kline(self, symbol: str, bar: str, limit: int) -> "pd.DataFrame":
        """Registry fetch + non-empty guard (shared by full/incremental paths)."""
        df = get_data_source(self._source).fetch_kline(symbol, bar=bar, limit=limit)
        if df is None or df.empty:
            raise RuntimeError(f"No {self._source} kline for {symbol} (bar={bar})")
        return df

    def _fetch_kline_range(self, symbol: str, bar: str,
                           start_ts: int, end_ts: int) -> "pd.DataFrame":
        """to_ts 区间拉取：[start_ts, end_ts]（unix 秒）。

        2026-08-28：增量改走区间（to_ts 语义）而非 limit——绕开 limit 路径
        CDN 缓存窗口（低流动性币 append 可能固化旧价）；经数据源 start/end →
        分页拉取（缺口自动补齐、超深度截断由 paged 处理）。
        """
        from datetime import datetime, timezone
        df = get_data_source(self._source).fetch_kline(
            symbol, bar=bar, limit=1000,
            start=datetime.fromtimestamp(start_ts, tz=timezone.utc),
            end=datetime.fromtimestamp(end_ts, tz=timezone.utc),
        )
        if df is None or df.empty:
            raise RuntimeError(f"No {self._source} kline range for {symbol} (bar={bar})")
        return df

    def get_chains(self, asset=None, quote=None):
        return {}

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
        """Return lumibot Bars for the asset from Gate spot candles.

        K 线统一经数据源注册表（gate_cex = CEX 执行通道同所）获取；S2 增量
        K 线缓存（docs/quant-system.md §22.6）：首轮全量预取，后续每轮只拉
        最近 2 根增量合并，缺口自动全量重拉。
        """
        symbol = asset.symbol
        # bar: 前缀 = live 直拉场景粒度（策略对 live broker 添加，lumibot
        # 无法解析 → 原样透传）；removeprefix 后按原生粒度直拉（如
        # "bar:5min" → 5m），绕开 lumibot multi-timeframe 转换。
        _raw_ts = str(timestep or "").lower().removeprefix("bar:")
        bar = self._bar_map.get(_raw_ts)
        if bar is None:
            # 缺键回退必须显式吐声：静默落 1D 会让「改了周期却不动结果」
            # 极难自查（不同数据源可用周期不同：gate 16 项 / okx_cex 7 项）。
            _miss = getattr(self, "_missing_ts_logged", set())
            if _raw_ts not in _miss:
                _miss.add(_raw_ts)
                self._missing_ts_logged = _miss
                print(
                    f"[DIAG] {self._source}: 周期 {_raw_ts!r} 未在该数据源周期表"
                    f"（{sorted(set(self._bar_map))}）——已回退 {_DEFAULT_BAR}；"
                    "若场景要用该周期，请在数据源 spec.bars 注册",
                    file=sys.stderr, flush=True,
                )
            bar = _DEFAULT_BAR
        limit = max(1, min(int(length), 1000))
        if self._cache is not None:
            df = self._cache.get(symbol, bar, limit)
            if df is None:
                # 2026-08-28：K 线缺口 → 本轮跳过（fail-closed，不基于缺口
                # 数据算信号）；下轮区间拉取自动补齐，连续缺口持续报 GAP。
                return None
        else:
            df = self._fetch_kline(symbol, bar, limit)
        # lumibot v4.5.78 Bars.__init__ 构造时访问小写列 df["close"] 派生 return 列；
        # 而 gate_cex 数据源（rows_to_df）输出大写列 Open/High/Low/Close/Volume（td-table
        # 页面契约）——列名不一致会抛 KeyError: 'close'（2026-08-17 A 修复）。此处统一
        # 小写化对齐 lumibot 契约；策略层 col_map 再映射回大写供 calculate() 使用。
        df = df.rename(columns=str.lower)
        from lumibot.entities import Bars
        return Bars(df, self._source, asset)

    def get_last_price(self, asset, quote=None, exchange=None):
        """Last price for the asset from the same exchange's public ticker."""
        p = get_data_source(self._source).get_price(asset.symbol)
        return p if p else None

    def get_timestamp(self):
        import time
        return time.time()
