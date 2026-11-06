"""回测规则预设（C118，2026-11-06 拍板）。

设计约束（用户拍板，改动前先读）：

1. **preset 是模板，不是锁** —— 选 preset 只是把一套键一次性填满（防漏参），
   页面 / MCP 传的显式覆盖优先级永远高于 preset。
2. **版本只增不改（append-only）** —— 同一 preset 名的历史版本 NEVER 原地改
   数值：改口径就新增一个版本（v1 → v2），v1 保留。这样半年前写在结果里的
   ``preset=xxx@v1`` 今天仍然逐位可复现。
3. **复现不靠 preset，靠落盘快照** —— 每个 run 的 ``backtest_config`` 记录
   全部实际生效值 + ``preset`` / ``preset_overrides``；preset 只是"少手打
   30 个键 + 便于口头引用"。
4. **自包含且 fail-fast** —— preset 必须覆盖 ``REQUIRED_OVERRIDE_KEYS`` 全集，
   缺一个就拒绝启动。理由：缺键会静默回退到场景/全局值，正是 2026-11-06
   那次 ``quantity_mode`` 漏传导致「以为在跑 A、实际跑 B」的同型缺陷。
5. 不做「页面另存为 preset」「用户自定义 preset 文件」「我的默认覆盖」——
   参数入口只留两条：preset + 本次覆盖（简单优先）。

字段分两段：

* ``run`` —— run 级参数（场景/标的/区间/周期/数据源/资金/滑点/共享池）。
  页面填了就以页面为准；未填由本段兜底。
* ``overrides`` —— 策略参数（影响决策的全部键，见 ``REQUIRED_OVERRIDE_KEYS``）。
"""

from __future__ import annotations

import copy

__all__ = [
    "preset_names",
    "presets_meta",
    "presets_full",
    "get_preset",
    "expand_preset",
    "REQUIRED_OVERRIDE_KEYS",
]


# ── 影响决策的键全集（preset 必须全部给出）──────────────────────────
# 新增策略参数时，除了加进 tools_backtest 的类型表，也要加进这里，
# 否则 preset 会"漏一个键"而静默回退到场景值 —— 见模块 docstring 第 4 条。
REQUIRED_OVERRIDE_KEYS: frozenset[str] = frozenset({
    # 入场
    "entry_setup",
    "entry_countdown",
    "cd_entry_setup_gap",
    "entry_periods",
    "entry_periods_partial",
    "entry_ema_enabled",
    "entry_ema_period",
    "entry_ema_offset_pct",
    "entry_min_gap_pct",
    "adaptive_gap_enabled",
    "adaptive_m",
    "wave_max_buys",
    "gate_enabled",
    "gate_red_min",
    "ema_gate_enabled",
    "ema_n",
    "ema_k",
    "tdst_filter",
    # 出场
    "exit_setup",
    "exit_countdown",
    "exit_first_cross",
    "exit_order",
    "sell_only_profit_high",
    "sell_only_profit_low",
    "momentum_exit",
    "cd_stall_n",
    "td_sell_all",
    "cd_exit_min_profit",
    "cd_exit_all",
    "min_hold_bars",
    "stop_loss_pct",
    "take_profit_pct",
    "min_account_value",
    # 强平
    "eod_enabled",
    "eod_parity",
    "eod_mode",
    "eod_hour_utc",
    # 数量 / 成本
    "quantity_mode",
    "td_start_slot",
    "fee_rate",
})


