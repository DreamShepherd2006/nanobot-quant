"""TD Sequential strategy parameters — defaults, storage & validation.

The WebUI page ``/config/td-params`` edits these parameters and persists
them to ``{data_root}/legion/td_params.json``. Every consumer (MCP tools,
pipeline, strategy, backtest) reloads the latest file on each call, so
changes take effect immediately — no restart required.

Since the strategy registry (v0.2) supports multiple TD variants, the
parameter file is keyed by strategy name — each strategy keeps its own
independent set (and schema: the 同花顺 cycle variant has no countdown).
A legacy single-layer file (params directly at top level) is treated as
the ``td_sequential`` (production default) set.

Missing / invalid file → the strategy's ``params_defaults``, which for
``td_sequential`` is byte-for-byte identical to the pre-parameterisation
hardcoded behaviour (setup 9, countdown 13, compare 4, weights
0.40/0.30/0.15/0.10/0.05, score > 0).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ── Schema / defaults ────────────────────────────────────────────────────

#: Full parameter set (engine + strategy layers). Defaults == old hardcoded.
DEFAULT_TD_PARAMS: dict[str, Any] = {
    # ── ① TD algorithm (DeMark standard) ──────────────────────────────
    "setup_period": 9,          # int 5–14 (standard 9)
    "countdown_period": 13,     # int 9–16 (standard 13)
    "compare_length": 4,        # int 3–5 — close vs close[i-compare_length]
    "countdown_strict": True,   # bool — strict: close<=low[i-2]; relaxed: close<=high[i-1]
    "recycle_threshold": 18,    # int 1–30 — recycle countdown after >N consecutive bars
    # ── ② Scoring weights (sum must equal 1.0) ────────────────────────
    "weight_setup": 0.40,       # float ≥0 — Setup direction
    "weight_countdown": 0.30,   # float ≥0 — Countdown proximity
    "weight_tdst": 0.15,        # float ≥0 — TDST support
    "weight_volume": 0.10,      # float ≥0 — Volume confirmation
    "weight_bb": 0.05,          # float ≥0 — Bollinger regime
    # ── ③ Strategy layer ──────────────────────────────────────────────
    "score_threshold": 0.0,     # float 0–100 — entry requires score > threshold
    "entry_setup": 9,           # int 1–20 — LONG entry when setup_buy >= entry_setup
    "exit_setup": 9,            # int 1–20 — exit when setup_sell >= exit_setup
    "exit_countdown": 13,       # int 1–20 — exit when cd_sell >= exit_countdown
    "tdst_filter": False,       # bool — require close > tdst_support for entry
}

#: Human-readable bounds used by the WebUI form validation + display.
#: ``strategies`` limits a parameter to specific strategies (absent = all).
#: TD variant ``td_sequential_cycle`` (同花顺口径) has no countdown, so
#: countdown-related parameters are excluded from its schema.
PARAM_META: dict[str, dict[str, Any]] = {
    "setup_period": {"group": "td", "min": 5, "max": 14, "step": 1, "std": 9,
                     "label": "Setup 周期", "hint": "DeMark 标准 9；8–14 为实验变体"},
    "countdown_period": {"group": "td", "min": 9, "max": 16, "step": 1, "std": 13,
                         "label": "Countdown 周期", "hint": "DeMark 标准 13",
                         "strategies": ["td_sequential"]},
    "compare_length": {"group": "td", "min": 3, "max": 5, "step": 1, "std": 4,
                       "label": "Setup 比较长度", "hint": "close vs close[i-N]，标准 4"},
    "countdown_strict": {"group": "td", "type": "bool", "std": True,
                         "label": "Countdown 严格判定", "hint": "strict: close≤low[i-2]；relaxed: close≤high[i-1]",
                         "strategies": ["td_sequential"]},
    "recycle_threshold": {"group": "td", "min": 1, "max": 30, "step": 1, "std": 18,
                          "label": "Countdown 回收阈值", "hint": "连续 >N 根同向 K 线后重置",
                          "strategies": ["td_sequential"]},
    "weight_setup": {"group": "weights", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.40,
                     "label": "Setup 权重", "hint": "评分方向因子"},
    "weight_countdown": {"group": "weights", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.30,
                         "label": "Countdown 权重", "hint": "评分邻近度因子",
                         "strategies": ["td_sequential"]},
    "weight_tdst": {"group": "weights", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.15,
                    "label": "TDST 权重", "hint": "评分支撑因子"},
    "weight_volume": {"group": "weights", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.10,
                      "label": "Volume 权重", "hint": "评分量能因子"},
    "weight_bb": {"group": "weights", "min": 0.0, "max": 1.0, "step": 0.05, "std": 0.05,
                  "label": "Bollinger 权重", "hint": "评分区间因子"},
    "score_threshold": {"group": "strategy", "min": 0.0, "max": 100.0, "step": 1.0, "std": 0.0,
                        "label": "score 入场阈值", "hint": "score > 阈值才入场（当前 0 形同虚设）"},
    "entry_setup": {"group": "strategy", "min": 1, "max": 20, "step": 1, "std": 9,
                    "label": "入场 Setup 阈值", "hint": "setup_buy ≥ N 触发做多"},
    "exit_setup": {"group": "strategy", "min": 1, "max": 20, "step": 1, "std": 9,
                   "label": "平仓 Setup 阈值", "hint": "setup_sell ≥ N 平仓（当前偏早）"},
    "exit_countdown": {"group": "strategy", "min": 1, "max": 20, "step": 1, "std": 13,
                       "label": "平仓 Countdown 阈值", "hint": "cd_sell ≥ N 平仓",
                       "strategies": ["td_sequential"]},
    "tdst_filter": {"group": "strategy", "type": "bool", "std": False,
                    "label": "TDST 方向过滤", "hint": "要求 close > tdst_support 才入场"},
}

WEIGHT_KEYS = ("weight_setup", "weight_countdown", "weight_tdst",
               "weight_volume", "weight_bb")


# ── Path / load / save ───────────────────────────────────────────────────

def td_params_path() -> Path:
    """Path to the persisted td_params.json (WebUI 业务管理 → TD 参数)."""
    for root in ("/data", "/mnt/workspace"):
        d = Path(root) / "legion"
        try:
            if d.exists():
                return d / "td_params.json"
        except OSError:
            continue
    return Path.home() / ".td_params.json"


def load_td_params(strategy: str | None = None) -> dict[str, Any]:
    """Load persisted params for a strategy (default: currently selected).

    File layout: ``{"td_sequential": {...}, "td_sequential_cycle": {...}}``
    so each strategy keeps an independent parameter set (and schema). A
    legacy single-layer file (params directly at top level) is treated as
    the ``td_sequential`` (production default) set for backward
    compatibility. Missing / invalid file → the strategy's defaults.
    """
    if strategy is None:
        strategy = _selected_strategy()
    raw = _read_raw()
    if raw is None:
        return _defaults_for(strategy)

    if strategy in raw and isinstance(raw[strategy], dict):
        section = raw[strategy]
    elif _is_legacy_flat(raw):
        # legacy single-layer file → belongs to the production default strategy
        section = raw if strategy == "td_sequential" else {}
    else:
        section = {}

    merged = _defaults_for(strategy)
    for key in merged:
        if key in section and validate_value(key, section[key]) is None:
            merged[key] = section[key]
    return merged


def save_td_params(params: dict[str, Any], strategy: str | None = None) -> dict[str, Any]:
    """Validate + persist for a strategy (default: currently selected).

    Returns dict with "ok" and optional "error". ``params == {"reset":
    True}`` removes only the strategy's section and returns its defaults
    (used by the WebUI 恢复默认 button).
    """
    if strategy is None:
        strategy = _selected_strategy()
    merged = _defaults_for(strategy)
    if not isinstance(params, dict):
        return {"ok": False, "error": "请求体必须为 JSON 对象"}
    if params.get("reset") is True:
        path = td_params_path()
        try:
            raw = _read_raw() or {}
            if _is_legacy_flat(raw):
                raw = _migrate_legacy(raw)
            raw.pop(strategy, None)
            if raw:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            elif path.is_file():
                path.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"重置失败: {exc}"}
        return {"ok": True, "message": "已恢复默认参数", "params": merged}
    for key in merged:
        if key in params:
            err = validate_value(key, params[key])
            if err is not None:
                return {"ok": False, "error": f"{PARAM_META[key]['label']}: {err}"}
            merged[key] = params[key]
    err = validate_weights(merged)
    if err is not None:
        return {"ok": False, "error": err}

    path = td_params_path()
    try:
        raw = _read_raw() or {}
        if _is_legacy_flat(raw):
            raw = _migrate_legacy(raw)
        raw[strategy] = merged
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
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

def _selected_strategy() -> str:
    """Current strategy name from the registry (import deferred to avoid
    the registry→td_params module-level import cycle)."""
    from nanobot_quant.strategies.registry import load_selected

    return load_selected()


def _defaults_for(strategy: str) -> dict[str, Any]:
    """Strategy-specific defaults (registry params_defaults); falls back to
    the global defaults on any problem."""
    try:
        from nanobot_quant.strategies.registry import get_strategy

        return dict(get_strategy(strategy).params_defaults)
    except Exception:
        return dict(DEFAULT_TD_PARAMS)


def _read_raw() -> dict | None:
    """Parse td_params.json; None when missing or invalid JSON."""
    try:
        raw = json.loads(td_params_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _is_legacy_flat(raw: dict) -> bool:
    """True when the file is the old single-layer layout (params at top
    level instead of keyed by strategy)."""
    return any(key in raw for key in DEFAULT_TD_PARAMS)


def _migrate_legacy(raw: dict) -> dict:
    """Convert legacy single-layer layout to per-strategy layout."""
    return {
        "td_sequential": {k: raw[k] for k in DEFAULT_TD_PARAMS if k in raw},
    }


# ── Validation ───────────────────────────────────────────────────────────

def validate_value(key: str, value: Any) -> str | None:
    """Return an error message for an invalid value, or None if valid."""
    meta = PARAM_META.get(key)
    if meta is None:
        return "未知参数"
    if meta.get("type") == "bool":
        if not isinstance(value, bool):
            return "必须是布尔值"
        return None
    if isinstance(meta.get("min"), int) and isinstance(meta.get("max"), int):
        # integer parameter
        if isinstance(value, bool) or not isinstance(value, int):
            return f"必须是整数（{meta['min']}–{meta['max']}）"
    else:
        # float parameter
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"必须是数字（{meta['min']}–{meta['max']}）"
    lo, hi = meta["min"], meta["max"]
    if value < lo or value > hi:
        return f"超出范围 {lo}–{hi}"
    return None


def validate_weights(params: dict[str, Any]) -> str | None:
    """Weights must sum to 1.0 (tolerance 1e-6). The cycle variant keeps
    ``weight_countdown`` at 0 (schema-excluded), so the visible four weights
    still sum to 1.0."""
    total = sum(float(params[k]) for k in WEIGHT_KEYS)
    if abs(total - 1.0) > 1e-6:
        return f"权重合计必须 = 1.0（当前 {total:.3f}）"
    return None
