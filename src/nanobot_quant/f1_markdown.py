"""F1 分析结果的 markdown 渲染（§33.36）。

给 ``analyze_f1_td`` / ``analyze_f1_drawdown`` 的结果生成可拷贝的 markdown，
用途有二：

* WebUI「📊 F1 模式回测」分栏的「📋 复制 Markdown」按钮
* MCP 工具返回值的 ``markdown`` 字段（agent 直接读）

渲染口径（用户 2026-09-21 拍板）：

1. **参数快照在最前**——先展示实际生效的标的/源/周期/lookback/ATR/k，
   再给结论表（沿用现货回测「先参数后 KPI」的惯例）。
2. **回撤诊断三列并列**（段回撤比 / 单根比 / 插针比）——§33.34 永久规则
   「幅度可预测 ≠ 风险可降」的落地。缺「插针比」会误导（601127 的 1D
   价格 sell9 段回撤比 0.62 看着安全，插针比 1.53 才是真相）。
3. 空值统一显示 ``—``，不补 ``%``、不编造缺失字段。

两种结果结构不同，故分两个渲染器：

* ``td``（``analyze_f1_td``）：results 扁平，指标直接挂在 rec 上
  （``f1_buy9`` / ``f1_sell9`` / ``price_buy9`` …），每条含
  ``{n, median, hit, p}``。
* ``drawdown``（``analyze_f1_drawdown``）：results 嵌套
  ``horizons[k].segments[seg][口径]``，每次口径含
  ``{n, segment_min, bar_worst, breach_count}``，各比值含
  ``{n, ratio, trigger, control}``。
"""

from __future__ import annotations

from typing import Any, Optional

# analyze_f1_td：口径键 → 展示名（顺序即表内顺序）
# 2026-09-21 口径修正：F1 行以**价格**为被测量对象（F1 出信号、看价格反应）
_TD_ROWS: tuple[tuple[str, str], ...] = (
    ("f1_buy9", "F1 buy9 → 价格"),
    ("f1_sell9", "F1 sell9 → 价格"),
    ("f1_buy9_all", "F1 buy9 → 价格（累加期）"),
    ("f1_sell9_all", "F1 sell9 → 价格（累加期）"),
    ("price_buy9", "价格 buy9（对照）"),
    ("price_sell9", "价格 sell9（对照）"),
    ("price_buy9_all", "价格 buy9（累加期）"),
    ("price_sell9_all", "价格 sell9（累加期）"),
    ("f1_self_buy9", "F1 buy9 → F1（自反应·参考）"),
    ("f1_self_sell9", "F1 sell9 → F1（自反应·参考）"),
)

# analyze_f1_drawdown：口径键 → 展示名
_DD_ROWS: tuple[tuple[str, str], ...] = (
    ("buy9", "F1 buy9"),
    ("sell9", "F1 sell9"),
    ("price_buy9", "价格 buy9（对照）"),
    ("price_sell9", "价格 sell9（对照）"),
)

_DD_COLS: tuple[tuple[str, str], ...] = (
    ("segment_min", "段回撤比"),
    ("bar_worst", "单根比"),
    ("breach_count", "插针比"),
)


def _fmt_num(v: Any, nd: int = 3) -> str:
    """数值格式化：None/NaN → ``—``。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "—"
    return f"{f:.{nd}f}"


def _fmt_ratio(v: Any) -> str:
    """比值：附「更浅/更深」方向提示（>1 = 更深）。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return "—"
    if f > 1.0:
        return f"**{f:.2f}** 🔴"  # 比对照更深
    return f"{f:.2f} 🟢"  # 比对照更浅


def _short_note(note: Any, limit: int = 220) -> str:
    """口径说明太长会淹没结论表（源头 note 常带几千字实证背景）。

    markdown 是给人眼看的摘要，只取首句（第一个句号/换行前）+ 截断，
    完整口径留在工具源码与 §33.36 文档里。
    """
    if not note:
        return ""
    text = str(note).replace("\n", " ").strip()
    for sep in ("。", ". "):
        idx = text.find(sep)
        if 0 < idx < limit:
            return text[: idx + len(sep)].strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def _header(title: str, payload: dict) -> list[str]:
    """标题 + 汇总行 + 参数快照。"""
    lines = [f"## {title}", ""]
    summary = payload.get("summary")
    if summary:
        lines += [str(summary), ""]
    src = payload.get("data_source")
    if src:
        lines.append(f"- 数据源：`{src}`")
    note = _short_note(payload.get("note"))
    if note:
        lines.append(f"- 口径说明：{note}")
    if src or note:
        lines.append("")
    return lines


def _err_lines(results: list[dict]) -> list[str]:
    """失败条目单列——**不得静默降级成「无数据」**（用户永久规则）。"""
    bad = [r for r in results if r.get("status") != "ok"]
    if not bad:
        return []
    out = ["### ⚠️ 失败条目", "", "| 标的 | 周期 | 原因 |", "|:--|:--|:--|"]
    for r in bad:
        out.append(
            f"| {r.get('symbol') or '—'} | {r.get('period') or '—'} "
            f"| {r.get('error') or '未知'} |"
        )
    out.append("")
    return out


