"""执行参数 markdown 快照（2026-11-02）。

动机：排查实盘问题时，助手需要看到「当前生效的那套参数」——此前只能靠
用户逐项抄页面，或从保存日志里拼凑。本模块把 ``/config/exec`` 的配置渲染
成一份可直接粘贴的 markdown。

设计约束：

* **字段列表从 ``PARAM_META`` / ``SCENE_FIELD_ORDER`` 自动生成**，不手写第二份
  ——新增参数自动出现，避免「页面有、快照漏」这类漂移。
* **只渲染配置值**，不查台账/持仓/资金（那些是「状态」不是「参数」）。
* **需要重启 TD 循环才生效的字段打 ⚠️**（见 ``RESTART_REQUIRED``）——这类字段
  改了页面立刻变但运行中的循环仍用旧值，是「页面显示 ≠ 实际执行」的高发区。
* 交易对解析尽力而为：``gate_pair`` 纯计算，OKX instId 走 600s 缓存的
  ``inst_id_for``，任何异常降级为 ``—``，绝不让快照生成失败。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .exec_params import (
    GROUP_TITLES,
    PARAM_META,
    RENDER_SKIP_GROUPS,
    SCENES,
    SCENE_FIELD_MAP,
    SCENE_FIELD_ORDER,
)

_CST = timezone(timedelta(hours=8))

#: 改了「不会」自动重启循环的字段：必须人工停止 → 重新激活才生效。
#: （scenes.* 改动会进 sync_from_params 的重启指纹，属自动重启，不列此处）
RESTART_REQUIRED: dict[str, str] = {
    "quote_ccy": "改后需停止 TD 循环再重新激活才生效（运行中的循环沿用旧计价币）",
}

_TRUTHY_LABEL = {True: "✅ 开", False: "⚪ 关"}


def _fmt(v: Any) -> str:
    """标量 → 展示文本（bool 用 ✅/⚪，None 用「跟随全局」，浮点去尾零）。"""
    if v is None:
        return "（跟随全局）"
    if isinstance(v, bool):
        return _TRUTHY_LABEL[v]
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if v else "（空）"
    if isinstance(v, float):
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _channels_of(meta: dict) -> str | None:
    """字段的 ``channels`` 限定值（dex/cex），无则 None。"""
    c = meta.get("channels")
    return str(c) if c else None


def _field_rows(group: str, params: dict, channel: str) -> list[tuple[str, str, str]]:
    """按 PARAM_META 生成 ``(label, value, note)`` 行。

    与页面同源：**不过滤掉其它通道的字段**（页面上也是全部渲染、前端按通道
    显隐，防切回丢 DOM），而是在说明列标注「仅 `xxx` 通道」。
    """
    rows: list[tuple[str, str, str]] = []
    for key, meta in PARAM_META.items():
        if str(meta.get("group")) != group:
            continue
        notes: list[str] = []
        ch = _channels_of(meta)
        if ch and ch != channel:
            notes.append(f"仅 `{ch}` 通道")
        if key in RESTART_REQUIRED:
            notes.append(f"⚠️ {RESTART_REQUIRED[key]}")
        enum_labels = meta.get("enum_labels") or {}
        val = params.get(key, meta.get("std"))
        if key in enum_labels and val in enum_labels:
            val_text = str(enum_labels[val])
        else:
            val_text = _fmt(val)
        rows.append((str(meta.get("label", key)), val_text, "；".join(notes)))
    return rows


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _spot_pairs(symbols: list[str], quote: str | None) -> dict[str, tuple[str, str]]:
    """尽力解析实盘交易对：``{symbol: (gate_pair, okx_inst_id)}``，失败给 ``—``。"""
    out: dict[str, tuple[str, str]] = {}
    try:
        from .gate_credentials import gate_pair  # noqa: PLC0415 — 延迟 import 避免循环
    except Exception:  # noqa: BLE001
        gate_pair = None  # type: ignore[assignment]
    try:
        from .okx_cex_data import inst_id_for  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        inst_id_for = None  # type: ignore[assignment]

    for sym in symbols:
        gp = "—"
        if gate_pair is not None:
            try:
                gp = gate_pair(sym) or "—"
            except Exception:  # noqa: BLE001
                gp = "—"
        ok = "—"
        if inst_id_for is not None:
            try:
                ok = inst_id_for(sym, quote=quote) or "—"
            except Exception:  # noqa: BLE001
                ok = "—"
        out[sym] = (gp, ok)
    return out


def render_exec_params_md(
    params: dict,
    *,
    now: datetime | None = None,
    pairs: dict[str, tuple[str, str]] | None = None,
) -> str:
    """把执行参数渲染成 markdown 文本（可直接粘贴）。

    ``pairs`` 可注入 ``{symbol: (gate_pair, okx_inst_id)}`` 以便测试零网络。
    """
    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cst = now.astimezone(_CST)

    channel = str(params.get("execution_channel") or "")
    try:
        from .brokers.registry import spec_for_channel  # noqa: PLC0415

        family = spec_for_channel(channel).family
    except Exception:  # noqa: BLE001 — 注册表不可用时退化成显式判断
        family = "cex" if channel in ("gate", "okx_cex", "cex") else "dex"

    lines: list[str] = []
    lines.append("# 执行参数快照")
    lines.append("")
    lines.append(
        f"- **生成时间**：{now.strftime('%Y-%m-%d %H:%M:%S')} UTC ｜ "
        f"{cst.strftime('%Y-%m-%d %H:%M:%S')} CST（UTC+8）"
    )
    lines.append(f"- **执行通道**：`{channel or '（未设置）'}`（family=`{family}`）")
    lines.append("- **口径**：本快照只含**配置值**（`exec_params.json`），不含台账/持仓/资金；"
                 "标 ⚠️ 的字段改后需重启 TD 循环才生效。")
    lines.append("")

    # ── 各参数分组（与页面分组同源：GROUP_TITLES 扣 RENDER_SKIP_GROUPS）──
    # scene 组走专用渲染（页面是多场景卡片），故在此插入三列对照表，
    # 保持与 GROUP_TITLES 相同的节序（① ② ③ ④ ⑤）。
    for group, title in GROUP_TITLES.items():
        if group == "scene":
            lines += _scene_block(params)
            continue
        if group in RENDER_SKIP_GROUPS:
            continue
        rows = _field_rows(group, params, channel)
        if not rows:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines += _table(["参数", "值", "说明"], [list(r) for r in rows])
        lines.append("")

    # ── 标的池 + 实盘交易对 ──
    symbols = [str(s) for s in (params.get("td_symbols") or [])]
    if symbols:
        quote = params.get("quote_ccy") if family == "cex" else None
        pmap = pairs if pairs is not None else _spot_pairs(symbols, quote)
        prows = []
        for sym in symbols:
            gp, ok = pmap.get(sym, ("—", "—"))
            prows.append([sym, f"`{gp}`", f"`{ok}`"])
        lines.append("## ⑥ 标的池（实盘交易对）")
        lines.append("")
        lines += _table(["标的", "Gate 交易对", f"OKX 交易对（{quote or 'USDT'}）"], prows)
        lines.append("")
        lines.append("> 交易对一律调执行期同一批函数（`gate_pair()` / `inst_id_for()`），"
                     "与代币页同源；解析失败显示 `—`。")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _scene_block(params: dict) -> list[str]:
    """多场景三列对照表（一眼看出「场景覆盖」还是「跟随全局」）。"""
    scenes = params.get("scenes") or {}
    scene_keys = list(SCENES)
    header = ["场景参数"] + [
        f"{SCENES[k]['label']}（{SCENES[k]['freq']}）" for k in scene_keys
    ]
    srows: list[list[str]] = []
    for fkey in SCENE_FIELD_ORDER:
        label = _scene_field_label(fkey)
        vals = [_fmt((scenes.get(sk) or {}).get(fkey)) for sk in scene_keys]
        srows.append([label] + vals)
    out = [f"## {GROUP_TITLES.get('scene', '多场景（多时间框架）')}", ""]
    out += _table(header, srows)
    out += ["", "> 「（跟随全局）」= 该场景未填值，运行时回退全局/类默认。", ""]
    return out


def _scene_field_label(fkey: str) -> str:
    """场景字段 → 中文标签（优先 PARAM_META.label，其次场景专属映射）。"""
    meta = PARAM_META.get(fkey)
    if meta and meta.get("label"):
        return str(meta["label"])
    flat = SCENE_FIELD_MAP.get(fkey)
    if flat and PARAM_META.get(flat, {}).get("label"):
        return str(PARAM_META[flat]["label"])
    return fkey
