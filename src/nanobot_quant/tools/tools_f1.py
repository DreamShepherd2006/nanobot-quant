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
所以在 HF Space 上会自动使用新浪（云端唯一可用的 A 股源；东财被 IP 封禁，
两者互为备胎、失败时自动回退）。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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
        return "sina"
    if s.upper().endswith("-USDT") or "/" in s:
        return "okx_cex"
    if s.replace(".", "").isalpha():
        return "eastmoney"
    raise ValueError(
        f"无法从 {s!r} 推断数据源，请显式传 source=（可选：{list_data_sources()}）"
    )


# A 股东西双源：新浪与东财互为备胎。云端（HF Space / 容器）东财会被
# IP 封禁直接断连，新浪实测可用且深度更好（5m 约 5 个月、日线 24 年）。
_CN_FALLBACK = {"sina": "eastmoney", "eastmoney": "sina"}


# 各数据源的**单次请求**上限。超过时必须改走时间区间分页，否则会被
# 服务端静默截断（实证：okx_cex 传 limit=6008 实际只回 288 根，
#  导致分析工具在加密上只有 3 天数据）。
# 注：目前只有 OKX 系真的会截（300/次）；gate_cex 单次 1000 且自帯分页。
_SINGLE_MAX = {"okx_cex": 300, "onchainos": 300}


