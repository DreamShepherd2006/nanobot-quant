"""IV 分位（滚动序列 → 分位）—— 回测与将来的实盘共用同一份纯函数。

**为什么需要它**：``okx_options_strategy.evaluate_entry()`` 的 IV 环境闸门吃
``iv_percentile`` 参数，但在此之前**没有任何代码计算它** —— 阈值填任何值都形同
虚设。实证（2026-09-29 参数网格）：IV 闸门 0 与 70 的 8 组回测结果逐对完全相同，
因为 driver 从未把分位喂进决策函数。这里补上这一段（§33.43 Step 6 补齐）。

三条口径写死在这里、调用方不得各写一份：

* **参考 IV** = 「目标到期档（默认 7 天）的平值 put IV」，用曲面微笑在 ``K = 现货``
  处插值。固定同一个到期档才可比：近月 IV 在恐慌时高企、期限结构会翻转，混着取
  会把「期限结构变化」误读成「IV 分位变化」。目标档缺失时退到最近的 ≥ 最短期限档
  （``min_days``，默认 1 天，排除 <1 天的 theta 噪声），并如实记账。
* **滚动窗口** = 最近 ``window`` 个样本（按 bar 数，由天数与 bar 秒数换算），
  **只用截至当前时刻的历史** —— 无未来函数。
* **分位定义** = ``100 × #{窗口内 v ≤ 当前值} / n``（含当前值，单调；窗口内空值
  不参与）。样本 < ``min_samples`` → ``None``：由调用方 fail-open 放行并计数，
  「算不出分位」不得静默变成「分位 = 0 分」（那会把闸门变成常关）。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

DAY_MS = 86_400_000.0

#: 默认窗口（天）——一周的 trailing 样本，与实盘「同到期档 ATM put IV 分位」的口径对齐
DEFAULT_WINDOW_DAYS = 7.0
#: 参考到期档：目标 7 天（市场实际只有日到期与周线两族，7 天落在周线上、跨时间稳定）
DEFAULT_TARGET_DTE_DAYS = 7.0
#: 参考档最短剩余期限（天）：<1 天的 theta 噪声太大，不进参考序列
DEFAULT_MIN_DTE_DAYS = 1.0
#: 参考档最长剩余期限（天）：远超目标的档对「当下恐慌」不敏感
DEFAULT_MAX_DTE_DAYS = 14.0
#: 最小样本下限（根）：低于此值一律视为「样本不足」
MIN_SAMPLES_FLOOR = 20


def pick_ref_expiry(expiries_ms: Sequence[int], t_ms: int, *,
                    target_days: float = DEFAULT_TARGET_DTE_DAYS,
                    min_days: float = DEFAULT_MIN_DTE_DAYS,
                    max_days: float = DEFAULT_MAX_DTE_DAYS) -> Optional[int]:
    """在售到期档里挑「参考档」：优先目标带宽内离目标最近者，否则退到最近的 ≥最短档。

    返回 ``None`` = 该时刻没有任何可用到期档（调用方按「无参考 IV」处理）。
    """
    cands = [int(e) for e in expiries_ms if int(e) > int(t_ms)]
    if not cands:
        return None

    def dte(e: int) -> float:
        return (e - int(t_ms)) / DAY_MS

    band = [e for e in cands if min_days <= dte(e) <= max_days]
    pool = band or [e for e in cands if dte(e) >= min_days]
    if not pool:
        return None
    return min(pool, key=lambda e: abs(dte(e) - target_days))


def ref_atm_iv(surface: Any, expiries_ms: Sequence[int], t_ms: int, spot: float, *,
               target_days: float = DEFAULT_TARGET_DTE_DAYS,
               min_days: float = DEFAULT_MIN_DTE_DAYS,
               max_days: float = DEFAULT_MAX_DTE_DAYS) -> Optional[float]:
    """参考 IV = 参考档微笑在 ``K = 现货`` 处的平值 IV；任何一环缺失返回 ``None``。"""
    if surface is None or not spot or float(spot) <= 0:
        return None
    exp = pick_ref_expiry(expiries_ms, t_ms, target_days=target_days,
                          min_days=min_days, max_days=max_days)
    if exp is None:
        return None
    try:
        smile = surface.smile_at(int(t_ms), int(exp), forward=float(spot))
        iv = smile.iv(float(spot)) if smile is not None else None
    except Exception:  # noqa: BLE001 —— 曲面缺口/插值失败一律按「无参考 IV」
        return None
    if iv is None or float(iv) <= 0:
        return None
    return float(iv)


def percentile_rank(values: Sequence[float], current: float) -> float:
    """当前值在给定样本里的分位（0–100，``100 × #{v ≤ cur} / n``）。"""
    n = len(values)
    if n <= 0:
        return 0.0
    return 100.0 * sum(1 for v in values if float(v) <= float(current)) / n


def rolling_percentile(ivs: Sequence[Optional[float]], *, window: int,
                       min_samples: int) -> list[Optional[float]]:
    """对 IV 序列逐点算「trailing 窗口分位」；样本不足 → ``None``（fail-open 由调用方负责）。

    空值（无参考 IV）不参与窗口、也不产生分位 —— 缺口不得当成 0。
    """
    w = max(1, int(window))
    ms = max(1, int(min_samples))
    out: list[Optional[float]] = []
    hist: list[float] = []
    for v in ivs:
        if v is None:
            out.append(None)
            continue
        cur = float(v)
        hist.append(cur)
        tail = hist[-w:]
        out.append(percentile_rank(tail, cur) if len(tail) >= ms else None)
    return out


def window_bars_for(window_days: float, bar_seconds: float) -> int:
    """窗口天数 → bar 根数（至少 2 根；周期不明时按 1 小时）。"""
    b = float(bar_seconds or 0) or 3600.0
    return max(2, int(round(float(window_days or DEFAULT_WINDOW_DAYS) * 86400.0 / b)))


def min_samples_for(window_bars: int) -> int:
    """最小样本 = max(20, 窗口的 1/5) —— 少于 20 个点谈分位没有意义。"""
    return max(MIN_SAMPLES_FLOOR, int(max(1, int(window_bars)) // 5))
