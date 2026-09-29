"""期权回测参数网格（§33.43 Step 6 参数裁决）—— **一次预取、多组参数回放**。

为什么要单独一个模块：prefetch（拉归档成交 + 拟合 IV 曲面）是每组回测的耗时
大头，逐组重建数据源等于把同一批网络请求重跑 N 遍。这里共享一份
``OptionsReplayDataSource``，只换 driver 参数——每组状态互相独立（driver
自带全部回放状态），结果可直接横向比较。

输出 markdown 表（用户明确要求结果带 markdown，方便直接拷进对话）。

口径提醒：OKX 归档成交只滚动保留约 30 天，样本多为单边上涨（顺风），因此网格
结论只能作为**方向性初判**；下跌/震荡段的定稿仍要等实盘样本。
"""

from __future__ import annotations

import itertools
import sys
import time
from typing import Any, Optional

from nanobot_quant.backtest.options_driver import (
    DEFAULT_SETTLE_WINDOW_MIN,
    OptionsBacktestDriver,
)

# 轴名 → 说明（写进报告，别让读者猜 0/50/70 是什么）
AXIS_LABELS = {
    "iv_gate": "IV 分位闸门",
    "expiry": "到期档（天）",
    "tp": "止盈线（put %）",
    "chain": "资金链",
    "min_net_yield": "净收益率下限（%）",
}


def _base_opt_params(*, iv_gate: float, tp: float, expiry: tuple[float, float],
                     min_net_yield: float, td_period: str,
                     max_contracts: int,
                     iv_pct_window_days: float = 7.0) -> dict:
    return {
        "entry_setup": 9, "entry_countdown": 13, "td_period": td_period,
        "max_contracts_per_family": max_contracts,
        "max_contracts_total": max_contracts,
        "iv_min_percentile": float(iv_gate),
        "iv_pct_window_days": float(iv_pct_window_days),
        "take_profit_pct": float(tp),
        "tp_pct_call": 30.0,
        "selector": {
            "min_distance_pct": 5, "delta_min": 0.05, "delta_max": 0.35,
            "expiry_min_days": float(expiry[0]), "expiry_max_days": float(expiry[1]),
            "min_net_yield_pct": float(min_net_yield),
            "top_n": 5, "sort_by": "net_yield",
        },
    }


def run_grid(
    family: str = "SOL-USD_UM",
    *,
    days: int = 30,
    timestep: str = "15m",
    td_bars: int = 120,
    initial_cash: float = 100.0,
    iv_gates: tuple[float, ...] = (0.0,),
    expiries: tuple[tuple[float, float], ...] = ((0.0, 3.0),),
    tps: tuple[float, ...] = (50.0,),
    chain_modes: tuple[str, ...] = ("full",),
    min_net_yields: tuple[float, ...] = (0.0,),
    iv_pct_window_days: float = 7.0,
    settle_window_min: int = DEFAULT_SETTLE_WINDOW_MIN,
    end_ts: Optional[int] = None,
    data_source: Any = None,
    log=print,
) -> dict:
    """跑一组网格，返回 ``{"rows": [...], "meta": {...}}``。

    ``chain_modes`` 取 ``full``（被行权 → 补买 → covered 卖 call）或 ``off``
    （只卖 put，旧口径）——这是「资金链值不值」那一问的对照组。
    ``data_source`` 可注入（单测用假数据源，避免触网）。
    """
    from nanobot_quant.backtest.options_replay_data_source import (
        OptionsReplayDataSource,
    )

    t0 = time.time()
    end = int(end_ts or time.time())
    start = end - int(days) * 86400
    data = data_source
    if data is None:
        log(f"[OPT-GRID] 预取数据 family={family} timestep={timestep} "
            f"区间={days} 天…")
        data = OptionsReplayDataSource(family=family, timestep=timestep,
                                       start_ts=int(start), end_ts=end,
                                       length=td_bars)
        data.prefetch()                   # ① 只拉一次，后面 N 组复用
        _u = getattr(data, "_underlying", None)
        log(f"[OPT-GRID] 预取完成 用时={time.time() - t0:.1f}s "
            f"标的 bars={0 if _u is None else len(_u)}")

    combos = list(itertools.product(iv_gates, expiries, tps,
                                    min_net_yields, chain_modes))
    log(f"[OPT-GRID] 共 {len(combos)} 组参数")
    rows: list[dict] = []
    for i, (iv, expiry, tp, mny, chain) in enumerate(combos, 1):
        full = chain == "full"
        op = _base_opt_params(iv_gate=iv, tp=tp, expiry=expiry,
                              min_net_yield=mny, td_period=timestep,
                              max_contracts=10, iv_pct_window_days=iv_pct_window_days)
        drv = OptionsBacktestDriver(
            family, timestep=timestep, start_ts=start, end_ts=end,
            td_bars=td_bars, td_params={}, opt_params=op,
            initial_cash=initial_cash, data_source=data,
            cover_enabled=full, call_enabled=full,
            settle_window_min=settle_window_min,
        )
        out = drv.run()
        k = out.get("kpi") or {}
        rows.append({
            "combo": {"iv_gate": iv, "expiry": f"{expiry[0]:g}-{expiry[1]:g}",
                      "tp": tp, "min_net_yield": mny, "chain": chain,
                      "iv_pct_window_days": iv_pct_window_days},
            "label": (f"IV{iv:g}/档{expiry[0]:g}-{expiry[1]:g}/TP{tp:g}"
                      f"/净{mny:g}/{'全链' if full else '仅put'}"),
            "roi_pct": k.get("roi_pct"),
            "final_net_usd": k.get("final_net_usd"),
            "fills": k.get("fills"), "wins": k.get("wins"),
            "losses": k.get("losses"),
            "premium_put_usd": k.get("premium_put_usd"),
            "premium_call_usd": k.get("premium_call_usd"),
            "buyback_cost_usd": k.get("buyback_cost_usd"),
            "payout_usd": k.get("payout_usd"),
            "cover_spend_usd": k.get("cover_spend_usd"),
            "spot_value_usd": k.get("spot_value_usd"),
            "cost_basis_max": k.get("cost_basis_max"),
            "fees_usd": k.get("fees_usd"),
            "top_skips": _top_skips(out.get("skips") or {}),
            "error": out.get("error"),
        })
        log(f"[OPT-GRID] {i}/{len(combos)} {rows[-1]['label']} "
            f"ROI={rows[-1]['roi_pct']}% 成交={rows[-1]['fills']} "
            f"（全链用时 {time.time() - t0:.0f}s 累计）")

    return {
        "rows": rows,
        "meta": {
            "family": family, "days": days, "timestep": timestep,
            "td_bars": td_bars, "initial_cash": initial_cash,
            "settle_window_min": settle_window_min,
            "bars": getattr(data, "bar_times", None) and len(data.bar_times),
            "elapsed_s": round(time.time() - t0, 1),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        },
    }


