"""TD 趋势状态判断（大周期趋势过滤，2026-08-31）。

用大周期（1H）K 线的 TD Setup 结构判断趋势状态，供小周期（1m）策略做
趋势过滤（单向闸门，见 docs/quant-system.md 三十章）。

设计要点（经 8/25-8/30 真实数据验证与讨论收敛）：
- 复用 ``_DeMarkEngine``（原版引擎）计算每根 K 线的 setup 计数（全序列）
- **setup 计数方向 = 群体方向性行为持续性**（羊群效应度量），不是反转预测
- **setup >= 9 累加 ≠ 耗尽/反转窗口**——原版 TD 的 setup 到 9 后继续累加
  （实测 8/28 setup_buy 累加到 19~26），持续累加 = 趋势仍在进行，禁买
- 归零（close 站回 4 根前收盘）才是方向结束信号；归零后窗口保护——
  窗口内有大方向计数则延续方向（防「1 根反弹」误开闸，宁可错过）
- 只做多视角（系统不做空）：下跌计数中一律禁买；上涨分中继/末端
  （涨势末端禁 1m 高9 追高）

状态集合：
- ``downtrend``          下跌计数中（setup_buy 1~8 或 >=9 累加）→ 禁买
- ``uptrend``            上涨计数中（setup_sell 1~8）→ 允许
- ``uptrend_exhausted``  上涨计数 >=9 累加中（涨势末端）→ 禁 1m 高9 追高
- ``ranging``            弹簧/横盘（无方向计数或窗口内均无方向）
- ``unknown``            数据不足
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.strategies.td_sequential import _DeMarkEngine
from nanobot_quant.td_params import DEFAULT_TD_PARAMS

# ──────────────────────────────────────────────────────────────────────
# 状态常量
# ──────────────────────────────────────────────────────────────────────

UPTREND = "uptrend"
UPTREND_EXHAUSTED = "uptrend_exhausted"
DOWNTREND = "downtrend"
RANGING = "ranging"
UNKNOWN = "unknown"

STATE_LABELS = {
    UPTREND: "涨势",
    UPTREND_EXHAUSTED: "涨势末端",
    DOWNTREND: "跌势",
    RANGING: "弹簧",
    UNKNOWN: "未知",
}

# Setup 计数耗尽线：>=9 视为涨势末端（禁高9 追高）。下跌侧 >=9 仍是跌势禁买。
TREND_EXHAUST = 9
# Setup 计数确立线：>=5 视为方向确立（1~4 为未确立/噪声，横盘随机波动
# 也能走到 4——实测 8/25-8/30 横盘序列 setup 偶发 1~4，若 1 即禁买则弹簧态
# 永不出现）。未确立区间由弹簧保守参数兜底，不设禁买。
TREND_CONFIRM = 5
# 状态判定窗口（根）：当前 setup 未确立时回溯多少根找最近方向
TREND_WINDOW = 8
# 最少 K 线根数：不足视为数据不足（unknown）
MIN_BARS = 30


def classify_trend_state(
    setup_buy_series: pd.Series,
    setup_sell_series: pd.Series,
    window: int = TREND_WINDOW,
) -> str:
    """由 setup 计数序列判定趋势状态（纯判定函数，可单测）。

    规则（做多视角，不做空）：
    1. 当前 setup 方向优先（最后一根的 setup 值）：
       - setup_sell >= 9 → 涨势末端（禁高9 追高）
       - setup_buy  >= 5 → 跌势（含 >=9 累加，一律禁买）
       - setup_sell >= 5 → 涨势
    2. 当前未确立（< 5，含归零 0）→ 窗口内峰值方向延续：
       - 窗口 8 根内曾有 >=5 的方向计数 → 延续该方向（防「1 根反弹」
         误开闸/误判弹簧，宁可错过）
    3. 窗口内无任何方向确立 = 弹簧
    """
    if len(setup_buy_series) < MIN_BARS or len(setup_sell_series) < MIN_BARS:
        return UNKNOWN

    last_buy = int(setup_buy_series.iloc[-1])
    last_sell = int(setup_sell_series.iloc[-1])

    # 当前 setup 方向优先（同一根只可能有一个方向计数）
    if last_sell >= TREND_EXHAUST:
        return UPTREND_EXHAUSTED
    if last_buy >= TREND_CONFIRM:
        return DOWNTREND
    if last_sell >= TREND_CONFIRM:
        return UPTREND

    # 当前未确立：窗口内峰值方向延续（保守禁买/禁高9）
    peak_buy = int(setup_buy_series.tail(window).max())
    peak_sell = int(setup_sell_series.tail(window).max())
    if peak_sell >= TREND_EXHAUST:
        return UPTREND_EXHAUSTED
    if peak_buy >= TREND_CONFIRM:
        return DOWNTREND
    if peak_sell >= TREND_CONFIRM:
        return UPTREND

    return RANGING


def compute_trend_state(
    df: pd.DataFrame,
    params: dict | None = None,
    engine_cls=None,
) -> dict:
    """计算大周期 K 线的 TD 趋势状态。

    ``df`` 须含 Open/High/Low/Close/Volume（列名大小写均可，会自动归一化）。
    返回 dict：
    {
        "state": str,          # 状态码
        "label": str,          # 中文标签
        "setup_buy": int,      # 当前 setup_buy
        "setup_sell": int,     # 当前 setup_sell
        "peak_buy": int,       # 窗口内 setup_buy 峰值
        "peak_sell": int,      # 窗口内 setup_sell 峰值
        "cd_buy": int,         # 当前 countdown buy
        "cd_sell": int,        # 当前 countdown sell
        "bars": int,           # K 线根数
        "ts": str,             # 最后一根时间
    }
    """
    if params is None:
        params = dict(DEFAULT_TD_PARAMS)
    engine = (engine_cls or _DeMarkEngine)(df, params)
    out = engine.run_all()

    sb = out["buy_setup_count"]
    ss = out["sell_setup_count"]
    state = classify_trend_state(sb, ss)

    window = TREND_WINDOW
    return {
        "state": state,
        "label": STATE_LABELS[state],
        "setup_buy": int(sb.iloc[-1]),
        "setup_sell": int(ss.iloc[-1]),
        "peak_buy": int(sb.tail(window).max()),
        "peak_sell": int(ss.tail(window).max()),
        "cd_buy": int(out["buy_countdown_count"].iloc[-1]),
        "cd_sell": int(out["sell_countdown_count"].iloc[-1]),
        "bars": int(len(df)),
        "ts": str(out.index[-1]),
    }
