"""On-chain execution parameters — defaults, storage & validation.

The WebUI page ``/config/exec`` edits these parameters and persists them
to ``{data_root}/credentials/exec_params.json`` (next to ``live.json``).
Every consumer (pipeline, execute_signal, broker) reloads the latest file
on each call, so changes take effect immediately — no restart required.

Control-plane design (2026-08-08):

- These five parameters are SYSTEM-LEVEL policy: they are locked by the
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
    # ── ③ Execution mode (2026-08-08, §15.5.1) ───────────────────────
    # direct (default): execute_signal 同步直调; loop: 信号入队异步执行。
    "execution_mode": "direct",  # str — "direct" | "loop"
}

#: Valid execution_mode values (loop = StrategyExecutor 循环，见 execution_loop.py)。
EXECUTION_MODES: tuple[str, ...] = ("direct", "loop")

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
}

GROUP_TITLES = {
    "risk": "① 风险控制（WebUI 锁死 — LLM 不可改）",
    "exec": "② 执行质量（WebUI 锁死 — LLM 不可改）",
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
    if key == "execution_mode":
        return None if value in EXECUTION_MODES else "必须是 direct 或 loop"
    meta = PARAM_META.get(key)
    if meta is None:
        return "未知参数"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"必须是数字（{meta['min']}–{meta['max']}）"
    lo, hi = meta["min"], meta["max"]
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
    return merged


def save_exec_params(params: dict[str, Any]) -> dict[str, Any]:
    """Validate + persist the full parameter set.

    Returns dict with "ok" and optional "error".  ``params == {"reset":
    True}`` removes the file and returns defaults (WebUI 恢复默认 button).

    ``execution_mode`` is preserved across WebUI saves: the form only submits
    the five numeric parameters, so a mode set in the file (or via CLI) must
    not be reset to "direct" by an unrelated parameter edit.
    """
    merged = dict(DEFAULT_EXEC_PARAMS)
    raw = _read_raw()
    if raw is not None and raw.get("execution_mode") in EXECUTION_MODES:
        merged["execution_mode"] = raw["execution_mode"]
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
