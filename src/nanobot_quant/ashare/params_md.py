"""A 股页面参数快照（Markdown）——「📋 复制参数」的唯一实现。

用途：把 ``/config/ashare`` 上**当前生效的参数**渲染成一段可直接粘贴的 Markdown，
给助手/他人核对配置用（同 ``/config/exec`` 的「📋 复制参数 Markdown」）。

设计约束（durable）：

- **单一来源**：所有数值一律从运行模块现读（``tape.tape_config()`` /
  ``paper_runner.load_config()``），本模块**不保存任何默认值副本**；
  与默认值不同的项自动标 ``⚠️（默认 X）``，默认值本身取自各自的
  ``DEFAULT_TAPE`` / ``default_params()``，同样不重复定义。
- **只读**：不写文件、不改配置、不触发 IV 重算（只读缓存态，见
  ``iv_panel.cached_state``）——复制参数不应让采集/账本产生任何副作用。
- 新增字段若漏了标签，``tests/test_ashare_params_md.py`` 会红（标签表必须覆盖
  全部 ``PaperParams`` 字段），避免快照静默少行。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import iv_panel as ivp
from . import paper_ledger as pl
from . import paper_runner as pr
from . import paper_strategy as ps
from . import tape
from . import trades_md as tm

CST = timezone(timedelta(hours=8))

# ── 展示文案（只放标签，不放数值）────────────────────────────
TAPE_LABELS = {
    "enabled": "启用采集",
    "interval_s": "采样间隔（秒）",
    "underlyings": "标的池",
    "expiries": "到期档数",
    "band_pct": "虚值带（±%）",
    "max_per_side": "每档每方向上限",
}

PAPER_BASE_LABELS = {
    "enabled": "启用模拟循环",
    "interval_s": "心跳（秒）",
    "symbols": "标的池",
    "initial_cash": "初始资金（元）",
    "second_arm": "臂 B（D 方案：高9 + 浮盈）",
}

# PaperParams 分组与中文名；必须覆盖全部字段（有测试锁定）
PARAM_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("选档", [
        ("min_dte", "最短到期（天）"),
        ("max_dte", "最长到期（天）"),
        ("min_distance_pct", "最小虚值距离（%）"),
        ("band_pct", "选档虚值带（±%）"),
    ]),
    ("张数", [
        ("max_per_underlying", "单标的最大张数"),
        ("max_total", "总张数上限"),
        ("max_per_wave", "每浪每标的上限（0=关）"),
    ]),
    ("出场", [
        ("take_profit_pct", "止盈（权利金回落 %）"),
        ("force_close_dte", "DTE 强平阈值（天）"),
    ]),
    ("费用", [
        ("fee_open", "开仓手续费（元/张）"),
        ("mult", "合约面值（份/张）"),
    ]),
]

NOTES = [
    "资金占用按 **100% 全损担保**（行权价 × 面值 × 张数），不按平台 7–12% 保证金——否则资金效率虚高。",
    "成交价 **盘口优先、bar 近似兜底**（取不到真实 bid/ask 才回退「bar 收盘 ± 1 tick」）。",
    f"手续费：卖出开仓 **{pl.FEE_OPEN_PER_CONTRACT} 元/张**、买入平仓 **{pl.FEE_CLOSE_PER_CONTRACT} 元/张**。",
    "出场主路径 = **DTE 强平**（宁可少赚不接货）；到期接货仅为兜底，与「不设止损」铁律一致（浮亏不主动平）。",
    "IV 口径：**中价 + put-call parity 隐含远期 F** 自算，不用新浪行内 IV、也不用现货 spot；HV 用日线收盘（禁用 1m RV）。",
    "定位：模拟账本（不接券商），不与实盘持仓对齐，也不作实盘验收证据。",
]


# ── 小工具 ────────────────────────────────────────────────────
def _num(v: Any) -> str:
    if isinstance(v, bool):
        return "开" if v else "关"
    if isinstance(v, float):
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s or "0"
    return str(v)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _cell(val: Any, default: Any = None) -> str:
    """值 + 与默认值不同时标注（默认值来自各自模块，不在此重复定义）。"""
    s = _num(val)
    if default is not None and not _same(val, default):
        return f"{s} ⚠️（默认 {_num(default)}）"
    return s


def _cst(ts: Any) -> str:
    """把 ISO 串或 epoch 秒转成北京时间文本；解析不了原样返回。"""
    if ts in (None, ""):
        return "—"
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 — 展示层不因时间解析失败而报错
        return str(ts)


def _codes(codes: Any, pool: dict[str, str]) -> str:
    if isinstance(codes, str):
        codes = [codes]
    if not isinstance(codes, (list, tuple)):
        return _num(codes)
    return "、".join(f"{c}（{pool.get(str(c), '?')}）" for c in codes) or "—"


def _today_rows() -> int | None:
    """今日 tape 落盘行数（跨重启累计）——内存计数器重建后归零，不能当「今日样本」。"""
    try:
        p = tape.sample_path()
        if not p.exists():
            return 0
        n = 0
        with open(p, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                n += chunk.count(b"\n")
        return n
    except Exception:  # noqa: BLE001 — 统计失败不影响快照
        return None


def _runner_line(label: str, st: dict, extra: str = "") -> str:
    """运行态行。注意两种状态形态：\n
    ``LiveRunnerBase.status()`` 给 ``thread_alive``，而 ``tape.state()`` 给 ``running``——
    读错键会把「在跑」渲染成「未运行」（假阴性），故两键兼容。
    """
    alive = bool(st.get("thread_alive") or st.get("running"))
    parts = ["🟢 运行中" if alive else "⚪ 未运行",
             f"上次 {_cst(st.get('last_run'))}"]
    if extra:
        parts.append(extra)
    totals = st.get("totals") or {}
    kv = "、".join(f"{k}={v}" for k, v in totals.items() if v)
    if kv:
        parts.append(kv)
    err = st.get("last_error") or ""
    if err:
        parts.append(f"⚠️ {err}")
    return f"| {label} | " + " · ".join(parts) + " |"


def _codes_cell(val: Any, default: Any, pool: dict[str, str]) -> str:
    """列表类参数（标的池）：带中文名，与默认值不同时标出。"""
    s = _codes(val, pool)
    if default is None:
        return s
    if isinstance(val, str) or isinstance(default, str):
        same = str(val) == str(default)
    elif isinstance(val, (list, tuple)) and isinstance(default, (list, tuple)):
        same = [str(x) for x in val] == [str(x) for x in default]
    else:
        same = _same(val, default)
    if same:
        return s
    return f"{s} ⚠️（默认 {_codes(default, pool)}）"


# ── 渲染 ─────────────────────────────────────────────────────
def build(undl: str | None = None) -> str:
    """渲染参数快照 Markdown（只读）。"""
    now = datetime.now(CST)
    tcfg = tape.tape_config()
    tdef = getattr(tape, "DEFAULT_TAPE", {}) or {}
    tstate = tape.state()
    pcfg = pr.load_config()
    pdef = pr.default_config()
    pparams = pcfg.get("params") or {}
    pdefaults = pr.default_params()
    pool = dict(tape.POOL_UNDERLYINGS)

    src = ivp.find_option_dir()
    iv_state = ivp.cached_state(undl or (tcfg.get("underlyings") or list(pool))[0])
    try:
        tape_dir = str(tape.tape_dir())
    except Exception:  # noqa: BLE001
        tape_dir = "—"

    out: list[str] = []
    out.append("# A 股 ETF 期权 · 页面参数快照")
    out.append("")
    out.append(f"- 生成时间：**{now:%Y-%m-%d %H:%M:%S} CST**（{now.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC）")
    out.append("- 页面：`/config/ashare`（模拟账本看板 · 只读视图）")
    out.append(f"- 期权 1m 分片：`{src}`" if src else "- 期权 1m 分片：`（未找到 qmt-option-1m/run_*）`")
    out.append(f"- 盘口落盘：`{tape_dir}/tape_YYYYMMDD.jsonl`")
    out.append("")
    out.append("> 数值 = 当前生效值；标 `⚠️（默认 X）` 的项与模块默认值不同。参数只读自 `tape` / `paper_runner`，本快照不改任何配置。")
    out.append("")

    # 运行态
    out.append("## 运行态（读取时刻）")
    out.append("")
    out.append("| 项 | 状态 |")
    out.append("|:---|:---|")
    rows_today = _today_rows()
    tape_extra = (f"本次进程 samples={int(tstate.get('total_samples') or 0)}"
                  f" · rows={int(tstate.get('total_rows') or 0)}")
    if rows_today is not None:
        tape_extra += f" · 今日落盘 {rows_today} 条"
    tape_extra += f" · {'交易时段内' if tstate.get('in_session') else '非交易时段'}"
    out.append(_runner_line("盘口采集（tape）", tstate, tape_extra))
    out.append(_runner_line("模拟账本（paper）", pr.runner_state()))
    iv_bits = [f"状态 `{iv_state.get('status') or 'idle'}`"]
    if iv_state.get("asof"):
        iv_bits.append(f"数据截至 {iv_state['asof']}")
    if iv_state.get("run"):
        iv_bits.append(f"run `{iv_state['run']}`")
    if iv_state.get("error"):
        iv_bits.append(f"⚠️ {iv_state['error']}")
    out.append("| IV 分位缓存 | " + " · ".join(iv_bits) + " |")
    out.append("")

    # ① 盘口采集
    out.append("## ① 盘口采集（tape）")
    out.append("")
    out.append("| 参数 | 生效值 |")
    out.append("|:---|:---|")
    for k, label in TAPE_LABELS.items():
        val = tcfg.get(k)
        if k in ("underlyings", "symbols"):
            out.append(f"| {label} | {_codes_cell(val, tdef.get(k), pool)} |")
        else:
            out.append(f"| {label} | {_cell(val, tdef.get(k))} |")
    out.append("")

    # ② 模拟账本基础
    out.append("## ② 模拟账本（基础）")
    out.append("")
    out.append("| 参数 | 生效值 |")
    out.append("|:---|:---|")
    for k, label in PAPER_BASE_LABELS.items():
        val = pcfg.get(k)
        if k in ("underlyings", "symbols"):
            out.append(f"| {label} | {_codes_cell(val, pdef.get(k), pool)} |")
        else:
            out.append(f"| {label} | {_cell(val, pdef.get(k))} |")
    out.append("")

    # ③ 账本规则（PaperParams）
    out.append("## ③ 账本规则（入场 / 张数 / 出场 / 费用）")
    out.append("")
    out.append("| 分组 | 参数 | 生效值 |")
    out.append("|:---|:---|:---|")
    for group, items in PARAM_GROUPS:
        for key, label in items:
            out.append(f"| {group} | {label} | {_cell(pparams.get(key), pdefaults.get(key))} |")
    out.append("")

    # ④ IV 口径
    out.append("## ④ IV 分位口径（只读）")
    out.append("")
    out.append("| 项 | 值 |")
    out.append("|:---|:---|")
    bands = " / ".join(
        (f"{lo}–{hi}d" if hi < 400 else f"{lo}d+") for lo, hi in ivp.BUCKETS)
    out.append(f"| 分位分桶 | {bands}（ATM put，按日取收盘值） |")
    out.append("| 常数期限 | 30d（相邻到期线性插值，不外推；短档缺失时标 nearest） |")
    out.append(f"| 缓存 TTL | {ivp.CACHE_TTL_S} 秒 |")
    out.append("| 分位窗口 | 全历史（`min_days=1`，样本不足则显式标注） |")
    out.append(f"| 数据 run | `{iv_state.get('run') or '—'}` |")
    out.append("")

    # ⑤ 账本摘要（不带明细——成交体量会涨，明细走「📋 复制成交」）
    out.append("## ⑤ 账本摘要（成交/持仓明细见「📋 复制成交」）")
    out.append("")
    try:
        payload = pr.state(limit_fills=pr.FILLS_KEEP)
        out.extend(tm.summary_table(payload))
        if payload.get("error"):
            out.append(f"| ⚠️ 状态读取 | {payload['error']} |")
    except Exception as e:  # noqa: BLE001 — 账本读不到也要出参数快照
        out.append(f"（账本读取失败：{type(e).__name__}: {e}）")
    out.append("")

    # ⑥ 口径注记
    out.append("## ⑥ 口径注记")
    out.append("")
    for n in NOTES:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)


__all__ = ["build", "TAPE_LABELS", "PAPER_BASE_LABELS", "PARAM_GROUPS", "NOTES"]
