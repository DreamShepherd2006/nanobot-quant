"""回测指标汇总（C125，2026-11-06）。

把对外指标表（最终权益 / 总收益率 / 实现 PnL / 期末现金 / 期末持仓市值 /
期末持仓占用资金 / 期末持仓手数 / 总开仓数 / 错过次数与错过率）以及风险、
成本、结构类派生指标集中在一个纯函数里；driver 只负责喂原始数据，不重复算。

口径写死（避免各处理解不一）：
- 最终权益 ``final_equity`` = 期末现金 + 期末持仓市值
- 实现 PnL ``realized_pnl`` = 最终权益 − 初始资金 − 未实现浮盈
  （未实现浮盈 = 持仓市值 − 持仓成本；用户表「实现PnL」即此口径）
- 期末持仓市值优先取 broker 快照（与 ``net_values`` 同口径），
  缺失时回退 Σ 未平仓量 × 期末收盘价
- 错过率 ``missed_rate_pct`` = 拦截次数 ÷ (拦截次数 + 开仓笔数)
  ⚠ 拦截次数按「候选买入 bar」计，同一信号连续多根 bar 被拦会重复计数，
  因此它是「拦截强度」而不是「错过的独立信号数」
- 每笔盈亏与持有时间用 FIFO 配对近似：按标的把买入与卖出顺序配对，
  手续费按 ``fee_rate`` 显式扣（买入侧与卖出侧各一次）

所有数值保留原精度，不做 round（格式化留给展示层）。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

# 出场原因归一化前缀（原始 reason 仍保留在 fills_detail 里）
_REASON_PREFIXES = (
    ("setup_sell", "high9"),
    ("cd_sell", "cd13"),
    ("take_profit", "take_profit"),
    ("stop_loss", "stop_loss"),
    ("eod_close", "eod"),
    ("entry_gap", "entry_gap"),
)


def normalize_exit_reason(reason: str | None) -> str:
    """把出场原因归到稳定的统计桶（只用于汇总，不改明细）。"""
    text = str(reason or "").strip()
    if not text:
        return "unknown"
    low = text.lower()
    for prefix, bucket in _REASON_PREFIXES:
        if low.startswith(prefix):
            return bucket
    return text[:32]


def _drawdown(net_values: list[dict]) -> dict[str, Any]:
    """从逐 bar 净值序列算最大回撤（绝对值、百分比、峰谷时刻）。"""
    peak = None
    peak_ts = None
    worst = 0.0
    worst_pct = 0.0
    worst_peak_ts = None
    worst_trough_ts = None
    for row in net_values or []:
        net = row.get("net")
        ts = row.get("ts")
        if net is None:
            continue
        net = float(net)
        if peak is None or net > peak:
            peak = net
            peak_ts = ts
        if peak and peak > 0:
            dd = peak - net
            dd_pct = dd / peak
            if dd > worst:
                worst = dd
                worst_pct = dd_pct
                worst_peak_ts = peak_ts
                worst_trough_ts = ts
    return {
        "max_drawdown_usd": worst,
        "max_drawdown_pct": worst_pct,
        "max_drawdown_peak_ts": worst_peak_ts,
        "max_drawdown_trough_ts": worst_trough_ts,
    }


def _fifo_trades(
    fills_detail: list[dict], fee_rate: float
) -> tuple[list[dict], float]:
    """按标的 FIFO 配对买卖，返回每笔平仓盈亏与持有小时数，以及费用估算。"""
    books: dict[str, deque] = defaultdict(deque)
    trades: list[dict] = []
    fees = 0.0
    for fill in fills_detail or []:
        sym = fill.get("symbol") or "?"
        side = str(fill.get("side") or "").lower()
        qty = float(fill.get("quantity") or 0.0)
        px = float(fill.get("avg_price") or 0.0)
        if qty <= 0 or px <= 0:
            continue
        fees += qty * px * fee_rate
        ts = fill.get("ts")
        if side == "buy":
            books[sym].append([ts, qty, px])
            continue
        if side != "sell":
            continue
        remain = qty
        pnl = 0.0
        paired_qty = 0.0
        hours_weighted = 0.0
        pairs = 0
        while remain > 1e-12 and books[sym]:
            lot = books[sym][0]
            take = min(remain, lot[1])
            pnl += take * px * (1.0 - fee_rate) - take * lot[2]
            hours = _hours_between(lot[0], ts)
            if hours is not None:
                hours_weighted += hours * take
            paired_qty += take
            pairs += 1
            lot[1] -= take
            remain -= take
            if lot[1] <= 1e-12:
                books[sym].popleft()
        # 一笔卖出可能跨多个买入批：按「卖出笔」聚合（与成交明细对齐），
        # 配对段数单独记在 pairs，持有时间按数量加权。
        trades.append(
            {
                "symbol": sym,
                "qty": qty,
                "exit_px": px,
                "pnl": pnl,
                "pairs": pairs,
                "hours": (hours_weighted / paired_qty) if paired_qty else None,
                "reason": normalize_exit_reason(fill.get("reason")),
            }
        )
    return trades, fees


def _hours_between(start: Any, end: Any) -> float | None:
    from datetime import datetime

    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds() / 3600.0


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_metrics(
    *,
    fills_detail: list[dict],
    slots_summary: dict[str, Any],
    initial_total: float,
    final_net: float,
    cash_end: float,
    positions_value_end: float,
    net_values: list[dict],
    skip_counts: dict[str, Any] | None = None,
    capital_stats: dict[str, Any] | None = None,
    fee_rate: float = 0.001,
) -> dict[str, Any]:
    """汇总对外指标表 + 风险/成本/结构派生指标。"""
    fills = list(fills_detail or [])
    entries = [f for f in fills if str(f.get("side") or "").lower() == "buy"]
    exits = [f for f in fills if str(f.get("side") or "").lower() == "sell"]

    # 期末持仓（成本取台账入场价；市值优先用 broker 快照）
    open_cost = 0.0
    open_qty: dict[str, float] = {}
    open_lots = 0
    for sym, summary in (slots_summary or {}).items():
        final_px = summary.get("final_price")
        for lot in summary.get("open") or []:
            qty = float(lot.get("qty") or 0.0)
            entry = float(lot.get("entry_price") or 0.0)
            open_lots += 1
            open_qty[sym] = open_qty.get(sym, 0.0) + qty
            open_cost += qty * (entry or (final_px or 0.0))
    positions_mv = float(positions_value_end or 0.0)
    if positions_mv <= 0 and open_qty:
        positions_mv = sum(
            qty * float((slots_summary.get(sym) or {}).get("final_price") or 0.0)
            for sym, qty in open_qty.items()
        )
    unrealized = positions_mv - open_cost

    initial_total = float(initial_total or 0.0)
    final_net = float(final_net or 0.0)
    cash_end = float(cash_end or 0.0)
    realized = final_net - initial_total - unrealized
    roi_pct = (final_net / initial_total - 1.0) * 100.0 if initial_total > 0 else 0.0

    # 交易配对（FIFO 近似）
    trades, fees_total = _fifo_trades(fills, float(fee_rate))
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    hold_hours = [t["hours"] for t in trades if t.get("hours") is not None]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # 出场原因分布（按卖出笔统计；pairs = 跨的买入批次数）
    reason_stats: dict[str, dict[str, Any]] = {}
    for trade in trades:
        bucket = reason_stats.setdefault(
            trade["reason"], {"count": 0, "pairs": 0, "pnl": 0.0, "qty": 0.0}
        )
        bucket["count"] += 1
        bucket["pairs"] += int(trade.get("pairs") or 0)
        bucket["pnl"] += trade["pnl"]
        bucket["qty"] += trade["qty"]
    for bucket in reason_stats.values():
        bucket["avg_pnl"] = (
            bucket["pnl"] / bucket["count"] if bucket["count"] else 0.0
        )

    dd = _drawdown(net_values)
    calmar = (roi_pct / (dd["max_drawdown_pct"] * 100.0)) if dd["max_drawdown_pct"] > 0 else None

    missed = int(sum(int(v or 0) for v in (skip_counts or {}).values()))
    missed_rate = (missed / (missed + len(entries)) * 100.0) if (missed + len(entries)) else 0.0

    fees_share = (fees_total / (final_net - initial_total + fees_total) * 100.0) if (final_net - initial_total + fees_total) > 0 else None

    cash_plus_mv = cash_end + positions_mv
    diff = cash_plus_mv - final_net
    tol = max(0.01, abs(final_net) * 1e-4)

    stats = capital_stats or {}
    return {
        # —— 用户指标表（对外口径）——
        "final_equity": final_net,
        "roi_pct": roi_pct,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "cash_end": cash_end,
        "positions_market_value": positions_mv,
        "positions_cost": open_cost,
        "open_lots": open_lots,
        "open_qty_by_symbol": open_qty,
        "entries": len(entries),
        "exits": len(exits),
        "missed": missed,
        "missed_rate_pct": missed_rate,
        # —— 风险 ——
        "max_drawdown_usd": dd["max_drawdown_usd"],
        "max_drawdown_pct": dd["max_drawdown_pct"] * 100.0,
        "max_drawdown_peak_ts": dd["max_drawdown_peak_ts"],
        "max_drawdown_trough_ts": dd["max_drawdown_trough_ts"],
        "calmar": calmar,
        # —— 成本与资金效率 ——
        "fees_total": fees_total,
        "fees_pct_of_gross": fees_share,
        "utilization_pct": (float(stats.get("utilization") or 0.0) * 100.0),
        "turnover": stats.get("turnover"),
        "turnover_two_side": stats.get("turnover_two_side"),
        "idle_pct": (100.0 - float(stats.get("utilization") or 0.0) * 100.0),
        # —— 交易结构 ——
        "win_rate_pct": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "avg_win_usd": (gross_profit / len(wins)) if wins else None,
        "avg_loss_usd": (-gross_loss / len(losses)) if losses else None,
        "profit_factor": profit_factor,
        "hold_hours_median": _median(hold_hours),
        "hold_hours_mean": (sum(hold_hours) / len(hold_hours)) if hold_hours else None,
        "hold_hours_max": max(hold_hours) if hold_hours else None,
        "exit_reason": reason_stats,
        # —— 一致性（不静默：现金+市值与净值不一致时标出来）——
        "consistency": {
            "cash_plus_positions_mv": cash_plus_mv,
            "final_net": final_net,
            "diff": diff,
            "ok": abs(diff) <= tol,
        },
        "method_note": (
            "盈亏/持有时间用 FIFO 买卖配对近似（一笔卖出跨多批时按笔聚合、时长数量加权）；"
            "出场结构的 count 是卖出笔数、pairs 是跨的买入批次数；"
            "手续费按 fee_rate 双侧估算；错过率按候选买入 bar 计（同一信号多根 bar 被拦会重复计）"
        ),
    }
