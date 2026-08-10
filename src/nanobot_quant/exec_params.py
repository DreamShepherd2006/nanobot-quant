"""On-chain execution parameters — defaults, storage & validation.

The WebUI page ``/config/exec`` edits these parameters and persists them
to ``{data_root}/credentials/exec_params.json`` (next to ``live.json``).
Every consumer (pipeline, execute_signal, broker) reloads the latest file
on each call, so changes take effect immediately — no restart required.

Control-plane design (2026-08-08):

- These parameters are SYSTEM-LEVEL policy: they are locked by the
  WebUI and are NOT exposed through MCP (LLM cannot pass them in
  execute_signal).  Only portfolio_value / quantity (call-level sizing)
  stay in the MCP schema.
- ``max_position_pct`` is enforced live by RiskEngine on every order.
- ``slippage`` / ``sol_buffer_pct`` are passed to OnchainOSBroker for
  actual swap execution.
- ``max_drawdown_pct`` / ``stop_loss_pct`` are effective in backtest and
  paper trading today; on the execute_signal path they are formal checks
  (no position context yet) — the parameters are configured here so a
  future position-context integration picks them up automatically.
- ``td_*`` / ``quantity_mode`` drive the TD autonomous StrategyExecutor
  loop (P2 B3): ``td_enabled`` is the WebUI on/off switch.

P1 loop mode (execution_mode / loop_interval_seconds) was retired in B3:
execute_signal is synchronous only (direct).

Missing / invalid file → DEFAULT_EXEC_PARAMS, which is byte-for-byte
identical to the pre-parameterisation hardcoded behaviour (20% position
limit, 15% drawdown, 10% stop-loss, 1% slippage, 5% SOL buffer).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Schema / defaults ────────────────────────────────────────────────────

#: Full execution parameter set. Defaults == old hardcoded values.
DEFAULT_EXEC_PARAMS: dict[str, Any] = {
    # ── ① Risk control ───────────────────────────────────────────────
    "max_position_pct": 0.20,   # float (0,1] — single-order value ≤ pv × pct
    "max_drawdown_pct": 0.15,   # float (0,1] — account drawdown threshold
    "stop_loss_pct": 0.10,      # float (0,1] — per-position stop-loss
    # ── ② Execution quality ──────────────────────────────────────────
    "slippage": 0.01,           # float [0,1) — swap slippage tolerance (0.01 = 1%)
    "sol_buffer_pct": 0.05,     # float [0,1) — extra SOL reserved on buys
    # ── ③ TD 自主运行（P2 B2/B3, StrategyExecutor 主循环）─────────────
    "td_enabled": False,        # WebUI 开关：TD 自主 live 循环启停
    "td_symbols": ["SOL"],     # TD 标的池（多标的扫描，谁 Setup 9 谁执行；
                                #   /config/tokens 登记代币 symbol，稳定币不列入）
    "td_sleeptime": "1D",      # 主循环周期（对应 lumibot sleeptime + K 线粒度）
    "quantity_mode": "fixed",  # fixed=固定 td_quantity；value=portfolio_value × max_position_pct
    "td_quantity": 10,          # int ≥1 — quantity_mode=fixed 时的下单数量
    "td_bars": 120,             # int 20-300 — TD 每轮拉取最近 N 根 K 线（固定窗口）
    # ── ④ 子钱包分批（批次=子钱包，真分账 v1.1，2026-08-10）─────────
    "td_batches": 1,            # int 1-50 — 批次/子钱包数量；1=单仓模式（现状）
    "exit_order": "fifo",      # fifo=先买先卖（默认）/ lifo=后买先卖
    "take_profit_pct": 0.0,     # 止盈线（%）；0=关闭（纯 TD SELL + 止损）
    "td_start_slot": 1,          # int 1-50 — BUY 扫描起点（完整循环 + 起点偏移）
    "min_account_value": 0,    # float ≥0 — BUY 门槛：目标 slot 子钱包总资产低于该值则跳过（0=关闭）
}

#: Valid TD main-loop cadences (lumibot sleeptime strings).
TD_SLEEPTIMES: tuple[str, ...] = ("1m", "5m", "15m", "1H", "1D", "1W")

#: Valid position-sizing modes for the TD autonomous strategy.
QUANTITY_MODES: tuple[str, ...] = ("fixed", "value")

#: Valid batch exit orders.
EXIT_ORDERS: tuple[str, ...] = ("fifo", "lifo")

#: Human-readable bounds used by the WebUI form validation + display.
PARAM_META: dict[str, dict[str, Any]] = {
    "max_position_pct": {
        "group": "risk", "min": 0.01, "max": 1.0, "step": 0.05, "std": 0.20,
        "label": "单仓上限", "hint": "单笔订单价值 ≤ 组合 × 该比例（实盘真实生效）",
    },
    "max_drawdown_pct": {
        "group": "risk", "min": 0.01, "max": 1.0, "step": 0.05, "std": 0.15,
        "label": "回撤阈值", "hint": "组合净值从峰值回撤超限触发风控（回测/纸交易生效；实盘待持仓上下文）",
    },
    "stop_loss_pct": {
        "group": "risk", "min": 0.01, "max": 1.0, "step": 0.05, "std": 0.10,
        "label": "止损阈值", "hint": "持仓从入场价跌超限强制平仓（回测/纸交易生效；实盘待持仓上下文）",
    },
    "slippage": {
        "group": "exec", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.01,
        "label": "滑点容忍", "hint": "swap 滑点容忍（0.01=1%）；过小易滑点超限失败（82112），过大成交价劣",
    },
    "sol_buffer_pct": {
        "group": "exec", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.05,
        "label": "SOL 缓冲", "hint": "BUY 时按比例预留 SOL 覆盖 gas 与报价-成交间价格波动",
    },
    "td_enabled": {
        "group": "td", "type": "bool", "std": False,
        "label": "TD 自主运行", "hint": "开启后 TD 自主策略在 quant agent 进程内驻留 StrategyExecutor 主循环（标的/周期/数量见下）",
    },
    "td_symbols": {
        "group": "td", "type": "list", "std": ["SOL"],
        "label": "TD 标的池", "hint": "多标的扫描：每轮遍历池子算 TD，谁 Setup 9 谁执行（同 bar 按池子顺序全部处理）。从 /config/tokens 登记代币选（SOL 登记后可选，稳定币不列入）",
    },
    "td_sleeptime": {
        "group": "td", "type": "enum", "enum": list(TD_SLEEPTIMES), "std": "1D",
        "label": "TD 周期", "hint": "主循环周期 = lumibot sleeptime 与 K 线粒度（1D 默认）",
    },
    "quantity_mode": {
        "group": "td", "type": "enum", "enum": list(QUANTITY_MODES), "std": "fixed",
        "label": "数量模式", "hint": "fixed=固定 td_quantity（默认 10，回测语义不变）；value=按实时 portfolio_value × 单仓上限计算",
    },
    "td_quantity": {
        "group": "td", "min": 1, "max": 100000, "step": 1, "std": 10, "integer": True,
        "label": "TD 固定数量", "hint": "quantity_mode=fixed 时的下单数量（默认 10）",
    },
    "td_bars": {
        "group": "td", "min": 20, "max": 300, "step": 1, "std": 120, "integer": True,
        "label": "K 线窗口", "hint": "TD 每轮拉取最近 N 根 K 线（固定窗口，不累积增长；300 = onchainos CLI 单次上限）。与分析页 K 线数设一致可完全对照",
    },
    # ── ④ 子钱包分批 ──────────────────────────────────────────────────
    "td_batches": {
        "group": "batch", "min": 1, "max": 50, "step": 1, "std": 1, "integer": True,
        "label": "批次数量（子钱包）", "hint": "1=单仓模式（现状）；>1 时每批绑定一个 Agentic Wallet 子钱包，保存后自动创建不足的子钱包并建立映射",
    },
    "exit_order": {
        "group": "batch", "type": "enum", "enum": list(EXIT_ORDERS), "std": "fifo",
        "label": "平仓顺序", "hint": "TD SELL 信号/止损/止盈命中多批时按此顺序平仓：fifo=先买先卖（默认）/ lifo=后买先卖",
    },
    "take_profit_pct": {
        "group": "batch", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.0,
        "label": "止盈线", "hint": "每批浮盈 ≥ 该值即平仓（0=关闭，纯 TD SELL + 止损；如 0.05 = 5%）",
    },
    "td_start_slot": {
        "group": "batch", "min": 1, "max": 50, "step": 1, "std": 1, "integer": True,
        "label": "建仓起始批次", "hint": "BUY 从该 slot 开始扫描（完整循环 + 起点偏移；设 3 → 3→4→5→1→2；资金不足自动跳下一 slot）",
    },
    "min_account_value": {
        "group": "batch", "min": 0, "max": 1000000, "step": 10, "std": 0,
        "label": "子账户最小资金(USD)", "hint": "BUY 时目标 slot 子钱包总资产低于该值则跳过该槽位（TD SLOT SKIP min_account_value），避免小资金碎仓；0=关闭。SELL/止损/止盈平仓不受限（平仓永远允许）",
    },
}

GROUP_TITLES = {
    "risk": "① 风险控制（WebUI 锁死 — LLM 不可改）",
    "exec": "② 执行质量与循环（WebUI 锁死 — LLM 不可改）",
    "td": "③ TD 自主运行（P2 — StrategyExecutor 主循环）",
    "batch": "④ 子钱包分批（批次=子钱包，真分账 v1.1，2026-08-10）",
}


# ── Path / load / save ───────────────────────────────────────────────────

def exec_params_path() -> Path:
    """Path to the persisted exec_params.json (WebUI 业务管理 → 执行参数)."""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion" / "credentials"
        try:
            if d.exists():
                return d / "exec_params.json"
        except OSError:
            continue
    return Path.home() / ".exec_params.json"


def validate_exec_param(key: str, value: Any) -> str | None:
    """Return an error message for an invalid value, or None if valid."""
    meta = PARAM_META.get(key)
    if meta is None:
        return "未知参数"
    vtype = meta.get("type", "float")
    if vtype == "bool":
        return None if isinstance(value, bool) else "必须是布尔值"
    if vtype == "list":
        if not isinstance(value, list) or not value:
            return "必须是非空列表"
        if not all(isinstance(v, str) and v.strip() for v in value):
            return "列表项必须是非空字符串"
        return None
    if vtype == "enum":
        if value not in meta["enum"]:
            return f"必须是 {'/'.join(meta['enum'])} 之一"
        return None
    if vtype == "str":
        if not isinstance(value, str) or not value.strip():
            return "不能为空"
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"必须是数字（{meta['min']}–{meta['max']}）"
    lo, hi = meta["min"], meta["max"]
    if meta.get("integer") and int(value) != value:
        return f"必须是整数（{lo}–{hi}）"
    if value < lo or value > hi:
        return f"超出范围 {lo}–{hi}"
    return None


def load_exec_params() -> dict[str, Any]:
    """Load persisted params, merged over defaults (validated keys only).

    Missing / invalid file → defaults.  A key saved with a value that no
    longer validates is ignored (falls back to the default), so a WebUI
    range change can never poison execution.
    """
    merged = dict(DEFAULT_EXEC_PARAMS)
    raw = _read_raw()
    if raw is None:
        return merged
    for key in merged:
        if key in raw and validate_exec_param(key, raw[key]) is None:
            merged[key] = raw[key]
    # 迁移：旧版单标的 td_symbol → td_symbols（标的池，2026-08-10）
    if "td_symbols" not in raw and raw.get("td_symbol"):
        merged["td_symbols"] = [raw["td_symbol"]]
    return merged


def save_exec_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist the full parameter set.

    Returns dict with "ok" and optional "error".  ``params == {"reset":
    True}`` removes the file and returns defaults (WebUI 恢复默认 button).
    """
    merged = dict(DEFAULT_EXEC_PARAMS)
    if not isinstance(params, dict):
        return {"ok": False, "error": "请求体必须为 JSON 对象"}
    if params.get("reset") is True:
        path = exec_params_path()
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"重置失败: {exc}"}
        return {"ok": True, "message": "已恢复默认执行参数", "params": dict(DEFAULT_EXEC_PARAMS)}
    for key in merged:
        if key in params:
            err = validate_exec_param(key, params[key])
            if err is not None:
                label = PARAM_META.get(key, {}).get("label", key)
                return {"ok": False, "error": f"{label}: {err}"}
            merged[key] = params[key]

    path = exec_params_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError as exc:
        return {"ok": False, "error": f"写入失败: {exc}"}
    return {"ok": True, "params": merged}


# ── Internal helpers ──────────────────────────────────────────────────────

def _read_raw() -> dict | None:
    """Parse exec_params.json; None when missing or invalid JSON."""
    try:
        raw = json.loads(exec_params_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None
