"""td-table 视图 → markdown（用户拍板 2026-09-21）。

用途：把「📊 TD 序列分析」页面当前所见（① 实时快照 / ② 历史区间 / TD F1
序列）连同**实际生效参数快照**渲染成 markdown，页面一键复制后可直接粘给
助手——助手据此看到完整信息（数据源/周期/根数/算法参数/阈值/表格/口径），
不必反复追问「当时用的什么参数」。

设计原则（与 ``backtest_markdown.py`` / ``f1_markdown.py`` 同构）：

* **一处渲染**：页面与将来的 MCP 工具共用本模块，避免两边格式漂移。
* **复制 = 页面所见**：列、数值格式、空值口径与 ``td_table_handlers`` 的
  HTML 渲染逐项一致（数字格式由单元测试交叉锁定，防止静默漂移）。
* 纯函数、无 I/O、无副作用；渲染失败由调用方决定是否展示（不影响页面主体）。
"""

from __future__ import annotations

import pandas as pd

__all__ = [
    "fmt_price",
    "fmt_pct",
    "fmt_score",
    "md_table",
    "params_snapshot_rows",
    "render_bars_markdown",
    "render_f1_markdown",
]

_EMPTY = ""


def fmt_price(v) -> str:
    """与 ``td_table_handlers._fmt_price`` 同口径（6 位有效数字）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _EMPTY
    if f != f:  # NaN
        return _EMPTY
    if f == 0:
        return "0"
    return f"{f:.6g}"


def fmt_pct(v) -> str:
    """涨跌幅：带符号 2 位小数；NaN/None → 空。"""
    if v is None:
        return _EMPTY
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _EMPTY
    if f != f:
        return _EMPTY
    return f"{f:+.2f}%"


def fmt_score(v) -> str:
    if v is None:
        return _EMPTY
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _EMPTY
    if f != f:
        return _EMPTY
    return f"{f:.2f}"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    """markdown 表格（数值列右对齐）。空 headers → 空串。"""
    if not headers:
        return ""
    align = []
    numeric_words = ("收盘", "涨跌", "Setup", "Countdown", "CD", "TDST", "Score", "F1", "价", "根后")
    for h in headers:
        align.append("--:" if any(w in h for w in numeric_words) else ":--")
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(
            "" if c is None else str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def params_snapshot_rows(
    params: dict,
    strategy_name: str,
    *,
    entry_setup,
    exit_setup,
    exit_cd,
    trend_period: str = "",
    execution_channel: str = "",
) -> list[list[str]]:
    """实际生效参数快照（参数 / 值 / 来源）。

    来源口径按代码事实标注（不臆造）：
    * TD 算法参数与入场/出场阈值 → ``td_params.json``（按策略独立保存）
    * 大周期趋势周期、执行通道 → ``exec_params``
    """
    p = params or {}
    src = f"td_params.json（{strategy_name}）"
    rows = [
        ["策略", str(strategy_name), "strategy.json"],
        ["Setup 周期 / Countdown 周期",
         f"{p.get('setup_period', 9)} / {p.get('countdown_period', 13)}", src],
        ["比较长度 / 回收阈值",
         f"{p.get('compare_length', 4)} / {p.get('recycle_threshold', 18)}", src],
        ["Score 阈值 / TDST 方向过滤",
         f"{p.get('score_threshold', 0)} / "
         f"{'开' if p.get('tdst_filter') else '关'}", src],
        ["入场阈值 entry_setup", str(entry_setup), src],
        ["出场阈值 exit_setup / exit_countdown",
         f"{exit_setup} / {exit_cd}", src],
    ]
    if trend_period:
        rows.append(["大周期趋势周期", str(trend_period), "exec_params.trend_period"])
    if execution_channel:
        rows.append(["执行通道", str(execution_channel), "exec_params.execution_channel"])
    return rows


def _meta_block(meta: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {k}：{v}" for k, v in meta if v)


def _notes_block(notes: list[str]) -> str:
    return "\n".join(f"> {n}" for n in notes if n)


def render_bars_markdown(
    *,
    title: str,
    meta: list[tuple[str, str]],
    params_rows: list[list[str]],
    trend_line: str = "",
    status: dict | None = None,
    disp: pd.DataFrame | None = None,
    setup: int = 9,
    stats: tuple[list[dict], dict] | None = None,
    notes: list[str] | None = None,
) -> str:
    """价格 TD 视图（① 实时快照 / ② 历史区间）→ markdown。"""
    parts = [f"## {title}", "", _meta_block(meta)]
    if trend_line:
        parts += ["", f"- {trend_line}"]

    if params_rows:
        parts += ["", "### 📋 参数快照（实际生效）", "",
                  md_table(["参数", "值", "来源"], params_rows)]

    if status:
        parts += ["", "### 🧭 当前状态", "",
                  md_table(["项", "值"], [[k, v] for k, v in status.items()])]

    if disp is not None and len(disp):
        has_cd = ("buy_countdown_count" in disp.columns
                  and disp["buy_countdown_count"].abs().sum() > 0)
        has_tdst = ("tdst_support" in disp.columns
                    and disp["tdst_support"].notna().any())
        has_score = "combined_score" in disp.columns
        heads = ["时间", "UTC 时间", "收盘", "涨跌%", "Buy Setup", "Sell Setup"]
        if has_cd:
            heads.append("Countdown")
        if has_tdst:
            heads += ["TDST 支撑", "TDST 阻力"]
        if has_score:
            heads.append("Score")
        heads.append("信号")

        rows = []
        for i in range(len(disp)):
            rec = disp.iloc[i]
            sb = int(rec.get("buy_setup_count", 0) or 0)
            ss = int(rec.get("sell_setup_count", 0) or 0)
            sig = str(rec.get("recommendation", "HOLD"))
            row = [
                str(rec.get("_time", "")),
                str(rec.get("_time_utc", "")),
                fmt_price(rec.get("Close")),
                fmt_pct(rec.get("_pct")),
                str(sb) if sb else "",
                str(ss) if ss else "",
            ]
            if has_cd:
                cd = int(rec.get("buy_countdown_count", 0) or 0)
                row.append(str(cd) if cd else "")
            if has_tdst:
                sup, res = rec.get("tdst_support"), rec.get("tdst_resistance")
                row += [fmt_price(sup), fmt_price(res)]
            if has_score:
                row.append(fmt_score(rec.get("combined_score")))
            row.append(sig if sig != "HOLD" else "—")
            rows.append(row)
        parts += ["", f"### 📈 K 线（{len(disp)} 根）", "", md_table(heads, rows)]

    if stats:
        parts += ["", "### 🎯 信号回溯统计（区间内每个 count == Setup 周期）", ""]
        sig_rows, agg = stats
        rate_rows = []
        for d, label in (("BUY", "BUY（下跌 9 后反弹胜率）"),
                         ("SELL", "SELL（上涨 9 后回落胜率）")):
            cells = [label]
            for n in (3, 5, 10):
                a = (agg or {}).get(d, {}).get(n)
                cells.append(f"{a['rate']}%（{a['win']}/{a['n']}）" if a else "—")
            rate_rows.append(cells)
        parts.append(md_table(["方向", "3 根后", "5 根后", "10 根后"], rate_rows))
        if sig_rows:
            parts += ["", md_table(
                ["触发时间", "方向", "触发价", "3 根后", "5 根后", "10 根后"],
                [[str(r["time"])[:16].replace("T", " "), r["direction"],
                  fmt_price(r["price"]), fmt_pct(r.get("pct3")),
                  fmt_pct(r.get("pct5")), fmt_pct(r.get("pct10"))]
                 for r in sig_rows])]

    if notes:
        parts += ["", _notes_block(notes)]
    return "\n".join(parts) + "\n"


def render_f1_markdown(
    *,
    title: str,
    meta: list[tuple[str, str]],
    params_rows: list[list[str]],
    trend_line: str = "",
    disp: pd.DataFrame | None = None,
    eng: pd.DataFrame | None = None,
    pct=None,
    setup: int = 9,
    lb: int = 0,
    win: int = 0,
    has_cd: bool = False,
    notes: list[str] | None = None,
) -> str:
    """TD F1 序列视图 → markdown（只读诊断，不下单）。"""
    parts = [f"## {title}", "", _meta_block(meta)]
    if trend_line:
        parts += ["", f"- {trend_line}"]
    if params_rows:
        parts += ["", "### 📋 参数快照（实际生效）", "",
                  md_table(["参数", "值", "来源"], params_rows)]

    if disp is not None and eng is not None and len(disp):
        heads = ["时间", "UTC 时间", "F1 值", "F1 分位", "Buy Setup", "Sell Setup"]
        if has_cd:
            heads += ["Buy CD", "Sell CD"]
        heads.append("信号")
        rows = []
        for i in range(len(disp) - 1, -1, -1):
            sb = int(eng["buy_setup_count"].iloc[i])
            ss = int(eng["sell_setup_count"].iloc[i])
            cb = int(eng["buy_countdown_count"].iloc[i]) if has_cd else 0
            cs = int(eng["sell_countdown_count"].iloc[i]) if has_cd else 0
            if ss >= setup or cs >= 13:
                sig = "SELL"
            elif sb >= setup or cb >= 13:
                sig = "BUY"
            else:
                sig = "—"
            p = None if pct is None else pct.iloc[i]
            rows.append([
                str(disp["_time"].iloc[i]),
                str(disp["_time_utc"].iloc[i]),
                # 预热区 ATR 不足 → 空（页面同口径，不再出现字面量 nan）
                fmt_price(disp["Close"].iloc[i]),
                f"{p * 100:.1f}%" if p is not None and p == p else "",
                str(sb) if sb else "",
                str(ss) if ss else "",
                *(([str(cb) if cb else "", str(cs) if cs else ""]) if has_cd else []),
                sig,
            ])
        parts += ["", f"### 🔍 F1 序列（{len(disp)} 根 · 分位窗口 {win} 根）", "",
                  md_table(heads, rows)]

    if notes:
        parts += ["", _notes_block(notes)]
    return "\n".join(parts) + "\n"
