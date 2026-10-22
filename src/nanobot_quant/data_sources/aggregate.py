"""合成周期聚合（2026-10-22）：原生 K 线 → 目标周期，UTC 整除对齐。

只做**精确、可校验**的聚合，不做任何推断：

- 目标周期 = 源周期 × 整数因子（由 ``periods.COMPOSITE_PERIODS`` 声明，
  准入规则见该模块注释：不整除一律不做）；
- 按 UTC epoch 整除分组（``ts // target_sec``），**不用** pandas resample
  的日历/时区推断——日线以上与 A 股会话会因此错位；
- OHLC = 首/最大/最小/末；成交量求和；
- **内部缺口 fail-closed**：任一分组内源 bar 数不足或时点不连续 → 抛
  ``ValueError`` 并给出首个缺口时点。静默聚合会让 high/low 偏小、进而
  改变 TD 计数（跨所实测：收盘价差 0.005–0.027% 就能让 buy9 触发集合
  Jaccard 掉到 0.23–0.59）；
- **边界不完整组丢弃**（序列开头从半根开始、末尾未收盘）——与数据源
  ``drop_in_progress`` 契约一致，属正常截断而非缺口。

资源量级（实测，2026-10-22）：聚合本身 ≈ 4 ms/次（两次 groupby），
内存 ≈ 48 B/根；真正的成本是取源周期 K 线的网络请求。
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.data_sources.periods import INTERVAL_SECONDS

OUTPUT_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

_NANOS = 10 ** 9


def aggregate_bars(
    df: pd.DataFrame,
    *,
    period: str,
    source: str,
    factor: int,
) -> pd.DataFrame:
    """把 ``source`` 周期的 K 线精确聚合为 ``period`` 周期。

    参数
    ----
    df:
        源周期 K 线（index 为时间，列含 open/high/low/close/volume，大小写均可）。
    period / source / factor:
        目标周期名、源周期名、聚合因子；``INTERVAL_SECONDS`` 必须满足
        ``target == source × factor``，否则直接报错（配置错了就别算）。
    """
    if int(factor) < 2:
        raise ValueError(f"聚合因子非法: {factor}（合成周期至少 2）")
    src_sec = int(INTERVAL_SECONDS[source])
    tgt_sec = int(INTERVAL_SECONDS[period])
    if tgt_sec != src_sec * int(factor):
        raise ValueError(
            f"周期不整除: {period}={tgt_sec}s ≠ {source}={src_sec}s × {factor}"
        )

    if df is None or len(df) == 0:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    lower = {str(c).lower(): c for c in df.columns}
    missing = [c for c in ("open", "high", "low", "close", "volume") if c not in lower]
    if missing:
        raise ValueError(f"聚合缺少列: {missing}（现有: {list(df.columns)}）")

    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    idx = idx.as_unit("ns")
    order = idx.argsort()
    if not bool((order == range(len(idx))).all()):  # 已排序时跳过重排
        idx = idx[order]

    def _col(name: str) -> "pd.Series":
        return df[lower[name]].astype(float).to_numpy()[order]

    work = pd.DataFrame(
        {
            "Open": _col("open"),
            "High": _col("high"),
            "Low": _col("low"),
            "Close": _col("close"),
            "Volume": _col("volume"),
        },
        index=idx,
    )

    # 组号 = UTC 整除；组内偏移 = 源槽号 % 因子（0..factor-1）
    ns = idx.asi8
    group = ns // (tgt_sec * _NANOS)
    offset = (ns // (src_sec * _NANOS)) % int(factor)
    work["_g"] = group
    work["_o"] = offset

    stats = work.groupby("_g").agg(
        n=("_o", "size"), omin=("_o", "min"), omax=("_o", "max")
    )

    # 完整性：只有**首尾两组**允许不完整（序列从半根开始 / 末尾未收盘），
    # 都属于边界截断，丢弃即可；其余任何不完整组都是**内部缺口** →
    # fail-closed（不静默补齐，否则 high/low 偏小、TD 计数会错）。
    complete = (
        (stats["n"] == int(factor))
        & (stats["omin"] == 0)
        & (stats["omax"] == int(factor) - 1)
    )
    first_g = int(stats.index.min())
    last_g = int(stats.index.max())
    head_partial = (stats.index == first_g) & ~complete & (stats["omin"] > 0)
    tail_partial = (stats.index == last_g) & ~complete & (stats["omax"] < int(factor) - 1)
    boundary_ok = head_partial.to_numpy() | tail_partial.to_numpy()
    interior = stats[~complete.to_numpy() & ~boundary_ok]
    if len(interior):
        g0 = int(interior.index[0])
        ts0 = pd.to_datetime(g0 * tgt_sec, unit="s", utc=True)
        raise ValueError(
            f"{period} 聚合缺口：{ts0.isoformat()} 这个 {tgt_sec}s 分组只有 "
            f"{int(interior.loc[g0, 'n'])} 根源 {source} bar（应 {factor} 根）"
            f"——数据缺口不静默补齐，请缩短区间或换周期"
        )

    picked = work[work["_g"].isin(stats.index[complete.to_numpy()])]

    out = picked.groupby("_g").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    out.index = pd.to_datetime(out.index * tgt_sec, unit="s", utc=True).tz_localize(
        None
    )
    out.index.name = df.index.name
    return out[OUTPUT_COLUMNS]
