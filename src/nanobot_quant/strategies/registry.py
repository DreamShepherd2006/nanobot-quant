"""策略注册表 — 所有确定性信号策略在此登记。

路线（mode.json）管流程形态（quant = 确定性策略直出 / research = AI 辩论），
策略（strategy.json）管 quant 路线下用哪个确定性策略。两者正交：

    mode.json      → {"mode": "quant" | "research"}
    strategy.json  → {"strategy": "td_sequential"}（quant 路线下生效）

新策略接入 = 实现 signal_fn（K线 → TickerSignal）+ 登记 StrategySpec。
TD 的两个口径（原版 / 同花顺九转循环）作为独立变体注册，供校准与参数
扫描对比研究，不预设对错。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DEFAULT_STRATEGY = "td_sequential"

# 持久化路径候选（HF / MS 平台，与 tools_execute 的 live.json 同模式）
# 注意 data_root 有两种约定：标准部署 data_root=/data（或 /mnt/workspace），
# nanobot-quant 等空间 squad_config.json 中 data_root=/data/legion，导致 WebUI
# 写入 {data_root}/legion/strategy.json 实际落在 /data/legion/legion/strategy.json。
# 候选覆盖两种约定（legion/legion 优先；同一空间只会存在其一）。
_STRATEGY_PATHS = [
    "/data/legion/legion/strategy.json",
    "/data/legion/strategy.json",
    "/mnt/workspace/legion/legion/strategy.json",
    "/mnt/workspace/legion/strategy.json",
]


@dataclass(frozen=True)
class StrategySpec:
    """A registered deterministic signal strategy."""

    name: str                              # 唯一标识，如 "td_sequential"
    label: str                             # WebUI 展示名
    description: str                       # 适用场景 / 口径差异说明
    variant_of: Optional[str] = None       # 变体归属（如两个 TD 变体 variant_of="td_sequential"）
    params_schema: dict = field(default_factory=dict)    # PARAM_META 风格参数定义
    params_defaults: dict = field(default_factory=dict)  # 参数默认值（变体差异）
    data_source: str = "onchainos"         # onchainos / okx_cex / yfinance
    signal_fn: Optional[Callable] = None   # (df, **kwargs) -> TickerSignal dict
    enabled: bool = True


_REGISTRY: dict[str, StrategySpec] = {}
_REGISTERED = False


def register(spec: StrategySpec) -> None:
    """Register a strategy spec. Raises on duplicate name."""
    if spec.name in _REGISTRY:
        raise ValueError(f"duplicate strategy: {spec.name}")
    _REGISTRY[spec.name] = spec


def _ensure_registered() -> None:
    """Lazily register built-in strategies (avoids importing lumibot
    chain at module import time — pure-pandas envs can use the registry)."""
    global _REGISTERED
    if _REGISTERED:
        return
    # TD variants
    from nanobot_quant.strategies.td_sequential import calculate as _td_calc
    from nanobot_quant.strategies.td_sequential_cycle import calculate as _td_cycle_calc
    from nanobot_quant.strategies.td_sequential_futu import calculate as _td_futu_calc
    from nanobot_quant.td_params import DEFAULT_TD_PARAMS, PARAM_META

    register(StrategySpec(
        name="td_sequential",
        label="TD Sequential（原版）",
        description="DeMark TD Sequential：setup 计数超过 9 后继续累加（10, 11, 12…）。"
                    "当前生产默认行为，参数见「TD 策略参数」页。",
        params_schema=PARAM_META,
        params_defaults=dict(DEFAULT_TD_PARAMS),
        data_source="onchainos",
        signal_fn=_td_calc,
    ))
    register(StrategySpec(
        name="td_sequential_cycle",
        label="TD Sequential（同花顺九转）",
        description="TD Sequential 同花顺「九转序列」口径：setup 达到 9 后从 1 重新计数"
                    "（1-9 循环）；无 countdown（cd_buy/cd_sell 恒 0）；评分仅由"
                    "setup/TDST/Volume/Bollinger 组成，权重归一化。研究变体，供算法校准对照。",
        variant_of="td_sequential",
        params_schema=PARAM_META,
        params_defaults=_cycle_defaults(),
        data_source="onchainos",
        signal_fn=_td_cycle_calc,
    ))
    register(StrategySpec(
        name="td_sequential_futu",
        label="TD Sequential（富途 NINE）",
        description="富途「神奇九转」口径（忠实复刻 moonscript 源码）：无翻转确认，"
                    "连续满足即数；setup 达到 9 后继续累加不重置，信号恰在 count==9 的"
                    "那根触发一次（连续单边行情只触发一次）；无 countdown/TDST，"
                    "score = setup_count/setup_period（0–1 尺度）。民间最简化口径样本，"
                    "供三向算法对照。",
        variant_of="td_sequential",
        params_schema=PARAM_META,
        params_defaults=dict(DEFAULT_TD_PARAMS),
        data_source="onchainos",
        signal_fn=_td_futu_calc,
    ))
    _REGISTERED = True


def _cycle_defaults() -> dict[str, Any]:
    """同花顺口径默认参数：无 countdown。

    Removes the countdown weight and re-normalises the remaining four
    weights so they still sum to 1.0 (0.40/0.15/0.10/0.05 ÷ 0.70).
    """
    from nanobot_quant.td_params import DEFAULT_TD_PARAMS

    d = dict(DEFAULT_TD_PARAMS)
    d["weight_countdown"] = 0.0
    d["weight_setup"] = round(0.40 / 0.70, 4)   # 0.5714
    d["weight_tdst"] = round(0.15 / 0.70, 4)    # 0.2143
    d["weight_volume"] = round(0.10 / 0.70, 4)  # 0.1429
    d["weight_bb"] = round(0.05 / 0.70, 4)      # 0.0714
    return d


def get_strategy(name: str) -> StrategySpec:
    _ensure_registered()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown strategy: {name!r} (available: {', '.join(list_strategies_names())})"
        ) from None


def list_strategies(enabled_only: bool = True) -> list[StrategySpec]:
    _ensure_registered()
    return [s for s in _REGISTRY.values() if not enabled_only or s.enabled]


def list_strategies_names(enabled_only: bool = True) -> list[str]:
    return [s.name for s in list_strategies(enabled_only=enabled_only)]


# ── strategy.json 持久化 ──────────────────────────────────────────────

def strategy_paths() -> list[str]:
    """Candidate persistent paths (WebUI writes via gatekeeper data_root;
    MCP tool processes fall back to platform hardcoded paths)."""
    return list(_STRATEGY_PATHS)


def load_selected(path: str | None = None) -> str:
    """Read selected strategy name; falls back to DEFAULT_STRATEGY."""
    _ensure_registered()
    candidates = [path] if path else strategy_paths()
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                name = data.get("strategy")
                if name in _REGISTRY:
                    return name
            except (OSError, json.JSONDecodeError):
                continue
    return DEFAULT_STRATEGY


def save_selected(path: str, name: str) -> None:
    """Atomically persist the selected strategy (validates against registry)."""
    get_strategy(name)  # raises KeyError for unknown names
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"strategy": name}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def resolve_signal_fn(strategy: str | None = None) -> Callable:
    """Resolve the signal function for an explicit strategy name, or the
    currently selected one (strategy.json) when ``strategy`` is None.

    Used by tools (e.g. ``run_td_sequential``) so the WebUI selection takes
    effect immediately at the next call.
    """
    name = strategy if strategy is not None else load_selected()
    spec = get_strategy(name)
    if spec.signal_fn is None:
        raise RuntimeError(f"strategy {name!r} has no signal_fn")
    return spec.signal_fn