def _top_skips(skips: dict, n: int = 3) -> str:
    items = sorted(skips.items(), key=lambda kv: -kv[1])[:n]
    return "；".join(f"{k} {v}" for k, v in items)


def render_grid_markdown(grid: dict) -> str:
    """网格结果 → markdown（对齐 backtest_markdown 的口径提示习惯）。"""
    meta, rows = grid["meta"], grid["rows"]
    lines = [
        f"## 期权回测参数网格 · {meta['family']}",
        "",
        f"- 区间：最近 {meta['days']} 天 · 周期 {meta['timestep']} · TD 窗口 {meta['td_bars']} 根 · 初始资金 {meta['initial_cash']:g}",
        f"- 结算价口径：到期前 {meta['settle_window_min']} 分钟标的均价（回放近似）",
        f"- 生成时间：{meta['generated_at']} · 用时 {meta['elapsed_s']}s · 共 {len(rows)} 组",
        "",
        "| 组合 | ROI% | 成交 | 盈/亏 | put 权利金 | call 权利金 | 止盈买回 | 行权赔付 | 补买支出 | 现货市值 | 成本锚C上限 | 手续费 | 主要 SKIP |",
        "|:--|--:|--:|:--|--:|--:|--:|--:|--:|--:|--:|--:|:--|",
    ]
    for r in rows:
        if r.get("error"):
            lines.append(f"| {r['label']} | — | — | — | — | — | — | — | — | — | — | — | {r['error']} |")
            continue
        lines.append(
            f"| {r['label']} | {r['roi_pct']} | {r['fills']} | {r['wins']}/{r['losses']} "
            f"| {r['premium_put_usd']} | {r['premium_call_usd']} | {r['buyback_cost_usd']} "
            f"| {r['payout_usd']} | {r['cover_spend_usd']} | {r['spot_value_usd']} "
            f"| {r['cost_basis_max']} | {r['fees_usd']} | {r['top_skips']} |")
    lines += [
        "",
        "> 口径：ROI =（期末净值 − 初始资金）÷ 初始资金；期末净值 = 现金 − 未平仓持仓负债 + 接货现货市值。",
        "> 归档成交只滚动保留约 30 天且样本多为单边上涨（顺风），网格结论仅作**方向性初判**。",
    ]
    return "\n".join(lines)


def _parse_expiry(text: str) -> tuple[float, float]:
    a, _, b = text.partition("-")
    return float(a), float(b or a)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="期权回测参数网格")
    ap.add_argument("--family", default="SOL-USD_UM")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--timestep", default="15m")
    ap.add_argument("--td-bars", type=int, default=120)
    ap.add_argument("--cash", type=float, default=100.0)
    ap.add_argument("--iv", default="0", help="逗号分隔，如 0,50,70")
    ap.add_argument("--expiry", default="0-3", help="逗号分隔，如 0-3,7-14")
    ap.add_argument("--tp", default="50", help="逗号分隔，如 30,50,70")
    ap.add_argument("--chain", default="full", help="逗号分隔：full,off")
    ap.add_argument("--min-net-yield", default="0", help="逗号分隔")
    ap.add_argument("--iv-window", type=float, default=7.0,
                    help="IV 分位滚动窗口（天），默认 7")
    ap.add_argument("--settle-window", type=int, default=DEFAULT_SETTLE_WINDOW_MIN)
    ap.add_argument("--out", default="", help="markdown 输出路径")
    a = ap.parse_args(argv)

    grid = run_grid(
        a.family, days=a.days, timestep=a.timestep, td_bars=a.td_bars,
        initial_cash=a.cash,
        iv_gates=tuple(float(x) for x in a.iv.split(",") if x.strip()),
        expiries=tuple(_parse_expiry(x) for x in a.expiry.split(",") if x.strip()),
        tps=tuple(float(x) for x in a.tp.split(",") if x.strip()),
        chain_modes=tuple(x.strip() for x in a.chain.split(",") if x.strip()),
        min_net_yields=tuple(float(x) for x in a.min_net_yield.split(",") if x.strip()),
        iv_pct_window_days=a.iv_window,
        settle_window_min=a.settle_window,
    )
    md = render_grid_markdown(grid)
    print(md)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        print(f"\n[OPT-GRID] markdown 已写入 {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
