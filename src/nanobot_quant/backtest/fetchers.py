"""回测数据源注册表（Gate CEX / OKX CEX）。

为什么需要这一层：场景回放引擎此前把数据源写死成 Gate
（``gate_pair`` + ``fetch_gate_kline_range_paged``）。OKX CEX 有长得多的
1m 历史（Gate 1m 仅 ≈6.9 天），可用来做**参数粗筛**。两个源共用同一套
反向分页器（``kline_paging.paginate_backwards``），差异只在两处：

1. 符号解析：Gate 用 ``gate_pair``（``CRCLX`` → ``CRCLX_USDT``），
   OKX 用 ``okx_ticker``（``CRCLX`` → ``XCRCL``，无该交易对则不可用）。
2. 请求形态：Gate 收 unix 秒区间，OKX 收 ISO 字符串区间。

**跨所回测的语义边界（2026-10-20 拍板：放行 + 强制标注）**

执行通道仍是 Gate 现货。用 OKX 数据回测 = 「数据源 ≠ 执行所」：

- 可回答：参数在另一家行情下的方向、闸门通过率/暴露度这类**状态量**；
- 不可回答：本次参数在 Gate 实盘会出多少笔、每笔赚多少（**事件级收益归属
  不成立**）——同日实测（C73）：1m TD 九连事件的跨所 Jaccard 仅
  0.227–0.591，而 EMA 斜率符号一致率 97.7–99.3%。

因此跨所结果一律标注 :data:`CROSS_VENUE_NOTE`，只能作参考、不作参数
裁决依据；裁决必须用与执行同所的数据（Gate）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

from nanobot_quant.data_sources.aggregate import aggregate_bars
from nanobot_quant.data_sources.periods import (
    INTERVAL_SECONDS,
    composite_factor,
    composite_source,
)
from nanobot_quant.gate_cex_data import fetch_gate_kline_range_paged
from nanobot_quant.gate_credentials import gate_pair, okx_ticker
from nanobot_quant.kline_paging import max_bars_for
from nanobot_quant.okx_cex_data import fetch_kline_range, fetch_ticker

DEFAULT_SOURCE = "gate_cex"

SOURCE_LABELS: dict[str, str] = {
    "gate_cex": "Gate CEX",
    "okx_cex": "OKX CEX",
}

CROSS_VENUE_NOTE = (
    "数据源 ≠ 执行所（Gate 现货）：本结果为跨所参考，仅用于参数粗筛，"
    "不作参数裁决依据"
)


def normalize_source(source: object) -> str:
    """数据源名归一化；未知值 fail-closed（不静默回退到默认源）。"""
    name = str(source or DEFAULT_SOURCE).strip().lower()
    if name not in SOURCE_LABELS:
        raise ValueError(
            f"不支持的回测数据源: {source!r}（支持: {sorted(SOURCE_LABELS)}）"
        )
    return name


def source_label(source: object) -> str:
    return SOURCE_LABELS[normalize_source(source)]


def is_cross_venue(source: object) -> bool:
    """该数据源是否与执行所（Gate）不同源。"""
    return normalize_source(source) != DEFAULT_SOURCE


def resolve_symbol(
    symbol: str, source: object, tokens_json: Optional[list[dict]] = None
) -> str:
    """symbol → 该源的交易对/ticker（Gate: ``CRCLX_USDT``；OKX: ``XCRCL``）。"""
    src = normalize_source(source)
    tokens_json = tokens_json or []
    if src == "okx_cex":
        return okx_ticker(symbol, tokens_json)
    return gate_pair(symbol, tokens_json)


def _okx_fetch(ticker: str, start_ts: int, end_ts: int, bar: str) -> pd.DataFrame:
    """OKX 适配：unix 秒 → ISO 字符串（okx_cex_data 只吃 ISO）。

    ``start_ts <= 0`` 表示调用方没指定起点（「拉满」）——OKX 没有深度闸门，
    一拉到底要几百页，故按单次上限截到最近的窗口；驱动会把这一事实写进
    ``source_note`` 并展示到页面/报告（**不是静默截断**）。
    """
    end = int(end_ts)
    start = int(start_ts)
    if start <= 0:
        bar_seconds = INTERVAL_SECONDS.get(bar) or 60
        start = end - max_bars_for(bar) * bar_seconds
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
    return fetch_kline_range(ticker, start_iso, end_iso, bar=bar)


def _composite_fetch(
    fetch: Callable[[str, int, int, str], pd.DataFrame],
) -> Callable[[str, int, int, str], pd.DataFrame]:
    """包一层合成周期支持（2026-10-22）：目标周期不在原生档时取源周期再聚合。

    源周期与前缀对齐很关键：
    - ``start_ts`` 先**向下对齐到目标格**，否则首个分组会从半根开始（会被
      聚合层当作边界截断丢掉，静默少一根）；
    - ``start_ts <= 0``（“拉满”）按**源周期**的深度预算截窗口（10000 × 源
      秒）——与 ``_okx_fetch`` 同口径，不静默超限。
    """

    def wrapper(pair: str, start_ts: int, end_ts: int,
                bar: str = "1D") -> pd.DataFrame:
        src = composite_source(bar)
        if src is None:
            return fetch(pair, start_ts, end_ts, bar)
        factor = int(composite_factor(bar) or 1)
        tgt_sec = int(INTERVAL_SECONDS[bar])
        src_sec = int(INTERVAL_SECONDS[src])
        start = int(start_ts)
        end = int(end_ts)
        if start > 0:
            start = (start // tgt_sec) * tgt_sec
        else:
            start = end - max_bars_for(src) * src_sec
        df_src = fetch(pair, start, end, src)
        out = aggregate_bars(df_src, period=bar, source=src, factor=factor)
        # 诊断（2026-10-22）：合成档取数全链路在 Runtime logs 里可见——
        # 否则“聚合后根数为 0 / 覆盖不足”只能从报告倒推。走 stderr，
        # 避免污染 MCP stdio JSON-RPC（回测工具也跑在 stdio server 里）。
        print(
            f"[BACKTEST] 合成周期 {bar} ← {src}×{factor} pair={pair} "
            f"src_bars={len(df_src)} out_bars={len(out)}",
            file=sys.stderr,
            flush=True,
        )
        return out

    # 供测试与诊断核对“底层究竟是哪条取数路径”（不参与业务逻辑）
    wrapper.__wrapped_fetch__ = fetch
    return wrapper


def make_fetcher(source: object) -> Callable[[str, int, int, str], pd.DataFrame]:
    """按源返回 ``fetcher(pair, start_ts, end_ts, bar) -> DataFrame``。

    合成周期（2m/4m/6m/10m/20m/45m）对调用方透明：取源周期 → 整除聚合。
    """
    src = normalize_source(source)
    base = _okx_fetch if src == "okx_cex" else fetch_gate_kline_range_paged
    return _composite_fetch(base)


def unavailable_symbols(
    symbols: list[str],
    source: object,
    tokens_json: Optional[list[dict]] = None,
) -> list[tuple[str, str]]:
    """启动前探测：返回 ``[(symbol, 原因)]`` —— 该源取不到行情的标的。

    只在 OKX 源做探测（Gate 侧标的池本来就按 Gate 交易对维护）。探测失败
    （含网络错误）一律视为不可用——fail-closed，不带病开跑。
    """
    src = normalize_source(source)
    if src != "okx_cex":
        return []
    bad: list[tuple[str, str]] = []
    for symbol in symbols:
        ticker = resolve_symbol(symbol, src, tokens_json)
        try:
            info = fetch_ticker(ticker)
        except Exception as exc:  # noqa: BLE001 — 探测失败即视为不可用
            bad.append((symbol, f"{ticker}: {type(exc).__name__}: {exc}"))
            continue
        # OKX 对不存在的交易对不报错、返回空 dict（实测 NOTACOIN → {}），
        # 只看异常会漏掉「标的静默变少」——必须看有无最新价。
        if not info or not info.get("last"):
            bad.append((symbol, f"{ticker}: 无行情（无该交易对或已下架）"))
    return bad


def max_bars_for_source(source: object, bar: str) -> Optional[int]:
    """该源该周期的单次上限（Gate 由服务端深度闸门兜底 → 本地不设闸）。"""
    if normalize_source(source) == "okx_cex":
        return max_bars_for(bar)
    return None