def _fetch_deep(spec, src_name: str, sym: str, period: str, limit: int):
    """取数；超出单次上限时改走时间区间（数据源内部会自己分页）。

    ``okx_cex.fetch_kline`` 在同时给 ``start`` 与 ``end`` 时会转调
    ``fetch_kline_range``（已实现 after-游标分页）；不给则单次直拉、
    超过 300 根被服务端截断。所以这里只需把 ``start`` 算出来就行。
    """
    per_bar_max = _SINGLE_MAX.get(src_name)
    if per_bar_max is None or limit <= per_bar_max:
        return spec.fetch_kline(sym, bar=period, limit=limit), src_name

    secs = INTERVAL_SECONDS.get(period)
    if not secs:
        return spec.fetch_kline(sym, bar=period, limit=limit), src_name
    end = datetime.now(timezone.utc)
    # 多要 10% + 一段缓冲，避免边界上少一根
    start = end - timedelta(seconds=secs * (limit + limit // 10 + 10))
    try:
        return (
            spec.fetch_kline(sym, bar=period, limit=limit, start=start, end=end),
            src_name,
        )
    except TypeError as exc:  # 源不支持 start/end 关键字
        _log(f"{src_name} 不支持区间分页（{exc}），回退单次拉取 {sym} {period}")
        return spec.fetch_kline(sym, bar=period, limit=limit), src_name


def _fetch_with_fallback(spec, src_name: str, sym: str, period: str, limit: int):
    """取数；失败时换兄弟源重试一次，返回 ``(df, 实际源名)``。

    A 股两个源在不同网络环境下各有一边通——云端东财被 IP 封、自建环境
    新浪未必可达——所以互为备胎而非二选一。回退必须留日志（不静默降级），
    回退后仍失败则把原始异常抛给调用方。
    """
    try:
        return _fetch_deep(spec, src_name, sym, period, limit)
    except Exception as exc:  # noqa: BLE001 — 换源重试，最终仍失败则抛出
        alt_name = _CN_FALLBACK.get(src_name)
        if not alt_name:
            raise
        alt_spec = get_data_source(alt_name)
        if period not in (alt_spec.bars or ()):
            raise
        _log(f"{src_name} 取数失败（{type(exc).__name__}），回退 {alt_name}: {sym} {period}")
        return _fetch_deep(alt_spec, alt_name, sym, period, limit)


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
    """仅作描述，**不再作可用性判据**。

    曾用「CV≤27% 可用 / >35% 不可用」当门限，2026-09-20 实证推翻：
    CV 不是标的属性，而是「周期 × 样本窗口长度」的函数（同一标的跨周期
    CV 单调下降约 10 倍）；A股 30 组实验中反向 0 次、Spearman(CV, 中位幅度)
    = +0.595（与判据方向相反）。现在的口径：**CV 只作数据描述，
    可用性一律看比值 / p 值本身。**
    """
    return f"CV={cv:.3f}（仅描述序列离散度，**不可用作可用性判据**——该判据已于 2026-09-20 证伪）"


_BRK_THRESHOLD = -0.02  # 单根跌幅超过 −2% 记一次「插针」


def _all_drawdown_metrics(
    low: np.ndarray, close: np.ndarray, k: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """在全序列上预先算好三种窗口回撤指标（滑动窗口，长度 = len(low)）。

    对触发点 ``i``（即 ``[i]`` 位置）：

    * **seg**   = ``min(low[i+1 : i+k+1]) / close[i] − 1`` —— 整段最低
      （混了「慢慢磨」和「一根插针」两种机制）
    * **bar**   = ``min_j (low[j] / close[j-1] − 1)``，j ∈ [i+1, i+k] ——
      **窗口内最坏的那一根 bar**，这才是卖 put 怕的「插针」
    * **cnt**   = 窗口内 ``bar`` 跌幅 < −2% 的根数 —— 插针**频率**

    与随机对照比较时用比值：<1 = 触发后更浅、>1 = 更深。
    """
    n = len(low)
    nan = np.full(n, np.nan)

    seg_min = pd.Series(low).rolling(k, min_periods=k).min().shift(-k).values
    with np.errstate(invalid="ignore"):
        seg = seg_min / close - 1.0

    ret1 = nan.copy()
    ret1[1:] = low[1:] / close[:-1] - 1.0
    bar = pd.Series(ret1).rolling(k, min_periods=k).min().shift(-k).values

    hit = (np.nan_to_num(ret1, nan=0.0) < _BRK_THRESHOLD).astype(float)
    cnt = pd.Series(hit).rolling(k, min_periods=k).sum().shift(-k).values

    return seg, bar, cnt


def _dd_ratio(
    v_all: np.ndarray,
    u_all: np.ndarray,
    idxs: np.ndarray,
    rng: np.random.Generator,
    ntrials: int,
) -> Optional[dict]:
    """触发组中位 / 随机对照中位（ATR 单位比值）。对照样本量与触发组同。"""
    m = np.isfinite(v_all[idxs]) & np.isfinite(u_all[idxs]) & (u_all[idxs] != 0)
    t = idxs[m]
    if len(t) < 8:
        return None
    a = float(np.median(v_all[t] / u_all[t]))

    pool = np.flatnonzero(np.isfinite(v_all) & np.isfinite(u_all) & (u_all != 0))
    if len(pool) < 8:
        return None
    draws = [
        float(np.median(v_all[j] / u_all[j]))
        for j in (
            rng.choice(pool, size=min(len(t), len(pool)), replace=False)
            for _ in range(ntrials)
        )
    ]
    b = float(np.median(draws))
    if not np.isfinite(b) or b == 0:
        return None
    return {"n": int(len(t)), "ratio": a / b, "trigger": a, "control": b}


def _dd_count_ratio(
    v_all: np.ndarray,
    idxs: np.ndarray,
    rng: np.random.Generator,
    ntrials: int,
) -> Optional[dict]:
    """插针次数比值（均值口径，无需 ATR 归一）。"""
    m = np.isfinite(v_all[idxs])
    t = idxs[m]
    if len(t) < 8:
        return None
    a = float(v_all[t].mean())
    pool = np.flatnonzero(np.isfinite(v_all))
    draws = [
        float(v_all[rng.choice(pool, size=min(len(t), len(pool)), replace=False)].mean())
        for _ in range(ntrials)
    ]
    b = float(np.mean(draws))
    if b <= 0:
        return {"n": int(len(t)), "ratio": None, "trigger": a, "control": b}
    return {"n": int(len(t)), "ratio": a / b, "trigger": a, "control": b}


def _drawdown_block(
    low: np.ndarray,
    close: np.ndarray,
    counts: np.ndarray,
    k: int,
    threshold: int,
    atr_unit: np.ndarray,
    q: Optional[np.ndarray] = None,
    qmin: float = 0.0,
    qmax: float = 1.0,
    lo: int = 0,
    hi: Optional[int] = None,
    ntrials: int = 120,
    seed: int = 42,
    include_tail: bool = True,
) -> Optional[dict]:
    """单个 标的×周期×horizon×分位 回撤统计块（含整段/单根/插针）。"""
    n = len(close)
    hi = n if hi is None else hi
    idx = [
        i
        for i, v in enumerate(counts)
        if v >= threshold and lo <= i < hi and i + k < hi
        and (q is None or qmin <= q[i] < qmax)
    ]
    if len(idx) < 8:
        return None

    # 只看当前段内的样本，随机对照也从段内抽
    seg_all, bar_all, cnt_all = _all_drawdown_metrics(low, close, k)
    seg_all = seg_all[:hi]
    bar_all = bar_all[:hi]
    cnt_all = cnt_all[:hi] if include_tail else cnt_all
    u = atr_unit[:hi]

    idxs = np.array(idx, dtype=int)
    rng = np.random.default_rng(seed)
    out: dict = {
        "n": int(len(idxs)),
        "segment_min": _dd_ratio(seg_all, u, idxs, rng, ntrials),
        "bar_worst": _dd_ratio(bar_all, u, idxs, rng, ntrials),
    }
    if include_tail:
        out["breach_count"] = _dd_count_ratio(cnt_all, idxs, rng, ntrials)
    return out


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
                df, used_src = _fetch_with_fallback(spec, src_name, sym, period, limit)
                rec["source"] = used_src
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
        f"其中 {len(usable)} 个 F1 的 TD 显著（first_cross 口径，p<0.05）；"
        + (f"价格 TD 对照组不显著 {len(px_bad)}/{len(ok)}。" if include_price_td else "")
        + " 本工具量的是**衰竭方向**（会不会收回来），不量回撤深度——"
        "要看回撤/尾部用 analyze_f1_drawdown。"
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
            "p 来自 200 次随机位置对照。价格 TD 为同数据基线（已实证六类资产均失效）。"
            "**两种触发口径都给出**：主字段（f1_buy9/f1_sell9/price_*）只取"
            "「计数首次穿越阈值」的那一根（样本不重叠、更严格）；"
            "`*_all` 后缀字段把「计数 ≥ 阈值」的每一根都算一次触发"
            "（含 10/11/12… 累加期，n 常到 15–40，代价是窗口重叠）。"
            "**显著性一律以主字段（first_cross）为准，`*_all` 不可用于判显著**——"
            "2026-09-20 实证：A股 日线用 `*_all` 得出「10/10 显著」，改用"
            "first_cross 后独立样本只剩 0–3 个。\n"
            "**CV 字段仅作数据描述**（序列离散度），**不可用作可用性判据**——"
            "该判据已于 2026-09-20 证伪：CV 是「周期 × 窗口长度」的函数而非标的"
            "属性，同一标的跨周期 CV 单调下降约 10 倍，且与中位幅度正相关（+0.595），"
            "与「CV 低=可用」的预期方向相反。\n"
            "**本工具只回答衰竭方向；回撤深度与尾部风险请用 analyze_f1_drawdown。**"
        ),
    }


def analyze_f1_drawdown(
    symbols: list[str],
    periods: Optional[list[str]] = None,
    source: str = "",
    atr_n: int = 20,
    lookback_hours: float = 3.0,
    threshold: int = 9,
    ks: Optional[list[int]] = None,
    qmin: float = 0.0,
    qmax: float = 1.0,
    include_tail: bool = True,
    include_price_td: bool = True,
    split: float = 0.0,
    limit: int = 6008,
    ntrials: int = 120,
) -> dict:
    """F1 上的 TD 触发后，**回撤有多深**（ATR 单位比值口径）。

    与 ``analyze_f1_td`` 的分工：

    * ``analyze_f1_td`` 量的是「**衰竭方向**」——触发后 k 根，序列
      终值比起点涨/跌了多少（median / hit / p）。回答「会不会收回来」。
    * ``analyze_f1_drawdown`` 量的是「**回撤深度**」——触发后 k 根内，
      最低点比触发价低多少（ATR 单位归一）。回答「中间跌得深不深」，
      以及**尾部会不会被打穿**。

    这两个是**不同的物理量**：一个信号可以既「最终收回来」（hit 高）
    又「中间跌很深」（尾部大）。对卖 put 而言，后者才是风险。

    三项指标（全部归一化到 ATR 单位）：

    * **整段**：``min(low[i+1 : i+k+1]) / close[i] − 1`` —— 传统回撤口径
      （混了「慢慢磨」和「一根插针」两种机制）
    * **单根**：``min_j (low[j] / close[j-1] − 1)`` —— 窗口内**最坏的那一根**
      bar，即「插针」。实证上它比整段更干净（样本外 12/12 vs 11/12）。
    * **插针次数**：单根跌幅 < −2% 的根数（频率口径）。

    每个指标输出**触发组 / 随机对照**的比值：**<1 = 触发后更浅（好），
    >1 = 更深（危险）**。这就是判据，不需另看 p 值。

    Args:
        symbols: 标的列表（同 ``analyze_f1_td``）。
        periods: 周期列表，默认 ``["15m"]``（实证显示 F1-TD 在 15m/1H 上
            最稳；A股 仅 15m 宽基有微弱迹象）。
        source: 显式数据源，留空按标的自动判断。
        atr_n: ATR 周期（默认 20）。
        lookback_hours: F1 语义窗口（默认 3h），按周期换算（1m→180、
            15m→12、1H→3）。
        threshold: setup 触发阈值（默认 9）。
        ks: horizon 列表（根），默认 ``[12, 24]``。回撤深度随 k 单调加深，
            给两档能看出**风险的积累速度**。
        qmin / qmax: F1 分位过滤区间（默认 0–1 = 不过滤）。
            **实证关键**：buy9 的全部信息集中在 **Q3（0.6–0.8）**，
            不过滤时效应被 Q0/Q4 稀释。**不要用「越低越好」的思路** ——
            Q0（F1 最低）无信息且绝对回撤更深（分布是**倒 U**）。
        include_tail: 是否算插针次数（默认 True）。
        include_price_td: 是否加「价格 TD」对照组（默认 True）——
            价格上的 TD 在六类资产上均已失效，作反面对照。
        split: >0 时做**时间样本外切分**（如 0.6 = 前 60% 训练 /
            后 40% 留出），两段分别输出。默认 0 = 只看全样本。
        limit: 每标的每周期最多拉取 K 线数（默认 6008，对齐各源上限）。
        ntrials: 随机对照重复次数（默认 120）。

    Returns:
        dict，含 ``results``（逐 标的×周期×horizon 明细，每条内含
        ``segments`` 数组：全 / 训练 / 留出）、``data_source``、``summary``、
        ``note``。每段内按 ``buy9`` / ``sell9`` 给三项比值。
    """
    if periods is None:
        periods = ["15m"]
    if ks is None:
        ks = [12, 24]
    src_name = _resolve_source(symbols, source)
    spec = get_data_source(src_name)
    supported = set(spec.bars) if spec.bars else set(INTERVAL_SECONDS)

    results: list[dict] = []
    for period in periods:
        if period not in supported:
            results.append(
                {"symbol": symbols[0] if symbols else "", "period": period,
                 "status": "error",
                 "error": f"{src_name} 不支持周期 {period}（支持 {sorted(supported)}）"}
            )
            continue
        secs = INTERVAL_SECONDS.get(period)
        if not secs:
            results.append(
                {"symbol": symbols[0] if symbols else "", "period": period,
                 "status": "error", "error": f"未知周期 {period}"}
            )
            continue
        lb = max(1, int(round(lookback_hours * 3600 / secs)))

        for sym in symbols:
            rec: dict = {"symbol": sym, "period": period, "lookback_bars": lb,
                         "quantile": [qmin, qmax], "ks": list(ks)}
            try:
                df, used_src = _fetch_with_fallback(spec, src_name, sym, period, limit)
                rec["source"] = used_src
            except Exception as exc:  # noqa: BLE001
                rec.update(status="error", error=f"取数失败: {type(exc).__name__}: {exc}")
                results.append(rec)
                _log(f"{sym} {period}: 取数失败 {exc}")
                continue
            if df is None or len(df) < lb + 80:
                rec.update(status="error",
                           error=f"数据不足（{0 if df is None else len(df)} 根）")
                results.append(rec)
                continue

            df = df.rename(columns=lambda c: {
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            }.get(c, c))
            if not {"High", "Low", "Close"}.issubset(df.columns):
                rec.update(status="error", error=f"列不完整: {list(df.columns)}")
                results.append(rec)
                continue

            f1 = _f1_series(df, atr_n, lb)
            if len(f1) < 80:
                rec.update(status="error", error=f"F1 有效点不足（{len(f1)}）")
                results.append(rec)
                continue

            # 对齐：F1 是 dropna 后的子集，所有数组都按它取
            al = df.loc[f1.index]
            low = np.asarray(al["Low"].values, dtype=float)
            close = np.asarray(al["Close"].values, dtype=float)
            atr_unit = np.asarray(_atr(al, atr_n).values, dtype=float)
            n = len(close)
            q = f1.rank(pct=True).values if (qmin > 0 or qmax < 1) else None

            fb, fs = _td_counts(f1)
            pb = ps = None
            if include_price_td:
                pb, ps = _td_counts(al["Close"])

            days = (al.index[-1] - al.index[0]).total_seconds() / 86400.0
            rec.update(status="ok", bars=int(n), days=round(float(days), 1),
                       cv=round(float(f1.std() / f1.mean()), 4))

            horizons: list[dict] = []
            for k in ks:
                if n <= k + 10:
                    continue
                blocks: list[dict] = []
                if split and split > 0:
                    cut = int(n * split)
                    segs = [("训练", 0, cut), ("留出", cut, n)]
                else:
                    segs = [("全", 0, n)]
                for seg_name, lo_i, hi_i in segs:
                    seg_rec: dict = {"segment": seg_name, "bars": hi_i - lo_i}
                    for side_name, counts, sign in (("buy9", fb, +1), ("sell9", fs, -1)):
                        blk = _drawdown_block(
                            low, close, counts, k, threshold, atr_unit,
                            q=q, qmin=qmin, qmax=qmax, lo=lo_i, hi=hi_i,
                            ntrials=ntrials,
                            seed=abs(hash((sym, period, side_name, k, seg_name))) % 2**31,
                            include_tail=include_tail,
                        )
                        if blk is not None:
                            blk["side"] = side_name
                        seg_rec[side_name] = blk
                    if include_price_td and pb is not None and ps is not None:
                        for side_name, counts in (("price_buy9", pb), ("price_sell9", ps)):
                            blk = _drawdown_block(
                                low, close, counts, k, threshold, atr_unit,
                                q=None, lo=lo_i, hi=hi_i, ntrials=ntrials,
                                seed=abs(hash((sym, period, side_name, k, seg_name))) % 2**31,
                                include_tail=include_tail,
                            )
                            if blk is not None:
                                seg_rec[side_name] = blk
                    blocks.append(seg_rec)
                horizons.append({"k": k, "segments": blocks})
            rec["horizons"] = horizons
            results.append(rec)

    ok = [r for r in results if r.get("status") == "ok"]
    # 只用每标的第一个 horizon 的全样本段做大一统判据
    def _ref(rec: dict, side: str) -> Optional[float]:
        for h in rec.get("horizons", []):
            for s in h.get("segments", []):
                if s.get("segment") != "全":
                    continue
                blk = s.get(side) or {}
                b = (blk.get("segment_min") or {}).get("ratio")
                if b is not None:
                    return b
        return None

    buy_r = [x for x in (_ref(r, "buy9") for r in ok) if x is not None]
    sell_r = [x for x in (_ref(r, "sell9") for r in ok) if x is not None]
    summary = (
        f"数据源={src_name}；{len(ok)}/{len(results)} 个 标的×周期 成功"
        + (f"（分位过滤 [{qmin}, {qmax})）" if q is not None else "（全样本）")
        + "。"
        + (f"buy9 整段回撤比 中位 {np.median(buy_r):.3f}（n={len(buy_r)}）" if buy_r else "")
        + "；"
        + (f"sell9 整段回撤比 中位 {np.median(sell_r):.3f}（n={len(sell_r)}）" if sell_r else "")
        + "。判据：<1 = 触发后回撤更浅，>1 = 更深。"
    )
    return {
        "results": results,
        "data_source": src_name,
        "periods": list(periods),
        "summary": summary,
        "note": (
            "回撤口径：整段 = min(low[i+1:i+k+1])/close[i]−1（传统口径）；"
            "单根 = min_j(low[j]/close[j−1]−1)（最坏的那一根 bar，= 插针）；"
            "插针次数 = 单根跌幅 < −2% 的根数。整段/单根按 ATR_n 归一，"
            "插针按均值比。所有指标均对比同段内随机位置的对照，输出**比值**："
            "<1 = 触发后更浅（好），>1 = 更深（危险）。\n"
            "**已在 2026-09-20 实证的两条关键结论**（详见 docs/quant-system.md "
            "§33.33 / §33.34）：\n"
            "① F1 buy9 在加密 15m/1H 上使回撤幅度系统变浅（15m 0.828 / 1H 0.712，"
            "6 标的 36/36 一致），**加 F1 分位 Q3（0.6–0.8）过滤后压到 0.612**；"
            "sell9 是严格镜像（Q3 时 1.461）。\n"
            "② 但**这只影响幅度、不影响插针频率**——buy9 后单根跌幅变浅（0.657，"
            "样本外 12/12），插针次数却在 1.0–1.2 之间（不降）。"
            "**不要把它当风险规避工具**：−5% 档被打穿的概率并没有下降。\n"
            "③ **分布是倒 U，不是单调**——buy9 在 Q3 最安全（0.62），"
            "两端的 Q0/Q4 ≈ 1；Q0（F1 最低）无信息且绝对回撤更深。"
            "**切勿用「F1 越低越安全」。**\n"
            "④ A股 结论完全不同：日线 first_cross 样本仅 0–3（无法分析）；"
            "30m 无方向（1.030）、5m 反向（1.140）；**仅 15m 宽基三只"
            "（510050/510300/510500）有微弱迹象（0.58–0.77），且样本仅 ~1 年**。\n"
            "⑤ 同数据上的「价格 TD」对照组已实证失效（六类资产全部不显著），"
            "仅作反面基线。\n"
            "只读分析工具：不下单、不改任何配置。"
        ),
    }


# ───────────────────────────────────────────────────────────────
# 异步契约（WebUI「📊 F1 模式回测」分栏 / 长任务）
# ───────────────────────────────────────────────────────────────
# 设计（§33.36 S1）：F1 分析在 WebUI 上可能是多标的 × 多周期，单次
# 超过 MCP stdio 的 30s 硬超时 —— 与现货/期权回测同款 run_id + 轮询
# 契约。结果落 ``{data_root}/legion/backtests/<run_id>.json``，前缀
# ``f1-`` 与现货（``YYYYMMDD-...``）/ 期权（``opt-``）区分，页面历史
# 分开列（见 ``_f1_runs``）。
#
# 与回测的关键差异：F1 分析**不 import lumibot**，所以不需要
# ``_run_guarded`` 那套 stdio 守护 —— 但保留 running/done/error 三段
# 状态机，否则run 进行中的几分钟里页面会看起来「什么都没发生」。
#
# 覆盖参数只作用于本次运行，**绝不回写任何实盘配置**（同 2026-08-30
# 拍板口径；F1 分析本身也不写任何参数文件）。

F1_RUN_PREFIX = "f1-"
_F1_KINDS = ("f1_td", "f1_drawdown")


def _f1_write(run_id: str, payload: dict) -> None:
    """持久化到 ``{data_root}/legion/backtests/<run_id>.json``。

    与回测共用目录（页面历史统一），靠 run_id 前缀区分来源。
    写失败只记 stderr —— 落盘失败不得影响分析本身。
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        out_dir = backtests_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"_f1_write failed for {run_id}: {exc}")


def _f1_guarded(run_id: str, kind: str, kwargs: dict) -> None:
    """后台线程：跑分析 → 落盘 done/error。

    先落一条 ``status=running``，否则长分析期间的页面历史空白（与
    ``_run_guarded`` 同一理由）。
    """
    _f1_write(run_id, {"status": "running", "run_id": run_id, "kind": kind})
    try:
        fn = analyze_f1_td if kind == "f1_td" else analyze_f1_drawdown
        result = fn(**kwargs)
        if isinstance(result, dict):
            result["kind"] = kind
        _f1_write(run_id, {"status": "done", "run_id": run_id, "result": result})
        _log(f"{run_id} done kind={kind}")
    except Exception as exc:  # noqa: BLE001 — 单次失败不得杀死线程外的任何东西
        _f1_write(run_id, {"status": "error", "run_id": run_id, "error": str(exc)})
        _log(f"{run_id} error kind={kind}: {exc}")


def run_f1_analysis(
    kind: str = "f1_td",
    symbols: Optional[list[str]] = None,
    periods: Optional[list[str]] = None,
    source: str = "",
    **kwargs,
) -> dict:
    """起一轮 F1 分析（后台线程），返回 ``{status, run_id}``。

    Args:
        kind: ``"f1_td"``（触发统计）或 ``"f1_drawdown"``（回撤诊断）。
        symbols: 标的列表，如 ``["601127"]`` / ``["SOL", "ETH"]``。
        periods: 周期列表，如 ``["1D"]`` / ``["15m", "1H"]``。
                 可用性由数据源决定（新浪无 1m；东财云端不可达）。
        source: 数据源名（``gate_cex`` / ``okx_cex`` / ``sina`` /
                ``eastmoney``）；留空则按标的形式自动推断。
        **kwargs: 透传给对应分析函数（``k`` / ``atr_n`` / ``threshold`` /
                  ``ks`` / ``qmin`` / ``qmax`` / ``split`` / ``limit`` /
                  ``include_price_td`` / ``include_tail`` …）。

    Returns:
        ``{"status": "started", "run_id": "f1-..."}``；参数不合法时
        返回 ``{"error": ...}``。用 ``get_f1_result(run_id)`` 轮询。
    """
    kind = str(kind or "f1_td").strip().lower()
    if kind not in _F1_KINDS:
        return {
            "error": f"未知 kind={kind!r}",
            "hint": f"可用：{' / '.join(_F1_KINDS)}",
        }
    syms = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not syms:
        return {"error": "至少需要一个标的", "hint": "如 symbols=['601127']"}
    pers = [str(p).strip() for p in (periods or []) if str(p).strip()] or ["1D"]

    params = {
        "symbols": syms,
        "periods": pers,
        "source": source or "",
        **{k: v for k, v in kwargs.items() if v is not None},
    }

    import threading
    from uuid import uuid4

    run_id = (
        f"{F1_RUN_PREFIX}"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    )
    _log(
        f"start {run_id} kind={kind} symbols={syms} periods={pers} "
        f"source={source or 'auto'} extra={sorted(k for k in params if k not in ('symbols','periods','source'))}"
    )
    threading.Thread(
        target=_f1_guarded, args=(run_id, kind, params), daemon=True
    ).start()
    return {"status": "started", "run_id": run_id, "kind": kind}


def get_f1_result(run_id: str) -> dict:
    """读 F1 分析结果，并附上 ``markdown``（页面复制按钮 / agent 直读）。

    与 ``tools_backtest.get_backtest_result`` 同形：后台 run 写的是
    ``{status, run_id, result}`` 包装，markdown 挂到**内层 result**；
    裸 result（旧记录）挂顶层。running/error 不加 markdown。
    """
    if not run_id:
        return {"error": "缺少 run_id"}
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        p = backtests_dir() / f"{run_id}.json"
        if not p.is_file():
            return {
                "error": f"no f1 result for run_id={run_id}",
                "hint": "分析可能仍在运行，或 run_id 有误。",
            }
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("run_id", run_id)
            inner = payload.get("result")
            target = inner if isinstance(inner, dict) else payload
            target.setdefault("run_id", run_id)
            try:
                from nanobot_quant.f1_markdown import render_markdown

                md = render_markdown(target)
                if md:
                    target["markdown"] = md
            except Exception:  # noqa: BLE001 — markdown 只是 UX 增强
                pass
        return payload
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read f1 result for {run_id}: {exc}"}
