"""策略注册表测试 — StrategySpec 注册 / 选择持久化 / 分发。"""

from __future__ import annotations

import json

import pytest
from nanobot_quant.strategies.registry import (
    DEFAULT_STRATEGY,
    StrategySpec,
    get_strategy,
    list_strategies,
    list_strategies_names,
    load_selected,
    register,
    resolve_signal_fn,
    save_selected,
)


def test_default_strategy_is_td_sequential():
    assert DEFAULT_STRATEGY == "td_sequential"


def test_builtin_strategies_registered():
    names = list_strategies_names()
    assert "td_sequential" in names
    assert "td_sequential_cycle" in names


def test_cycle_is_variant_of_td():
    base = get_strategy("td_sequential")
    cycle = get_strategy("td_sequential_cycle")
    assert cycle.variant_of == "td_sequential"
    assert base.variant_of is None
    assert base.signal_fn is not None and cycle.signal_fn is not None


def test_duplicate_register_rejected():
    spec = StrategySpec(name="td_sequential", label="dup", description="d")
    with pytest.raises(ValueError):
        register(spec)


def test_unknown_strategy_raises():
    with pytest.raises(KeyError):
        get_strategy("no_such_strategy")


def test_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "strategy.json")
    save_selected(p, "td_sequential_cycle")
    assert load_selected(p) == "td_sequential_cycle"
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == {"strategy": "td_sequential_cycle"}


def test_save_unknown_strategy_rejected(tmp_path):
    p = str(tmp_path / "strategy.json")
    with pytest.raises(KeyError):
        save_selected(p, "bogus")


def test_load_missing_file_falls_back_to_default(tmp_path):
    assert load_selected(str(tmp_path / "nope.json")) == DEFAULT_STRATEGY


def test_resolve_default_is_td_calculate():
    fn = resolve_signal_fn()  # no strategy.json on CI → default
    assert fn.__module__ == "nanobot_quant.strategies.td_sequential"


def test_resolve_explicit_cycle():
    fn = resolve_signal_fn("td_sequential_cycle")
    assert fn.__module__ == "nanobot_quant.strategies.td_sequential_cycle"


def test_list_strategies_returns_specs():
    specs = list_strategies()
    assert all(isinstance(s, StrategySpec) for s in specs)
    assert any(s.name == "td_sequential" for s in specs)
