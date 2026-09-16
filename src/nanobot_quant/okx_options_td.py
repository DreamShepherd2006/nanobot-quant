"""期权链页「📊 标的 TD 状态」面板（C24 ⑤）——只读展示，不参与交易。

给每个标的家族算一条分钟级 TD 状态，用于人工卖 put 前看标的衰竭进度。

与自动循环同源：阈值读 ``option_params.json`` 的 ``live.strategy``
（``entry_setup`` / ``entry_countdown``），所以页面上的「距信号还差多少」
就是策略轮次实际使用的口径 —— 页面与自动化不会各说各话。

数据源 = OKX 现货 K 线（与期权链 / IV / HV 同一数据面），复用 td-table 的引擎，
口径与 TD 序列分析页一致。
"""

from __future__ import annotations

import time
from typing import Any

from nanobot_quant.okx_options_data import FAMILIES, td_kline
from nanobot_quant.td_table_handlers import _engine_run

# 面板可选周期（33.13 原设计：1m 提示级 / 5m 主力 / 15m 高质量补充）
PERIODS = ("1m", "3m", "5m", "15m")
DEFAULT_PERIOD = "5m"
DEFAULT_BARS = 120

_CACHE_TTL = 15.0
_cache: dict[str, tuple[float, dict]] = {}


def base_of(family: str) -> str:
    """SOL-USD_UM → SOL（现货对与信号都以基础币为准）。"""
    return family.split("-")[0]


def thresholds() -> dict[str, Any]:
    """自动循环用的入场阈值与周期（与 okx_options_live.live_config 同源）。

    读取失败一律回落到策略默认值（9 / 13），面板不因配置缺失而空白。
    """
    out: dict[str, Any] = {"entry_setup": 9, "entry_countdown": 13,
                           "period": DEFAULT_PERIOD, "bars": DEFAULT_BARS}
    try:
        from nanobot_quant.okx_options_live import _strategy_params, live_config

        s = _strategy_params(live_config())
        if s.get("entry_setup") is not None:
            out["entry_setup"] = int(s["entry_setup"])
        if s.get("entry_countdown") is not None:
            out["entry_countdown"] = int(s["entry_countdown"])
        if s.get("td_period"):
            out["period"] = str(s["td_period"])
        if s.get("td_bars"):
            out["bars"] = int(s["td_bars"])
    except Exception as e:  # noqa: BLE001 —— 面板是只读展示，配置异常不该 500
        out["note"] = f"阈值读取失败，用默认值：{type(e).__name__}: {e}"
    return out


def _compute(family: str, period: str, bars: int, thr: dict) -> dict[str, Any]:
    base = base_of(family)
    row: dict[str, Any] = {
        "family": family, "base": base, "period": period, "bars": bars,
        "entry_setup": thr["entry_setup"], "entry_countdown": thr["entry_countdown"],
    }
    df, err = td_kline(family, period=period, bars=bars)
    if df is None or len(df) == 0:
        row["error"] = err or "无 K 线数据"
        return row

    try:
        # 与 td-table 同一条计算路径（含列名归一化 + 所选策略变体）
        from nanobot_quant.strategies.registry import load_selected
        from nanobot_quant.td_params import load_td_params

        name = load_selected()
        seq = _engine_run(df, name, load_td_params(name))
        last = seq.iloc[-1]
        row.update({
            "strategy": name,
            "setup_buy": int(last.get("buy_setup_count", 0) or 0),
            "setup_sell": int(last.get("sell_setup_count", 0) or 0),
            "cd_buy": int(last.get("buy_countdown_count", 0) or 0),
            "cd_sell": int(last.get("sell_countdown_count", 0) or 0),
            "score": round(float(last.get("combined_score", 0) or 0), 1),
            "price": float(last.get("Close", 0) or 0),
            "bar_time": str(seq.index[-1]),
        })
    except Exception as e:  # noqa: BLE001
        row["error"] = f"TD 计算失败：{type(e).__name__}: {e}"
        return row

    es, ec = thr["entry_setup"], thr["entry_countdown"]
    ready = row["setup_buy"] >= es or row["cd_buy"] >= ec
    near = (not ready) and (row["setup_buy"] >= es - 2 or row["cd_buy"] >= ec - 2)
    row["sell_put_ready"] = ready
    row["near"] = near
    row["progress"] = f"买9 {row['setup_buy']}/{es} · CD {row['cd_buy']}/{ec}"
    return row


def family_td(family: str, period: str | None = None, bars: int | None = None) -> dict[str, Any]:
    """单个家族的 TD 状态（带 15s 缓存——面板轮询不放大外部 API 调用）。"""
    thr = thresholds()
    period = period or thr["period"]
    bars = int(bars or thr["bars"])
    if period not in PERIODS:
        period = DEFAULT_PERIOD
    key = f"td:{family}:{period}:{bars}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    row = _compute(family, period, bars, thr)
    _cache[key] = (time.time(), row)
    return row


def panel(period: str | None = None, families: tuple[str, ...] | None = None,
          strategy_families: tuple[str, ...] | None = None) -> dict[str, Any]:
    """全部家族的 TD 状态（面板一次请求拿到整张表）。

    ``strategy_families`` = 自动循环当前配置的标的家族。面板渲染的仍是全集
    （只读参考有价值），但每行带 ``in_strategy`` 标记，前端据此区分「策略真的
    会在这里卖 put」与「只是行情参考」——否则家族外的行也会显示 HOLD/临近，
    看起来像可以行动的信号。
    """
    fams = list(families or FAMILIES)
    strat = {str(f).upper() for f in (strategy_families or [])}
    rows = []
    for f in fams:
        row = family_td(f, period)
        row["in_strategy"] = str(f).upper() in strat
        rows.append(row)
    return {
        "periods": list(PERIODS),
        "rows": rows,
        "strategy_families": sorted(strat),
    }
