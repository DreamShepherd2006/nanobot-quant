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
