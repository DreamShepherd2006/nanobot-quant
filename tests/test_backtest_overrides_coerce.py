"""回测覆盖参数归一化：MCP 与 WebUI 两个入口必须同源。

2026-11-05 修复：`_coerce_overrides` 原先只在 `backtest_handlers`（WebUI
路径）里，MCP `run_backtest` 直连时 overrides 原样透传 —— 字符串
`"false"` 在策略侧 `if parameters.get(...)` 里是真值，会静默生效，两个
入口可能跑出不同结果。现下沉到 `tools_backtest.coerce_overrides`，
`run_backtest` 入口统一调用。
"""

from __future__ import annotations

import sys
import time
import types

import pytest


def _tb():
    from nanobot_quant.tools import tools_backtest as tb

    return tb


def test_default_initial_quote_is_100():
    """页面输入框、MCP 默认、handler 兜底必须是同一个值。"""
    assert _tb().DEFAULT_INITIAL_QUOTE == 100.0


def test_int_fields_coerced_from_strings():
    got = _tb().coerce_overrides({"entry_setup": "8", "eod_hour_utc": "8", "adaptive_m": 30})
    assert got == {"entry_setup": 8, "eod_hour_utc": 8, "adaptive_m": 30}
    assert all(isinstance(v, int) for v in got.values())


def test_float_fields_coerced_from_strings():
    got = _tb().coerce_overrides({"take_profit_pct": "0.02", "sell_only_profit_high": "10"})
    assert got == {"take_profit_pct": 0.02, "sell_only_profit_high": 10.0}
    assert all(isinstance(v, float) for v in got.values())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("false", False), ("False", False), ("0", False), (0, False), (False, False),
        ("true", True), ("TRUE", True), ("1", True), ("on", True), ("yes", True),
        (1, True), (True, True),
    ],
)
def test_bool_fields_coerced(raw, expected):
    """关键回归：字符串 "false" 必须是 False，不能按非空字符串当 True。"""
    got = _tb().coerce_overrides({"eod_enabled": raw})
    assert got["eod_enabled"] is expected


def test_empty_and_null_ignored():
    got = _tb().coerce_overrides(
        {"entry_setup": None, "take_profit_pct": "", "eod_enabled": "null"}
    )
    assert got == {}


def test_unknown_key_passthrough():
    got = _tb().coerce_overrides({"exit_order": "min_entry", "quantity_mode": "fixed_amount"})
    assert got == {"exit_order": "min_entry", "quantity_mode": "fixed_amount"}


def test_invalid_value_raises():
    with pytest.raises(ValueError):
        _tb().coerce_overrides({"entry_setup": "abc"})


def test_none_returns_empty():
    assert _tb().coerce_overrides(None) == {}
    assert _tb().coerce_overrides({}) == {}


def test_run_backtest_coerces_before_dispatch(monkeypatch):
    """MCP 入口本身必须归一化，而不是依赖调用方传对类型。"""
    tb = _tb()
    captured = {}

    # run_backtest 的 driver 分支会 import fetchers（依赖 pandas/lumibot），
    # 测试环境可能没有 —— 注入一个最小 stub 即可。
    fake_fetchers = types.ModuleType("nanobot_quant.backtest.fetchers")
    fake_fetchers.normalize_source = lambda src: src or "gate_cex"
    monkeypatch.setitem(sys.modules, "nanobot_quant.backtest.fetchers", fake_fetchers)

    def fake_driver(*args, **kwargs):
        # 线程 target 用位置参数：run_id, scene, syms, start, end,
        # initial_quote, batches, slippage, fixed_amount, overrides, source, periods
        captured["overrides"] = args[9]

    monkeypatch.setattr(tb, "_auto_backtest_driver", fake_driver)

    resp = tb.run_backtest(
        engine="driver",
        scene="high",
        symbols=["SOL"],
        overrides={"eod_enabled": "false", "take_profit_pct": "0", "entry_setup": "9"},
    )
    assert resp.get("status") == "started", resp

    deadline = time.time() + 3
    while "overrides" not in captured and time.time() < deadline:
        time.sleep(0.02)

    assert captured["overrides"] == {
        "eod_enabled": False,
        "take_profit_pct": 0.0,
        "entry_setup": 9,
    }


def test_run_backtest_returns_error_on_bad_override(monkeypatch):
    tb = _tb()
    resp = tb.run_backtest(engine="driver", scene="high", symbols=["SOL"],
                           overrides={"entry_setup": "not-a-number"})
    assert "error" in resp
    assert "非法" in resp["error"]


def test_webui_handler_shares_same_implementation():
    """结构性断言：WebUI 侧不得再出现第二份实现。"""
    pytest.importorskip("starlette")
    from nanobot_quant import backtest_handlers as bh

    assert not hasattr(bh, "_coerce_overrides"), "handler 侧不应保留旧实现"
    assert bh.coerce_overrides is _tb().coerce_overrides
    assert bh.DEFAULT_INITIAL_QUOTE == _tb().DEFAULT_INITIAL_QUOTE