#: 预设定义表。**已发布版本禁止原地修改**（见 docstring 第 2 条）。
_PRESETS: dict[str, dict] = {
    "low9_multi_h9_minentry_eod": {
        "version": "v1",
        "label": "低9多周期 + 高9 min_entry + 双号强平",
        "description": (
            "EMA610 折价 ≤ −1.3% 过滤 + 1/2/3/4/5/8m 多周期 Low9（整根）建仓；"
            "High9 每轮平掉入场价最低且盈利的一仓；双号日 UTC 24:00 全平；"
            "共享现金池 10,000U（200×50），单笔 200U；不设止损/止盈。"
        ),
        "run": {
            "scene": "high",
            "symbols": ["ZEC", "NEAR", "DASH"],
            "start": "2026-08-01",
            "end": "2026-10-31",
            "periods": ["1m"],
            "source": "okx_cex",
            "initial_quote": 200.0,
            "batches": 50,
            "fixed_amount": 200.0,
            "slippage": 0.0,
            "shared_cash": True,
        },
        "overrides": {
            # ── 入场 ──
            "entry_setup": 9,
            "entry_countdown": 13,        # 富途变体 cd_buy 恒 0，此处为口径占位
            "cd_entry_setup_gap": 0,      # 关（cd 通路不参与）
            "entry_periods": "1,2,3,4,5,8",
            "entry_periods_partial": False,
            "entry_ema_enabled": True,
            "entry_ema_period": 610,
            "entry_ema_offset_pct": 1.3,
            "entry_min_gap_pct": 0.0,     # 关
            "adaptive_gap_enabled": False,
            "adaptive_m": 30,
            "wave_max_buys": 0,           # 关波锁
            "gate_enabled": False,        # 贝叶斯闸门关
            "gate_red_min": 0.45,
            "ema_gate_enabled": False,    # EMA 斜率闸门关
            "ema_n": 144,
            "ema_k": 3,
            "tdst_filter": False,         # 富途无 TDST，必须 false
            # ── 出场 ──
            "exit_setup": 9,
            "exit_countdown": 20,         # 关死 cd13 通道（只留高9）
            "exit_first_cross": True,
            "exit_order": "min_entry",
            "sell_only_profit_high": 1e-9,  # 「且盈利」：极小正数而非 0（0=关门）
            "sell_only_profit_low": 1e-9,
            "momentum_exit": False,
            "cd_stall_n": 3,
            "td_sell_all": False,         # 每轮只平一仓（不是全平）
            "cd_exit_min_profit": 0.0,
            "cd_exit_all": False,
            "min_hold_bars": 0,
            "stop_loss_pct": 0.0,         # 不设止损
            "take_profit_pct": 0.0,
            "min_account_value": 0.0,
            # ── 强平（双号日）──
            "eod_enabled": True,
            "eod_parity": "even",
            "eod_mode": "all",
            "eod_hour_utc": 0,
            # ── 数量 / 成本 ──
            "quantity_mode": "fixed_amount",
            "td_start_slot": 1,
            "fee_rate": 0.001,            # OKX 现货 taker 0.1% 单边
        },
    },
}


def preset_names() -> list[str]:
    """Return preset names in declaration order."""
    return list(_PRESETS)


def presets_meta() -> list[dict]:
    """Light metadata for the WebUI dropdown (no full parameter dump)."""
    return [
        {
            "name": name,
            "version": p.get("version"),
            "label": p.get("label", name),
            "description": p.get("description", ""),
        }
        for name, p in _PRESETS.items()
    ]


def presets_full() -> list[dict]:
    """Full preset payload for the WebUI (run defaults + every override).

    页面选预设时要把每个值**填进可见表单**，否则会出现「页面显示场景值、
    实际按预设跑」的静默不一致——这与「页面显示必须等于实际执行」相悖。
    """
    out = []
    for name, p in _PRESETS.items():
        out.append(
            {
                "name": name,
                "version": p.get("version"),
                "label": p.get("label", name),
                "description": p.get("description", ""),
                "run": copy.deepcopy(p.get("run") or {}),
                "overrides": copy.deepcopy(p.get("overrides") or {}),
            }
        )
    return out


def get_preset(name: str) -> dict:
    """Return a deep copy of ``name`` after validating self-containment.

    Raises ``ValueError`` when the name is unknown or the preset does not
    cover every key in :data:`REQUIRED_OVERRIDE_KEYS` — failing here beats
    silently falling back to scene values at runtime.
    """
    key = str(name or "").strip()
    if not key:
        raise ValueError("preset 名称不能为空")
    p = _PRESETS.get(key)
    if p is None:
        raise ValueError(
            f"未知 preset {key!r}（可用: {', '.join(_PRESETS) or '无'}）"
        )
    ov = p.get("overrides") or {}
    missing = sorted(REQUIRED_OVERRIDE_KEYS - set(ov))
    if missing:
        raise ValueError(
            f"preset {key}@{p.get('version')} 缺少必填键: {', '.join(missing)}"
            "（preset 必须自包含，缺键会静默回退场景值）"
        )
    return copy.deepcopy(p)


def expand_preset(name: str, overrides: dict | None = None) -> tuple[dict, dict]:
    """Merge a preset with explicit overrides (explicit always wins).

    Returns ``(merged_overrides, meta)`` where ``meta`` carries the preset
    name/version, its run-level defaults and the list of keys the caller
    overrode — all of which land in ``backtest_config`` so a run can be
    reproduced and audited without guessing.
    """
    p = get_preset(name)
    base = dict(p.get("overrides") or {})
    explicit = {k: v for k, v in (overrides or {}).items() if v is not None}
    merged = {**base, **explicit}
    diff = {k: v for k, v in explicit.items() if base.get(k) != v}
    meta = {
        "name": str(name).strip(),
        "version": p.get("version"),
        "label": p.get("label", name),
        "run": dict(p.get("run") or {}),
        "overridden": diff,
    }
    return merged, meta
