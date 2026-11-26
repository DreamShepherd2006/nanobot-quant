"""A 股模拟账本 · 成交与持仓 Markdown（「📋 复制成交」的唯一实现）。

与 ``params_md`` 同一套路：后端渲染、页面只负责复制；**单一来源**是
``paper_runner.state()``（页面自己也是读它），本模块不重算盈亏、不另建账。

分工（durable）：
- 参数快照（``params_md``）保持「配置 + 6 行账本摘要」，稳定小体积；
- 成交明细单独一份（本模块），因为条数随轮次增长、且要能按条数截断。

两臂必须**分开列**：同一份行情、同一套入场/选档，只差出场规则 ——
混成一张表会让「同一成交出现两次」看起来像重复下单。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import paper_runner as pr
from . import paper_strategy as ps

CST = timezone(timedelta(hours=8))
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

ACTION_LABELS = {
    "sell_open": "卖出开仓",
    "buy_close": "买入平仓",
    "settle_otm": "到期作废",
    "assign": "到期接货",
}
CLOSING_ACTIONS = ("buy_close", "settle_otm", "assign")


# ── 格式化（定点、不用科学计数法）────────────────────────────
def _n(v: Any, nd: int = 2) -> str:
    """定点显示（固定小数位，不用科学计数法）。"""
    if v in (None, ""):
        return "—"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _qty(v: Any) -> str:
    return _n(v, 3)


def _parse_ts(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ts_both(ts: Any) -> tuple[str, str]:
    """返回 (CST, UTC) 两列文本；解析失败原样返回。"""
    dt = _parse_ts(ts)
    if dt is None:
        return (str(ts or "—"), "—")
    return (dt.astimezone(CST).strftime("%m-%d %H:%M:%S"),
            dt.astimezone(timezone.utc).strftime("%m-%d %H:%M:%S"))


def _dte(expiry: Any) -> str:
    """到期还有几个自然日（A 股期权按日到期，日历日即可）。"""
    try:
        y, m, d = (int(str(expiry)[0:4]), int(str(expiry)[4:6]), int(str(expiry)[6:8]))
        days = (date(y, m, d) - datetime.now(CST).date()).days
    except (TypeError, ValueError):
        return "—"
    return f"{days} 天" if days >= 0 else "已到期"


# ── 两臂统计（params_md 的摘要段也复用，单一来源）────────────
def arm_stats(payload: dict, arm: str) -> dict:
    """从 ``paper_runner.state()`` 的 payload 抽一臂的汇总指标。"""
    view = (payload.get("arms") or {}).get(arm) or {}
    led = view.get("ledger") or {}
    fills = list(view.get("fills") or [])
    closed = [f for f in fills if f.get("action") in CLOSING_ACTIONS]
    wins = [f for f in closed if float(f.get("realized") or 0) > 0]
    open_qty = sum(int((s or {}).get("qty") or 0) for s in (led.get("shorts") or []))
    last = max(fills, key=lambda f: str(f.get("ts") or ""), default=None)
    return {
        "arm": arm,
        "label": ps.ARM_LABELS.get(arm, arm),
        "active": bool(view.get("active", True)),
        "equity": led.get("equity"),
        "return_pct": led.get("return_pct"),
        "realized": led.get("realized"),
        "unrealized": led.get("unrealized"),
        "collateral": led.get("collateral_locked"),
        "available_cash": led.get("available_cash"),
        "open_qty": open_qty,
        "open_n": len(led.get("shorts") or []),
        "closed": len(closed),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else None,
        "fills_n": len(fills),
        "last_ts": (last or {}).get("ts"),
        "consistency": view.get("consistency") or {},
    }


def _arm_rule(arm: str) -> str:
    return "止盈回落 50% / DTE 强平" if arm == ps.ARM_A else "TD 高9 且浮盈 / DTE 强平（D 方案）"


def summary_table(payload: dict) -> list[str]:
    """两臂汇总表（Markdown 行）——params_md 与本模块共用。"""
    rows = ["| 臂 | 出场规则 | 净值 | 收益率 | 已实现 | 未实现 | 持仓(张) | 已平仓 | 胜率 | 最近成交(CST) |",
            "|:---|:---|---|---|---|---|---|---|---|---|"]
    for arm in (ps.ARM_A, ps.ARM_B):
        s = arm_stats(payload, arm)
        if not s["active"]:
            continue
        wr = "—" if s["win_rate"] is None else f"{_n(s['win_rate'])}%"
        rows.append(
            f"| {s['label']} | {_arm_rule(arm)} | {_n(s['equity'])} | "
            f"{_n(s['return_pct'], 4)}% | {_n(s['realized'])} | {_n(s['unrealized'])} | "
            f"{s['open_qty']} | {s['closed']} | {wr} | {_ts_both(s['last_ts'])[0]} |")
    return rows


# ── 成交 / 持仓明细 ─────────────────────────────────────────
def _fill_row(f: dict) -> str:
    action = f.get("action") or ""
    cst, utc = _ts_both(f.get("ts"))
    amount = f.get("premium")
    if amount is None:
        amount = f.get("cost")
    if amount is None:
        amount = f.get("assign_cost")
    realized = f.get("realized")
    reason = f.get("reason") or f.get("note") or ""
    return (f"| {cst} | {utc} | {f.get('underlying') or ''} | {f.get('code') or ''} "
            f"({_n(f.get('strike'))} · {f.get('expiry') or ''}) | "
            f"{ACTION_LABELS.get(action, action)} | {f.get('qty')} | {_n(f.get('px'), 4)} | "
            f"{_n(amount)} | {_n(f.get('fee'))} | "
            f"{'—' if realized in (None, '') else _n(realized)} | {reason} |")


def _position_row(sp: dict) -> str:
    return (f"| {sp.get('code')} | {sp.get('underlying')} | {_n(sp.get('strike'))} | "
            f"{sp.get('expiry')} | {_dte(sp.get('expiry'))} | {sp.get('qty')} | "
            f"{_n(sp.get('entry_px'), 4)} | {_n(sp.get('mark_px'), 4)} | "
            f"{_n(sp.get('pnl_pct'))}% | {_n(sp.get('unrealized'))} | "
            f"{_n(sp.get('collateral'))} | {_ts_both(sp.get('entry_ts'))[0]} |")


# ── 渲染 ─────────────────────────────────────────────────────
def build(limit: int = DEFAULT_LIMIT) -> str:
    """渲染成交与持仓 Markdown（只读）。``limit=0`` 表示该臂全部保留的成交。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(0, min(limit, MAX_LIMIT))

    payload = pr.state(limit_fills=pr.FILLS_KEEP)   # 取全量（账本本身只保留 FILLS_KEEP 条）
    now = datetime.now(CST)
    arms = [a for a in (ps.ARM_A, ps.ARM_B) if arm_stats(payload, a)["active"]]

    out: list[str] = ["# A 股模拟账本 · 成交与持仓", ""]
    out.append(f"- 生成时间：**{now:%Y-%m-%d %H:%M:%S} CST**"
               f"（{now.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC）")
    out.append(f"- 账本目录：`{pr.paper_dir()}`（保留最近 {pr.FILLS_KEEP} 笔成交）")
    runner = payload.get("runner") or {}
    alive = "🟢 运行中" if runner.get("thread_alive") else "⚪ 未运行"
    out.append(f"- 模拟循环：{alive} · 上次 {_ts_both(runner.get('last_run'))[0]} CST")
    if limit == 0:
        out.append(f"- 展示范围：每臂全部保留成交（≤{pr.FILLS_KEEP} 笔）")
    else:
        out.append(f"- 展示范围：每臂最近 **{limit}** 笔成交（`?limit=` 可调，0 = 全部）")
    if payload.get("error"):
        out.append(f"- ⚠️ 状态读取：{payload['error']}")
    out.append("")
    out.append("> 两臂共用同一份行情与同一套入场、选档规则，只差出场；持仓会分叉，"
               "**不是重复下单**。资金按 100% 全损担保占用。")
    out.append("")

    out.append("## 两臂汇总")
    out.append("")
    out.extend(summary_table(payload))
    out.append("")

    for arm in arms:
        s = arm_stats(payload, arm)
        view = (payload.get("arms") or {}).get(arm) or {}
        fills = list(view.get("fills") or [])
        shown = fills[-limit:] if limit else fills
        out.append(f"## 臂 {s['label']} — {_arm_rule(arm)}")
        out.append("")
        cons = s["consistency"] or {}
        cons_txt = "✅ 一致" if cons.get("ok", True) else f"⚠️ 不一致：{cons}"
        out.append(f"- 净值 {_n(s['equity'])} 元 · 收益率 {_n(s['return_pct'], 4)}% · "
                   f"已实现 {_n(s['realized'])} · 未实现 {_n(s['unrealized'])} · "
                   f"担保占用 {_n(s['collateral'])} · 可用现金 {_n(s['available_cash'])}")
        out.append(f"- 成交 {s['fills_n']} 笔（已平仓 {s['closed']}）· "
                   f"持仓 {s['open_n']} 个合约 / {s['open_qty']} 张 · 对账 {cons_txt}")
        out.append("")
        if shown:
            out.append("### 成交明细")
            out.append("")
            out.append("| 时间(CST) | 时间(UTC) | 标的 | 合约(strike · 到期) | 动作 | 张数 | "
                       "价格 | 金额(元) | 手续费 | 已实现 | 原因 |")
            out.append("|:---|:---|:---|:---|:---|---|---|---|---|---|---|")
            for f in reversed(shown):      # 时间正序更好读
                out.append(_fill_row(f))
        else:
            out.append("### 成交明细")
            out.append("")
            out.append("（暂无成交）")
        out.append("")
        shorts = (view.get("ledger") or {}).get("shorts") or []
        out.append("### 当前持仓")
        out.append("")
        if shorts:
            out.append("| 合约 | 标的 | 行权价 | 到期 | 剩余 | 张数 | 开仓价 | 现价 | "
                       "浮盈% | 浮盈(元) | 担保占用 | 开仓时间(CST) |")
            out.append("|:---|:---|:---|---|---|---|---|---|---|---|---|---|---|")
            for sp in shorts:
                out.append(_position_row(sp))
        else:
            out.append("（暂无持仓）")
        etf = (view.get("ledger") or {}).get("etf") or []
        if etf:
            out.append("")
            out.append("### ETF 接货持仓")
            out.append("")
            out.append("| 标的 | 份数 | 成本(元/份) | 现价 | 市值 | 浮盈(元) | 接货时间(CST) |")
            out.append("|:---|---|---|---|---|---|---|")
            for h in etf:
                out.append(f"| {h.get('underlying')} | {_qty(h.get('qty'))} | "
                           f"{_n(h.get('cost_basis'), 4)} | {_n(h.get('mark'), 4)} | "
                           f"{_n(h.get('value'))} | {_n(h.get('unrealized'))} | "
                           f"{_ts_both(h.get('since'))[0]} |")
        out.append("")

    out.append("## 口径")
    out.append("")
    out.append("- 出场动作：`卖出开仓` / `买入平仓`（止盈回落或 DTE 强平）/ `到期作废`（OTM 权利金全收）"
               "/ `到期接货`（ITM 按行权价买 ETF，实物交割）。")
    out.append("- 胜率 = 已平仓单中 `已实现 > 0` 的占比，**按笔不按张**；B 臂拿得更久，"
               "胜率天然偏高，与 A 不可直接比大小。")
    out.append("- 模拟账本不连券商、不下单、不读 QMT 持仓 —— 不能当实盘验收证据。")
    out.append("")
    return "\n".join(out)


__all__ = ["build", "arm_stats", "summary_table", "DEFAULT_LIMIT", "MAX_LIMIT",
           "ACTION_LABELS", "CLOSING_ACTIONS"]
