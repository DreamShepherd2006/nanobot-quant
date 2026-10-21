"""逐 bar 明细（TD + EMA）CSV 导出（2026-10-20）。

纯导出层：数据取自回测结果 JSON 的 ``bars_rows`` / ``bars_meta``，**不重算任何
指标**——页面表怎么来的，CSV 就是同一份值（单一来源）。

列与页面「📊 逐 bar 明细」表一一对应：
时间(CST) / UTC 时间 / 标的 / 收盘 / 涨跌% / Buy Setup / Sell Setup / Score /
EMA / slope{k} / 闸门 / 动作/原因 / 成交

导出的是**全部行**（不受页面上「只看有动作/成交」「全部标的」筛选影响）——
用户要的就是"一整段"，筛选只是浏览辅助。

口径说明：
- ``ts`` 在回测结果里是 naive UTC（``Timestamp.isoformat()``）；CST 列按 UTC+8
  直接换算字符串，不经本地时区，避免跟随服务器时区漂移（2026-08-28 时区教训）。
- 数值列原样输出（10 位有效数字），**不做 round**——展示层负责取整，
  导出值要能拿去做分析（2026-08-28 ``round(2)`` 事故的教训）。
- 0 值计数列（Buy/Sell Setup）在页面上显示为空，这里同样留空，保持两处一致。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

BOM = "\ufeff"

_GATE_TEXT = {"block": "拦", "pass": "放行", "na": "预热不足"}


def _utc_dt(ts):
    """把回测结果里的时间戳解析成 naive UTC datetime；解析不了返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fmt_utc(ts) -> str:
    dt = _utc_dt(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _fmt_cst(ts) -> str:
    dt = _utc_dt(ts)
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _num(v) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.10g}"
    except (TypeError, ValueError):
        return str(v)


def _count(v) -> str:
    """Setup 计数：0 与空一律留空（与页面 ``r.setup_buy || ""`` 一致）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    return "" if n == 0 else str(n)


def _percent(v) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{float(v) * 100:.4f}"
    except (TypeError, ValueError):
        return ""


def bars_header(meta: dict | None = None) -> list[str]:
    meta = meta or {}
    k = meta.get("ema_k") or 3
    return [
        "时间(CST)",
        "UTC 时间",
        "标的",
        "收盘",
        "涨跌%",
        "Buy Setup",
        "Sell Setup",
        "Score",
        "EMA",
        f"slope{k}",
        "闸门",
        "动作/原因",
        "成交",
    ]


def bars_merge_header(result: dict) -> list[str]:
    """多周期对齐 CSV 表头（2026-10-21 方案 A）。"""
    result = result or {}
    periods = _merged_period_order(result)
    head = ["时间(CST)", "UTC 时间", "标的", "收盘", "涨跌%"]
    for p in periods:
        meta = ((result.get("period_results") or {}).get(p) or {}).get("bars_meta") or {}
        k = meta.get("ema_k") or 3
        head += [
            f"{p} Buy Setup",
            f"{p} Sell Setup",
            f"{p} EMA",
            f"{p} slope{k}",
            f"{p} 趋势",
            f"{p} 动作/原因",
            f"{p} 成交",
        ]
    return head


def _merged_period_order(result: dict) -> list[str]:
    """按周期长短排序（最细在前）——与页面「行基准 = 最细周期」一致。"""
    per = result.get("period_results") or {}
    try:
        from nanobot_quant.data_sources.periods import INTERVAL_SECONDS
    except Exception:  # pragma: no cover - 理论上不会发生
        INTERVAL_SECONDS = {}
    order = list((result.get("periods") or per.keys()))
    return sorted(order, key=lambda p: INTERVAL_SECONDS.get(p, 10**9))


def bars_merge_to_csv(result: dict) -> str:
    """多周期对齐 CSV：**以最细周期为行基准**，其余周期只在自己 bar 收盘行填值。

    - 口径与页面「🔀 多周期对齐」视图一致（同一份 bars_rows，不重算）；
    - 缺失单元格留空（不写 0，避免被当成"指标值 0"）；
    - 三档是独立实验：同一时间行的动作分属不同回测，**不可相加**。
    """
    result = result or {}
    per_results = result.get("period_results") or {}
    periods = _merged_period_order(result)
    rows_by_p = {}
    for p in periods:
        sub = per_results.get(p) or {}
        rows_by_p[p] = {
            str(r.get("ts")): r for r in (sub.get("bars_rows") or []) if isinstance(r, dict)
        }
    base_p = next((p for p in periods if rows_by_p[p]), None)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(bars_merge_header(result))
    if base_p is None:
        return BOM + buf.getvalue()
    base_rows = [
        r for r in (per_results.get(base_p) or {}).get("bars_rows") or []
        if isinstance(r, dict)
    ]
    for b in base_rows:
        ts = str(b.get("ts"))
        row = [
            _fmt_cst(b.get("ts")),
            _fmt_utc(b.get("ts")),
            b.get("symbol") or "",
            _num(b.get("close")),
            _percent(b.get("chg")),
        ]
        for p in periods:
            r = rows_by_p[p].get(ts)
            if not r:
                row += [""] * 7
                continue
            row += [
                _count(r.get("setup_buy")),
                _count(r.get("setup_sell")),
                _num(r.get("ema")),
                _num(r.get("slope")),
                _trend_text(r.get("slope")),
                r.get("action") or "",
                r.get("fill") or "",
            ]
        w.writerow(row)
    return BOM + buf.getvalue()


def _trend_text(slope) -> str:
    """趋势列（2026-10-21 用户拍板）：由 slope_k 符号判定、**每根 bar 都有值**。

    与闸门规则同源（slope_k > 0 才放行），但不含「候选买入计数」口径——
    实际闸门判定结果仍只在「动作/原因」列（如 `WAIT:EMA 趋势闸门拦截`）。
    """
    if slope is None:
        return "⊘"
    try:
        v = float(slope)
    except (TypeError, ValueError):
        return ""
    return "✅" if v > 0 else "⛔"


def bars_to_csv(result: dict) -> str:
    """把一次回测结果渲染成 CSV 文本（带 UTF-8 BOM，Excel 直接识别中文）。"""
    result = result or {}
    rows = result.get("bars_rows") or []
    meta = result.get("bars_meta") or {}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(bars_header(meta))
    for r in rows:
        if not isinstance(r, dict):
            continue
        w.writerow(
            [
                _fmt_cst(r.get("ts")),
                _fmt_utc(r.get("ts")),
                r.get("symbol") or "",
                _num(r.get("close")),
                _percent(r.get("chg")),
                _count(r.get("setup_buy")),
                _count(r.get("setup_sell")),
                _num(r.get("score")),
                _num(r.get("ema")),
                _num(r.get("slope")),
                _GATE_TEXT.get(str(r.get("gate") or ""), ""),
                r.get("action") or "",
                r.get("fill") or "",
            ]
        )
    return BOM + buf.getvalue()
