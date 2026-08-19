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
- ``max_drawdown_pct`` / ``stop_loss_pct`` are effective in backtest
  today; on the execute_signal path they are formal checks
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
    "slippage": 0.01,           # float [0,1) — swap slippage tolerance in percent (1 = 1%)
    "sol_buffer_pct": 0.05,     # float [0,1) — extra SOL reserved on buys
    "execution_channel": "okx_dex", # enum — 实例名（okx_dex=OKX DEX 链上 / gate=Gate.io 交易所）；旧值 dex/cex 自动迁移
    # ── ③ TD 自主运行（P2 B2/B3, StrategyExecutor 主循环）─────────────
    "td_enabled": False,        # WebUI 开关：TD 自主 live 循环启停
    "td_symbols": ["SOL"],     # TD 标的池（多标的扫描，谁 Setup 9 谁执行；
                                #   /config/tokens 登记代币 symbol，稳定币不列入）
    "td_sleeptime": "1D",      # 主循环周期（对应 lumibot sleeptime + K 线粒度）
    "quantity_mode": "fixed",  # fixed=固定 td_quantity；value=pv_slot × max_position_pct；fixed_amount=固定金额 td_fixed_amount
    "td_quantity": 10,          # int ≥1 — quantity_mode=fixed 时的下单数量
    "td_fixed_amount": 10.0,    # float 1-5000 — quantity_mode=fixed_amount 时的每笔建仓金额（U；CEX=USDT / DEX=USDC）
    "td_bars": 120,             # int 20-300 — TD 每轮拉取最近 N 根 K 线（固定窗口）
    # ── ④ 子钱包分批（批次=子钱包，真分账 v1.1，2026-08-10）─────────
    "td_batches": 1,            # int 1-50 — 批次/子钱包数量；1=单仓模式（现状）
    "exit_order": "fifo",      # fifo=先买先卖（默认）/ lifo=后买先卖
    "take_profit_pct": 0.0,     # 止盈线（%）；0=关闭（纯 TD SELL + 止损）
    "td_start_slot": 1,          # int 1-50 — BUY 扫描起点（完整循环 + 起点偏移）
    "min_account_value": 0,    # float ≥0 — BUY 门槛：目标 slot 子钱包总资产低于该值则跳过（0=关闭）
    "min_position_value": 1.0, # float ≥0 — 对账导入阈值(USD)：链上持仓价值低于该值视为 dust 不导入（0=关闭）
    # ── ⑤ UI ───────────────────────────────────────────────────────────
    "td_ui_refresh_s": 10,    # int 3-300 — /config/td-table 实时监控 tab 自动刷新间隔（秒）
}

#: Valid TD main-loop cadences (lumibot sleeptime strings).
TD_SLEEPTIMES: tuple[str, ...] = ("1m", "5m", "15m", "1H", "1D", "1W")

#: Valid position-sizing modes for the TD autonomous strategy.
#:  fixed = 固定数量（td_quantity 个币）；value = pv_slot × max_position_pct（百分比仓位）；
#:  fixed_amount = 固定金额（td_fixed_amount U，每笔建仓花固定稳定币金额，2026-08-19 新增）。
QUANTITY_MODES: tuple[str, ...] = ("fixed", "value", "fixed_amount")

#: Valid batch exit orders.
EXIT_ORDERS: tuple[str, ...] = ("fifo", "lifo")

#: Valid execution channels — concrete broker instances (spec names).
#: 大类（dex/cex）由 spec.family 表达，UI 按大类分组显示（方案 C，2026-08-17）：
#:   链上 DEX 组：okx_dex；交易所（CEX）组：gate。
#: 新增交易所 = 注册 BrokerSpec + 此处加一项 + enum_groups 加一项，上层零改动。
EXECUTION_CHANNELS: tuple[str, ...] = ("okx_dex", "gate")

#: Legacy channel values auto-migrated on load/save (2026-08-17 方案 C).
LEGACY_CHANNEL_ALIASES: dict[str, str] = {"dex": "okx_dex", "cex": "gate"}


