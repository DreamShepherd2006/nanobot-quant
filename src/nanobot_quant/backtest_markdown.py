"""把回测结果 dict 渲染成 markdown —— 供页面复制 / MCP 返回 / 贴聊天共用。

设计要点：

* **纯函数、零依赖、无副作用** —— 可以直接在单测里断言输出，不需要起服务。
* 输入是 driver 落盘在 ``{data_root}/legion/backtests/<run_id>.json`` 的 result dict，
  两种形态都吃：现货 ``backtest/driver.py``（含 ``scene``）与期权
  ``backtest/options_driver.py``（含 ``family``）。
* **绝不抛异常**：markdown 只是「方便拷贝」的 UX 增强，任何字段缺失/形态意外
  都必须降级成空串，不能影响结果接口本身的返回。
* 运行中（``status=running``）与失败（``error``）返回空串 —— 这两种载荷没有可
  拷贝的完整结论。
* ``notes`` 里的逐日归档清单（期权回测有 31 行 ``[n/N] 日期 → 文件名``）压缩成
  一行摘要，否则拷贝出来的 markdown 大半是下载日志。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

__all__ = ["render_markdown"]

_TZ_LOCAL = "Asia/Shanghai"


# ── 基础工具 ──────────────────────────────────────────────────────────

def _esc(v: Any) -> str:
    """表格单元格转义：竖线会截断 markdown 表格，换行会破坏行结构。"""
    if v is None:
        return "—"
    s = str(v)
    if s == "":
        return "—"
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _num(v: Any, nd: int = 4, suffix: str = "") -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return _esc(v)


def _pct(v: Any, nd: int = 2) -> str:
    """百分比显示。缺值返回 ``—`` 而不是 ``—%``（调用方可能传 None）。"""
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.{nd}f}%"
    except (TypeError, ValueError):
        return _esc(v)


def _iv(v: Any) -> str:
    """IV 内部是小数（0.794 = 79.4%），与页面显示口径对齐。"""
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return _esc(v)


def _intish(v: Any) -> str:
    """88.0 → 88，88.5 → 88.5。Strike 这类不该带 ``.0`` 尾巴。"""
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _spot_ref(res: dict) -> str:
    """``SOL-USDT（$100.79 → $106.53）`` —— 只给标的代码看不出价格水平。"""
    ref = _esc(res.get("ref_inst") or "—")
    rng = res.get("spot_range")
    if not isinstance(rng, dict):
        return ref
    first, last = rng.get("first"), rng.get("last")
    if first is None or last is None:
        return ref
    return f"{ref}（${first:g} → ${last:g}）"


def _spot_extremes(res: dict) -> str:
    rng = res.get("spot_range")
    if not isinstance(rng, dict):
        return "—"
    hi, lo = rng.get("high"), rng.get("low")
    if hi is None or lo is None:
        return "—"
    return f"最高 ${hi:g} / 最低 ${lo:g}"


def _max_contracts_txt(res: dict) -> str:
    """生效张数上限 —— 单家族 / 跨家族取小者。

    两个值都写出来，「页面填 10 却只开了 3」这类闷棍才能一眼看出被谁卡住。
    """
    mc = res.get("max_contracts")
    if not isinstance(mc, dict) or mc.get("effective") is None:
        return "—"
    per = mc.get("per_family")
    tot = mc.get("total")
    per_s = "—" if per is None else str(per)
    tot_s = "—" if tot is None else str(tot)
    return f"{mc['effective']} 张（单家族 {per_s} / 全局 {tot_s}，取小值）"


def _ts_local(v: Any) -> str:
    """ISO 时间串 → 本地（Asia/Shanghai）``YYYY/M/D HH:MM:SS``，与页面显示一致。

    解析不了就原样返回 —— 页面能显示的值这里也必须能显示。
    """
    if not v:
        return "—"
    s = str(v)
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(_TZ_LOCAL)).strftime("%Y/%-m/%-d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return _esc(s)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_（无）_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([":--"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_esc(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _kv_table(pairs: list[tuple[str, Any]]) -> str:
    return _table(["项", "值"], [[k, v] for k, v in pairs])


_ARCHIVE_RE = re.compile(r"^\[\d+/\d+\]\s+\S+\s+→\s+(\S+)\s+\(([\d.]+)\s*MB\)\s*$")
# 原始 notes 里的归档汇总行（我们自己会生成等效摘要，保留会重复）
_ARCHIVE_SUMMARY_RE = re.compile(r"^归档共\s*\d+\s*天")


# SKIP 原因键 → 展示名。与 ``backtest_page.html`` 的 ``SKIP_LABELS`` 保持同步。
# 现货 driver 产出英文键（cd_stale / batch_wait ...）；期权侧 ``_count_skips``
# 产出的已是中文前缀（张数上限 / 周期门控 ...）—— 查不到映射就原样输出，
# 一份表兼容两种来源。
_SKIP_LABELS = {
    "cd_stale": "CD 陈旧拦截（时效门）",
    "sell_profit_gate": "高9 盈利门拦",
    "sell_min_hold": "高9 min_hold 拦",
    "sell_no_open": "高9 无持仓",
    "cd_exit_profit_gate": "cd13 保本门拦",
    "cd_exit_min_hold": "cd13 min_hold 拦",
    "cd_exit_no_open": "cd13 无持仓",
    "batch_wait": "周期门控/波锁等待",
    "gate_blocked": "贝叶斯闸门拦截",
    "gate_na": "闸门数据不足(NA)",
}


def _skip_rows(skips: dict) -> list[list[Any]]:
    return [[_SKIP_LABELS.get(str(k), str(k)), v] for k, v in (skips or {}).items()]


def _compress_notes(notes: list[Any]) -> list[str]:
    """压缩 notes：把期权回测的逐日归档清单折叠成一行摘要。"""
    if not notes:
        return []
    kept: list[str] = []
    days: list[str] = []
    total_mb = 0.0
    for n in notes:
        s = str(n).strip()
        m = _ARCHIVE_RE.match(s)
        if m:
            fname, mb = m.group(1), float(m.group(2))
            d = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
            if d:
                days.append(d.group(1))
            total_mb += mb
            continue
        if s:
            kept.append(s)
    kept = [k for k in kept if not _ARCHIVE_SUMMARY_RE.match(k)]
    if days:
        kept.insert(
            0,
            f"归档 {len(days)} 天：{min(days)} ~ {max(days)}（{total_mb:.1f} MB）",
        )
    return kept


# ── 期权回测 ──────────────────────────────────────────────────────────

_SIDE_LABEL = {
    "sell_open": "🟠 卖出开仓",
    "close": "🔵 买回平仓",
    "buy_close": "🔵 买回平仓",
    "settle_otm": "✅ 到期作废",
    "settle_itm": "⚠️ 被行权",
}


def _opt_side(f: dict) -> str:
    raw = str(f.get("side") or "")
    return _SIDE_LABEL.get(raw, raw or "—")


def _options_md(res: dict) -> str:
    kpi = res.get("kpi") or {}
    bars = res.get("bars") or {}
    contracts = res.get("contracts") or {}
    fills = res.get("fills") or []
    final_positions = res.get("final_positions") or []
    skips = res.get("skips") or {}

    md: list[str] = [f"## 🟤 期权回测 {res.get('run_id', '')}\n"]

    md.append("**📋 回测参数（实际生效）**\n")
    md.append(_kv_table([
        ("标的家族", res.get("family")),
        ("周期", f"{res.get('timestep')}（bar={res.get('bar')}）"),
        ("参考现货", _spot_ref(res)),
        ("现货区间", _spot_extremes(res)),
        ("区间", f"{_ts_local(res.get('start_ts'))} → {_ts_local(res.get('end_ts'))}"),
        ("评估 bar", f"{bars.get('evaluated', 0)} / 拉取 {bars.get('fetched', 0)}"),
        ("初始资金", f"${_num(res.get('initial_cash'), 2)}"),
        ("TD 窗口", f"{res.get('td_bars')} bars"),
        ("张数上限", _max_contracts_txt(res)),
        ("滑点", _pct(res.get('slippage_pct'))),
        # 与回测页同序、同口径（页面上「价差模型」行印的是 spread_model_note）
        ("价差模型", res.get('spread_model_note') or res.get('spread_model') or "—"),
        ("手续费率", _pct((res.get('fee_rate') or 0) * 100)),
        ("止盈线", _pct(res.get('tp_pct'), 0)),
        ("合约总数", f"{contracts.get('in_archive', 0)} 档 · 有 IV {contracts.get('with_iv', 0)}"),
    ]))

    md.append("\n**📊 结果**\n")
    md.append(_kv_table([
        ("期末净值", f"${_num(kpi.get('final_net_usd'), 4)}"),
        ("ROI", _pct(kpi.get('roi_pct'), 4)),
        ("权利金收入（毛）", f"${_num(kpi.get('premium_income_usd'), 4)}"),
        ("买回支出", f"${_num(kpi.get('buyback_cost_usd'), 4)}"),
        ("赔付支出", f"${_num(kpi.get('payout_usd'), 4)}"),
        ("手续费合计", f"${_num(kpi.get('fees_usd'), 4)}"),
        ("净交易损益（毛权利金−买回−赔付−手续费）", f"${_num(kpi.get('net_trading_usd'), 4)}"),
        ("未平仓权利金", f"${_num(kpi.get('open_premium_usd'), 4)}"),
        ("期末持仓市值", f"${_num(kpi.get('open_mark_value_usd'), 4)}（负债，已从净值扣减）"),
        ("成交笔数", f"{kpi.get('fills', 0)}（盈利 {kpi.get('wins', 0)} / 亏损 {kpi.get('losses', 0)}）"),
        ("用时", f"{_num(res.get('elapsed_s'), 1)}s"),
    ]))

    md.append(f"\n**📒 成交明细（{len(fills)}）**\n")
    rows = []
    for f in fills:
        rows.append([
            _ts_local(f.get("ts")), f.get("inst_id", ""), _opt_side(f),
            f.get("sz", ""), _intish(f.get("strike")), _num(f.get("spot"), 2),
            _num(f.get("avg_px") or f.get("strategy_px"), 6),
            _num(f.get("pnl_usd"), 4),
            _iv(f.get("iv")),
            _num(f.get("delta"), 4),
            f.get("reason", ""),
        ])
    md.append(_table(
        ["时间", "合约", "方向", "张数", "Strike", "现货价", "价（每名义币）", "盈亏", "IV", "Delta", "原因"],
        rows,
    ))

    md.append(f"\n**📌 期末未平仓（{len(final_positions)}）**\n")
    md.append(_table(
        ["合约", "Strike", "张数", "开仓价", "期末 mark", "担保额", "浮盈%"],
        [[p.get("inst_id", ""), _intish(p.get("strike")), p.get("sz", ""),
          _num(p.get("entry_px"), 6), _num(p.get("mark_px"), 6),
          f"${_num(p.get('collateral_usd'), 2)}", _pct(p.get("pnl_pct"))]
         for p in final_positions],
    ))

    md.append("\n**🚦 SKIP 统计**\n")
    rows = _skip_rows(skips)
    md.append(_table(["原因", "次数"], rows) if rows else "_（无）_\n")

    notes = _compress_notes(res.get("notes") or [])
    if notes:
        md.append("\n**📝 运行备注**\n")
        md.append("\n".join(f"- {n}" for n in notes) + "\n")

    return "".join(md)


# ── 现货（TD 场景）回测 ────────────────────────────────────────────────

_SIDE_SPOT = {"buy": "🟢 买入", "sell": "🔴 卖出"}


def _spot_md(res: dict) -> str:
    cfg = res.get("backtest_config") or {}
    cap = res.get("capital_stats") or {}
    detail = res.get("fills_detail") or []
    slots = res.get("slots") or {}
    open_slots = slots.get("open") if isinstance(slots, dict) else None
    open_slots = open_slots or []
    skips = res.get("skip_counts") or {}

    md: list[str] = [f"## 📈 现货回测 {res.get('run_id', '')}\n"]

    md.append("**📋 回测参数（实际生效）**\n")
    md.append(_kv_table([
        ("场景", res.get("scene")),
        ("标的", ", ".join(res.get("symbols") or [])),
        ("周期", res.get("timestep")),
        ("区间", f"{_ts_local(res.get('start_ts'))} → {_ts_local(res.get('end_ts'))}"),
        ("评估 bar", f"{res.get('bars', 0)} / 拉取 {res.get('fetched_bars', 0)}"),
        ("初始资金", f"{_num(res.get('initial_total'), 2)}（每 slot {_num(res.get('initial_quote'), 2)} × {res.get('batches', '—')}）"),
        ("批次", res.get("batches")),
        ("入场 setup", cfg.get("entry_setup")),
        ("入场 countdown", cfg.get("entry_countdown")),
        ("出场 setup", cfg.get("exit_setup")),
        ("出场 countdown", cfg.get("exit_countdown")),
        ("最短持有期", cfg.get("min_hold_bars")),
        ("止损", _pct((cfg.get("stop_loss_pct") or 0) * 100)),
        ("止盈", _pct((cfg.get("take_profit_pct") or 0) * 100)),
        ("滑点", _pct(cfg.get("slippage"))),
        ("手续费率", _pct((cfg.get("fee_rate") or 0) * 100)),
    ]))

    md.append("\n**📊 结果**\n")
    md.append(_kv_table([
        ("期末净值", _num(res.get("final_net"), 4)),
        ("ROI", _pct((res.get("roi") or 0) * 100, 4)),
        ("成交笔数", res.get("fills", 0)),
        ("单边周转率", _num(cap.get("turnover"), 2)),
        ("双边周转率", _num(cap.get("turnover_two_side"), 2)),
        ("平均资金利用率", _pct((cap.get("utilization") or 0) * 100)),
        ("总资金", _num(cap.get("total_funds"), 2)),
    ]))

    md.append(f"\n**📒 成交明细（{len(detail)}）**\n")
    md.append(_table(
        ["时间", "标的", "方向", "数量", "策略价", "成交价", "slot", "状态", "原因"],
        [[_ts_local(f.get("ts")), f.get("symbol", ""),
          _SIDE_SPOT.get(str(f.get("side") or ""), f.get("side", "")),
          _num(f.get("quantity"), 6), _num(f.get("strategy_price"), 6),
          _num(f.get("avg_price"), 6), f.get("slot", ""),
          f.get("state", ""), f.get("reason", "")] for f in detail],
    ))

    md.append(f"\n**📌 期末未平仓（{len(open_slots)}）**\n")
    md.append(_table(
        ["标的", "slot", "数量", "成本价", "期末价", "浮盈%"],
        [[s.get("symbol", ""), s.get("slot", ""), _num(s.get("qty"), 6),
          _num(s.get("entry_price"), 6), _num(s.get("final_price"), 6),
          _num(s.get("pnl_pct"), 2, "%")] for s in open_slots],
    ))

    md.append("\n**🚦 SKIP 统计**\n")
    rows = _skip_rows(skips)
    md.append(_table(["原因", "次数"], rows) if rows else "_（无）_\n")

    notes = _compress_notes(res.get("notes") or [])
    if notes:
        md.append("\n**📝 运行备注**\n")
        md.append("\n".join(f"- {n}" for n in notes) + "\n")

    return "".join(md)


# ── 入口 ──────────────────────────────────────────────────────────────

def render_markdown(result: Any) -> str:
    """回测结果 → markdown。不可渲染时返回空串，绝不抛异常。"""
    try:
        if not isinstance(result, dict):
            return ""
        if result.get("error") or result.get("status") == "running":
            return ""
        if "family" in result:
            return _options_md(result)
        if "scene" in result:
            return _spot_md(result)
        return ""
    except Exception:  # noqa: BLE001
        return ""
