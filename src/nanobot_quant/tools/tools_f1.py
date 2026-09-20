"""``analyze_f1_td`` — 把波动率（F1）序列喂给 TD 的衰竭检验工具。

研究背景（2026-09-19 / 09-20 实证，docs/quant-system.md §33 待落档）
--------------------------------------------------------------------
TD Sequential 隐含依赖波动率、却从不显式处理它：setup 用绝对价格比较
（``close < close[i-4]``）、countdown 用相对极值（``close <= Low[i-2]``）、
而 9/13 这些常数把「典型波动率」假设固化了下来 —— 这正是它在日线
（DeMark 原始场景）鲁棒、在分钟级失效的原因。

把 F1 = ``ATR_n[t] / ATR_n[t-lookback]``（波动率扩张率，lookback 按
「3 小时语义」随周期换算）作为 TD 的**输入序列**后，实测（跨市场）：

* 加密蓝筹 1H（BTC/ETH/SOL/BNB/XRP/DOGE，300 天）：buy9 / sell9 均显著
* A股 日线（10 标的 × 20 年）：sell9 10/10 显著，buy9 8/10 显著
* A股 5m / 15m（6 标的 × 60 天）：buy9 显著（+16%~+32%）
* 美股 1H（10 标的 × 1064 天）：sell9 显著（但样本偏少）
* 对照组「价格 TD」在**六类资产**上均无入场优势（幅度 ±0.5% 内、命中≈50%）

**适用范围由 CV（F1 的变异系数）决定**：
``CV ≲ 27%`` → 衰竭语义成立；``CV ≳ 35%`` → TD 在 F1 上退化为趋势指标
（9 之后继续原方向）。这条线在加密 / A股 / 美股上都成立。

工具职责
--------
给一组标的 × 周期，拉 K 线 → 算 F1 → 在 F1 上跑 TD → 统计 setup 达到
阈值（默认 9）之后的衰竭表现（中位幅度 / 命中率 / 随机对照 p 值），
并附上同一数据上「价格 TD」的对照，最后按 CV 给出可用性建议。

数据源走 ``data_sources`` 注册表（``get_data_source(...).fetch_kline``），
所以在 HF Space 上会自动使用东财（那边连通、容器内不连通）。
"""

from __future__ import annotations

import sys
from typing import Optional

import numpy as np
import pandas as pd

from nanobot_quant.data_sources import get_data_source, list_data_sources
from nanobot_quant.data_sources.periods import INTERVAL_SECONDS
from nanobot_quant.strategies.td_sequential import (
    DEFAULT_TD_PARAMS,
    calculate_series,
)


def _log(msg: str) -> None:
    """stderr only — stdout is the MCP stdio JSON-RPC channel."""
    print(f"[F1-TD] {msg}", file=sys.stderr, flush=True)