def normalize_execution_channel(value: Any) -> str:
    """Normalize an execution-channel value to the concrete instance name.

    Idempotent: legacy ``dex``/``cex`` → ``okx_dex``/``gate``; unknown values
    pass through unchanged (callers fail closed later, never silently map).
    """
    v = str(value) if value is not None else ""
    return LEGACY_CHANNEL_ALIASES.get(v, v)

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
        "channels": "dex",
        "label": "滑点容忍", "hint": "swap 滑点容忍（百分比，1=1%，如 0.5=0.5%）；过小易滑点超限失败（82112），过大成交价劣",
    },
    "sol_buffer_pct": {
        "group": "exec", "min": 0.0, "max": 1.0, "step": 0.01, "std": 0.05,
        "channels": "dex",
        "label": "SOL 缓冲", "hint": "BUY 时按比例预留 SOL 覆盖 gas 与报价-成交间价格波动（仅 DEX 通道）",
    },
    "execution_channel": {
        "group": "exec", "type": "enum", "enum": list(EXECUTION_CHANNELS), "std": "okx_dex",
        "enum_labels": {
            "okx_dex": "OKX DEX（链上 · OnchainOS 子钱包）",
            "gate": "Gate.io（交易所 · 子账号）",
        },
        "enum_groups": {
            "链上 DEX（TEE 签名 · 子钱包）": ["okx_dex"],
            "交易所 CEX（API Key · 子账号）": ["gate"],
        },
        "label": "执行通道", "hint": "两大执行家族是完全不同的概念：dex=链上 DEX（OKX OnchainOS 子钱包，TEE 签名，默认）；cex=交易所（当前实例 Gate.io，子账号 API Key）。只影响之后的新下单（execute_signal / TD 循环），不迁移持仓；切到交易所时 TD 循环 K 线数据源联动切换为同所 Gate CEX 公共端点。未来多家 CEX/DEX 在此下拉分组内扩展",
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
        "label": "数量模式", "hint": "fixed=固定 td_quantity（默认 10，回测语义不变）；value=按实时 slot 总资产 × 单仓上限；fixed_amount=每笔固定金额（td_fixed_amount）",
    },
    "td_quantity": {
        "group": "td", "min": 1, "max": 100000, "step": 1, "std": 10, "integer": True,
        "label": "TD 固定数量", "hint": "quantity_mode=fixed 时的下单数量（默认 10）",
    },
    "td_fixed_amount": {
        "group": "td", "min": 1.0, "max": 5000.0, "step": 1.0, "std": 10.0,
        "label": "TD 固定金额", "hint": "quantity_mode=fixed_amount 时的每笔建仓金额（U：CEX=USDT / DEX=USDC）。固定金额模式跳过单仓上限（max_position_pct）校验（金额即用户显式仓位），但资金检查保留；CEX 通道需 ≥3U（Gate 最小单），DEX 无下限",
    },
    "td_bars": {
        "group": "td", "min": 20, "max": 300, "step": 1, "std": 120, "integer": True,
        "label": "K 线窗口", "hint": "TD 每轮拉取最近 N 根 K 线（固定窗口，不累积增长；300 = onchainos CLI 单次上限）。与分析页 K 线数设一致可完全对照",
    },
    # ── ④ 子钱包分批 ──────────────────────────────────────────────────
    "td_batches": {
        "group": "batch", "min": 1, "max": 50, "step": 1, "std": 1, "integer": True,
        "label": "批次数量（子钱包）",
        "label_cex": "批次数量（子账号）",
        "hint": "1=单仓模式（现状）；>1 时每批绑定一个 Agentic Wallet 子钱包，保存后自动创建不足的子钱包并建立映射",
        "hint_cex": "1=单仓模式（现状）；>1 时每批绑定一个 Gate 子账号（slot↔gate_bot1-5），子账号下单用自身 API Key",
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
    "min_position_value": {
        "group": "batch", "min": 0, "max": 1000000, "step": 1, "std": 1.0,
        "channels": "dex",
        "label": "对账导入阈值(USD)",
        "hint": "启动对账时链上持仓价值低于该值视为 dust 不导入（slot 保持可建仓），避免微量残留（如卖出后尾仓 $0.13）占用资金槽位；0=关闭。CEX 通道用 Gate min_quote 动态阈值（≈$3），不读此参数",
    },
    # ── ⑤ UI ────────────────────────────────────────────────────────────
    "td_ui_refresh_s": {
        "group": "td", "min": 3, "max": 300, "step": 1, "std": 10, "integer": True,
        "label": "监控刷新(秒)", "hint": "/config/td-table「实时监控」tab 自动刷新间隔",
    },
}

GROUP_TITLES = {
    "risk": "① 风险控制（WebUI 锁死 — LLM 不可改）",
    "exec": "② 执行通道与质量（WebUI 锁死 — LLM 不可改）",
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
    # 迁移：旧版 execution_channel 大类值（dex/cex）→ 实例名（okx_dex/gate），2026-08-17 方案 C
    if "execution_channel" in raw:
        raw["execution_channel"] = normalize_execution_channel(raw["execution_channel"])
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
    # 迁移：保存时旧版大类值自动归一化为实例名（WebUI 保存即迁移，2026-08-17 方案 C）
    if "execution_channel" in params:
        params["execution_channel"] = normalize_execution_channel(params["execution_channel"])
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