def render_td_markdown(payload: dict) -> str:
    """``analyze_f1_td`` 结果 → markdown。"""
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    lines = _header("📊 F1 的 TD 触发统计", payload)

    # ── 参数快照 ──────────────────────────────────────────────
    lines += ["### 📋 参数快照（实际生效）", ""]
    lines += ["| 标的 | 周期 | 源 | 根数 | 跨度(天) | lookback | CV | 提示 |"]
    lines += ["|:--|:--|:--|--:|--:|--:|--:|:--|"]
    for r in results:
        if r.get("status") != "ok":
            continue
        lines.append(
            f"| {r.get('symbol')} | {r.get('period')} | {r.get('source') or '—'} "
            f"| {r.get('bars')} | {_fmt_num(r.get('days'), 1)} "
            f"| {r.get('lookback_bars')} | {_fmt_num(r.get('cv'), 4)} "
            f"| {r.get('cv_hint') or '—'} |"
        )
    lines.append("")

    # ── 触发统计 ──────────────────────────────────────────────
    for r in results:
        if r.get("status") != "ok":
            continue
        rows = [(k, label) for k, label in _TD_ROWS if isinstance(r.get(k), dict)]
        if not rows:
            continue
        lines += [
            f"### 📈 {r.get('symbol')} · {r.get('period')} 触发统计",
            "",
            "> median / hit / p 对应阈值触发后 k 根的变化（k 按**根**计、非时间："
            "12 根 5m = 1 小时，12 根 1H = 3 个交易日），sign 已按衰竭方向取正"
            "（buy9 期望回升、sell9 期望回落）。p 来自 200 次随机位置对照。"
            "**F1 行以价格为被测量对象**（F1 出信号、看价格反应）；「自反应」行"
            "量 F1 序列自身回升（波动率均值回归，仅参考、不含交易含义）。",
            "",
            "| 口径 | n | 中位 | 命中率 | p |",
            "|:--|--:|--:|--:|--:|",
        ]
        for key, label in rows:
            d = r[key]
            lines.append(
                f"| {label} | {d.get('n', '—')} | {_fmt_num(d.get('median'))} "
                f"| {_fmt_num(d.get('hit'))} | {_fmt_num(d.get('p'))} |"
            )
        lines.append("")

    lines += _err_lines(results)
    lines.append(
        "> ⚠️ **本工具量的是「衰竭方向」（会不会收回来），不量回撤深度。**"
        "要看回撤/尾部请用 `analyze_f1_drawdown`。\n"
        ">\n"
        "> 口径说明（2026-09-21 修正）：F1 行此前误把 F1 自身当被测量对象，"
        "会得出 p=0.000 的同义反复（波动率均值回归）；现改为看**价格**反应。"
        "「自反应」行仍量 F1 自身，仅供参考。"
    )
    return "\n".join(lines)


def render_drawdown_markdown(payload: dict) -> str:
    """``analyze_f1_drawdown`` 结果 → markdown。"""
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    lines = _header("📉 F1 的 TD 回撤诊断", payload)

    # ── 参数快照 ──────────────────────────────────────────────
    lines += ["### 📋 参数快照（实际生效）", ""]
    lines += ["| 标的 | 周期 | 根数 | 跨度(天) | lookback |"]
    lines += ["|:--|:--|--:|--:|--:|"]
    for r in results:
        if r.get("status") != "ok":
            continue
        lines.append(
            f"| {r.get('symbol')} | {r.get('period')} | {r.get('bars')} "
            f"| {_fmt_num(r.get('days'), 1)} | {r.get('lookback_bars')} |"
        )
    lines.append("")

    # ── 回撤诊断（三列并列，强制）─────────────────────────────
    for r in results:
        if r.get("status") != "ok":
            continue
        horizons = r.get("horizons") or []
        if not horizons:
            continue
        lines += [
            f"### 📉 {r.get('symbol')} · {r.get('period')} 回撤诊断",
            "",
            "> 比值 <1 = 触发后回撤**更浅**，>1 = **更深**（已按 ATR 归一，"
            "剔除波动水平差异）。对照 = 同序列随机起点、同样本量。",
            "",
            "| k | 段 | 口径 | n | 段回撤比 | 单根比 | 插针比 |",
            "|--:|:--|:--|--:|--:|--:|--:|",
        ]
        for h in horizons:
            k = h.get("k")
            for seg in h.get("segments") or []:
                seg_name = seg.get("segment") or "—"
                for key, label in _DD_ROWS:
                    d = seg.get(key)
                    if not isinstance(d, dict):
                        continue
                    cells = []
                    for col, _ in _DD_COLS:
                        sub = d.get(col)
                        cells.append(
                            _fmt_ratio(sub.get("ratio") if isinstance(sub, dict) else None)
                        )
                    lines.append(
                        f"| {k} | {seg_name} | {label} | {d.get('n', '—')} "
                        f"| {cells[0]} | {cells[1]} | {cells[2]} |"
                    )
        lines.append("")

    lines += [
        "> ⚠️ **三列必须一起看。** 段回撤比（整段最深）走低不代表风险下降——"
        "插针比（触及频率）可能同时升高。§33.34 永久规则：**幅度可预测 ≠ 风险可降**。",
    ]
    lines += _err_lines(results)
    return "\n".join(lines)


def render_markdown(payload: Optional[dict]) -> str:
    """按 ``kind`` 分派；未知 kind 返回空串（调用方保持静默增强语义）。

    ``kind`` 取值：``"f1_td"`` / ``"f1_drawdown"``。缺省时按结构猜
    （有 ``horizons`` 视作 drawdown），保证老记录也能渲染。
    """
    if not isinstance(payload, dict):
        return ""
    kind = str(payload.get("kind") or "").strip().lower()
    if kind in ("f1_td", "f1-td", "td"):
        return render_td_markdown(payload)
    if kind in ("f1_drawdown", "f1-drawdown", "drawdown"):
        return render_drawdown_markdown(payload)
    # 兜底：按结构判断
    for r in payload.get("results") or []:
        if isinstance(r, dict) and r.get("horizons"):
            return render_drawdown_markdown(payload)
    if payload.get("results"):
        return render_td_markdown(payload)
    return ""