def _resolve_source(symbols: list[str], source: str) -> str:
    """Infer the data source from the symbol shape (explicit ``source`` wins).

    6-digit numeric (600519/588000) or a bare letter ticker (AAPL) → 东财;
    ``*-USDT`` crypto pairs → OKX CEX（与期权线同源）.
    """
    if source:
        return source
    if not symbols:
        raise ValueError("symbols 为空")
    s = symbols[0]
    if s.isdigit() and len(s) == 6:
        return "eastmoney"
    if s.upper().endswith("-USDT") or "/" in s:
        return "okx_cex"
    if s.replace(".", "").isalpha():
        return "eastmoney"
    raise ValueError(
        f"无法从 {s!r} 推断数据源，请显式传 source=（可选：{list_data_sources()}）"
    )


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat(
        [(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _f1_series(df: pd.DataFrame, atr_n: int, lookback_bars: int) -> pd.Series:
    a = _atr(df, atr_n)
    # A zero ATR (flat/ halted bars) makes the ratio infinite — drop it.
    # Real feeds occasionally emit identical high/low/close runs (one-word
    # limit boards on A-shares, halted bars), and inf would poison the
    # mean/std downstream.
    a = a.where(a > 0)
    ratio = a / a.shift(lookback_bars)
    return ratio.replace([np.inf, -np.inf], np.nan).dropna()


def _td_counts(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Run TD on an arbitrary 1-D series; return (buy_setup, sell_setup) arrays."""
    fd = pd.DataFrame(
        {
            "Open": series.values,
            "High": series.values,
            "Low": series.values,
            "Close": series.values,
            "Volume": 0.0,
        },
        index=series.index,
    )
    out = calculate_series(fd, params=dict(DEFAULT_TD_PARAMS))
    buy = pd.to_numeric(out["buy_setup_count"], errors="coerce").fillna(0)
    sell = pd.to_numeric(out["sell_setup_count"], errors="coerce").fillna(0)
    return buy.astype(int).values, sell.astype(int).values


def _trigger_stats(
    vals: np.ndarray,
    counts: np.ndarray,
    sign: int,
    k: int,
    threshold: int,
    ntrials: int = 200,
    seed: int = 42,
    mode: str = "first_cross",
) -> Optional[dict]:
    """Stats for ``threshold``-crossing triggers: median move / hit rate / p.

    ``sign=+1`` tests the "衰竭" direction for buy setups (series should
    rise afterwards), ``sign=-1`` for sell setups.  The p-value comes from a
    200-sample random-position null so a bare median is never reported
    without its baseline.

    ``mode`` picks which bars count as triggers:

    * ``"first_cross"`` — only the bar where the count first reaches
      ``threshold``.  Non-overlapping, the stricter reading of "a TD 9".
    * ``"all_bars"`` — every bar with ``count >= threshold``, i.e. the whole
      exhaustion run including 10/11/12… .  Larger n but overlapping windows.
    """
    n = len(vals)
    if n <= k:
        return None
    idx: list[int] = []
    if mode == "all_bars":
        idx = [i for i, v in enumerate(counts) if v >= threshold and i + k < n]
    else:
        prev = 0
        for i, v in enumerate(counts):
            if v >= threshold and prev < threshold and i + k < n:
                idx.append(i)
            prev = v
    if not idx:
        return None

    def _mv(i: int) -> float:
        return (vals[i + k] - vals[i]) / abs(vals[i]) * sign

    moves = np.array([_mv(i) for i in idx])
    rng = np.random.default_rng(seed)
    null = np.array(
        [np.median([_mv(int(j)) for j in rng.integers(0, n - k, len(idx))])
         for _ in range(ntrials)]
    )
    return {
        "n": int(len(idx)),
        "median": float(np.median(moves)),
        "hit": float(np.mean(moves > 0)),
        "p": float(np.mean(null >= np.median(moves))),
    }


def _cv_hint(cv: float) -> str:
    if cv <= 0.27:
        return "CV 低 → 衰竭语义成立，信号可用"
    if cv <= 0.35:
        return "CV 居中（27-35%）→ 临界区，信号可能不稳定"
    return "CV 高（>35%）→ TD 在 F1 上退化为趋势指标，9 之后倾向继续原方向"


def analyze_f1_td(
    symbols: list[str],
    periods: Optional[list[str]] = None,
    source: str = "",
    atr_n: int = 20,
    lookback_hours: float = 3.0,
    threshold: int = 9,
    k: int = 12,
    include_price_td: bool = True,
    limit: int = 2000,
) -> dict:
    """把 F1（波动率扩张率）序列喂给 TD，检验 setup 触发后的衰竭表现。

    对每个 标的 × 周期：拉 K 线 → 算 F1=ATR_n/ATR_n[lb] → 在 F1 上跑 TD →
    统计达到 ``threshold``（默认 9）之后 ``k`` 根的：中位幅度 / 方向命中率 /
    随机对照 p 值；同时输出该周期的 CV（判断适用性的核心指标）与同一数据上
    「价格 TD」的对照组。

    Args:
        symbols: 标的列表。6 位数字（A股/ETF，如 588000、600519）或字母
            代码（美股，如 AAPL）→ 东财；``*-USDT``（如 BTC-USDT）→ OKX CEX。
        periods: 统一周期名（1m/5m/15m/30m/1H/4H/1D/1W…），默认 ["1H"]。
        source: 显式指定数据源（eastmoney/yfinance/okx_cex/gate_cex/onchainos），
            留空按标的自动判断。
        atr_n: ATR 周期（默认 20 根）。
        lookback_hours: F1 的语义窗口（默认 3 小时），按周期换算成根数
            （1m→180、15m→12、1H→3），周期越长于 3h 时取 1。
        threshold: setup 触发阈值（默认 9，DeMark 标准）。
        k: 衰竭检验的 horizon（默认 12 根）。
        include_price_td: 是否输出同一数据上价格 TD 的对照组（默认 True）。
        limit: 每标的每周期最多拉取的 K 线数（默认 2000）。

    Returns:
        dict，含 ``results``（逐标的逐周期明细）、``data_source``、
        ``summary``（人类可读的要点）与 ``note``。
    """
    if periods is None:
        periods = ["1H"]
    src_name = _resolve_source(symbols, source)
    spec = get_data_source(src_name)
    supported = set(spec.bars) if spec.bars else set(INTERVAL_SECONDS)

    results: list[dict] = []
    for period in periods:
        if period not in supported:
            results.append(
                {"symbol": symbols[0], "period": period, "status": "error",
                 "error": f"{src_name} 不支持周期 {period}（支持 {sorted(supported)}）"}
            )
            continue
        secs = INTERVAL_SECONDS.get(period)
        if not secs:
            results.append(
                {"symbol": symbols[0], "period": period, "status": "error",
                 "error": f"未知周期 {period}"}
            )
            continue
        lb = max(1, int(round(lookback_hours * 3600 / secs)))

        for sym in symbols:
            rec: dict = {"symbol": sym, "period": period, "lookback_bars": lb}
            try:
                df = spec.fetch_kline(sym, bar=period, limit=limit)
            except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the batch
                rec.update(status="error", error=f"取数失败: {type(exc).__name__}: {exc}")
                results.append(rec)
                _log(f"{sym} {period}: 取数失败 {exc}")
                continue
            if df is None or len(df) < lb + 60:
                rec.update(status="error", error=f"数据不足（{0 if df is None else len(df)} 根）")
                results.append(rec)
                continue

            df = df.rename(columns=lambda c: {
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            }.get(c, c))
            f1 = _f1_series(df, atr_n, lb)
            if len(f1) < 60:
                rec.update(status="error", error=f"F1 有效点不足（{len(f1)}）")
                results.append(rec)
                continue

            days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
            cv = float(f1.std() / f1.mean())
            f1v = f1.values

            fb, fs = _td_counts(f1)
            rec.update(
                status="ok",
                bars=int(len(df)),
                days=round(float(days), 1),
                cv=round(cv, 4),
                # 主字段 = 首次穿越（更严格）；*_all = 累加期全计（样本更多）
                f1_buy9=_trigger_stats(f1v, fb, +1, k, threshold),
                f1_sell9=_trigger_stats(f1v, fs, -1, k, threshold),
                f1_buy9_all=_trigger_stats(f1v, fb, +1, k, threshold, mode="all_bars"),
                f1_sell9_all=_trigger_stats(f1v, fs, -1, k, threshold, mode="all_bars"),
                cv_hint=_cv_hint(cv),
            )
            if include_price_td:
                px = df["Close"].values
                pb, ps = _td_counts(df["Close"])
                rec["price_buy9"] = _trigger_stats(px, pb, +1, k, threshold)
                rec["price_sell9"] = _trigger_stats(px, ps, -1, k, threshold)
                rec["price_buy9_all"] = _trigger_stats(px, pb, +1, k, threshold, mode="all_bars")
                rec["price_sell9_all"] = _trigger_stats(px, ps, -1, k, threshold, mode="all_bars")
            results.append(rec)

    ok = [r for r in results if r.get("status") == "ok"]
    usable = [r for r in ok if (r.get("f1_buy9") or {}).get("p", 1) < 0.05
              or (r.get("f1_sell9") or {}).get("p", 1) < 0.05]
    px_bad = [r for r in ok
              if (r.get("price_buy9") or {}).get("p") is not None
              and (r["price_buy9"] or {}).get("p", 1) >= 0.05] if include_price_td else []

    summary = (
        f"数据源={src_name}；{len(ok)}/{len(results)} 个 标的×周期 成功；"
        f"其中 {len(usable)} 个 F1 的 TD 显著（p<0.05）；"
        + (f"价格 TD 对照组不显著 {len(px_bad)}/{len(ok)}。" if include_price_td else "")
        + " 判读要点：CV≤27% 可用，CV>35% 时 TD 退化为趋势指标。"
    )
    return {
        "results": results,
        "data_source": src_name,
        "periods": list(periods),
        "summary": summary,
        "note": (
            "F1 = ATR_n[t]/ATR_n[t-lookback]，lookback 按 3 小时语义换算；"
            "TD 跑在 F1 序列上（非价格）。median/hit/p 对应阈值触发后 k 根的变化，"
            "sign 已按衰竭方向取正（buy9 期望回升、sell9 期望回落）。"
            "p 来自 200 次随机位置对照。价格 TD 为同数据基线。"
            "**两种触发口径都给出**：主字段（f1_buy9/f1_sell9/price_*）只取"
            "「计数首次穿越阈值」的那一根（样本不重叠、更严格，但 n 常常只有 2–3）；"
            "`*_all` 后缀字段把「计数 ≥ 阈值」的每一根都算一次触发"
            "（含 10/11/12… 累加期，n 常到 15–40，代价是窗口重叠）。"
            "两者样本量差异很大时以 *_all 为参考、以主字段为准绳，并一起看。"
        ),
    }
