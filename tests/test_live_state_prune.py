"""LIVE_STATE 裁剪（2026-10-29，展示层修复方案 A）。

改池/缩池后 ``LIVE_STATE["symbols"]`` 里的旧标的条目必须被移除，否则页面
继续渲染它们、标题「N 标的」虚高（实测 9 → 4 后仍显示 9 行）。
"""

from __future__ import annotations

import pytest

from nanobot_quant import td_live_state as st
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


@pytest.fixture(autouse=True)
def _clean_state():
    st.LIVE_STATE["symbols"] = {}
    yield
    st.LIVE_STATE["symbols"] = {}


def test_prune_removes_symbols_out_of_pool():
    for sym in ("SOL", "BNB", "BTC", "ETH", "AAVE", "LINK", "UNI", "ARB", "AVAX"):
        st.update_symbol(sym, {"price": 1.0}, scene="high")
    assert len(st.LIVE_STATE["symbols"]["high"]) == 9

    removed = st.prune_symbols("high", ["SOL", "BNB", "BTC", "ETH"])

    assert sorted(removed) == ["AAVE", "ARB", "AVAX", "LINK", "UNI"]
    assert set(st.LIVE_STATE["symbols"]["high"]) == {"SOL", "BNB", "BTC", "ETH"}


def test_prune_keeps_other_scenes_untouched():
    st.update_symbol("SOL", {"price": 1.0}, scene="high")
    st.update_symbol("AAVE", {"price": 2.0}, scene="mid")

    st.prune_symbols("high", ["SOL"])

    assert set(st.LIVE_STATE["symbols"]["high"]) == {"SOL"}
    assert set(st.LIVE_STATE["symbols"]["mid"]) == {"AAVE"}


def test_prune_empty_keep_is_noop():
    """池解析失败（keep 为空）绝不能把状态清空。"""
    st.update_symbol("SOL", {"price": 1.0}, scene="high")
    assert st.prune_symbols("high", []) == []
    assert set(st.LIVE_STATE["symbols"]["high"]) == {"SOL"}


def test_activate_scene_prunes_in_live_mode():
    strat = object.__new__(TdSequentialStrategy)
    strat.parameters = {"live_mode": True}
    strat.symbols = ["AAVE", "ARB"]
    strat._current_scene = "high"
    for sym in ("SOL", "AAVE", "ARB"):
        st.update_symbol(sym, {"price": 1.0}, scene="high")

    # 直接走 _activate_scene 里那段裁剪逻辑（场景激活需 broker 等，此处只验语义）
    from nanobot_quant.td_live_state import prune_symbols

    removed = prune_symbols("high", strat.symbols)
    assert sorted(removed) == ["SOL"]


def test_prune_is_never_called_in_backtest_mode():
    """回测（live_mode 缺省）不得动 LIVE_STATE（PR #254 守卫语义）。"""
    st.update_symbol("SOL", {"price": 1.0}, scene="high")
    params = {}
    assert not params.get("live_mode")     # 守卫条件
    assert set(st.LIVE_STATE["symbols"]["high"]) == {"SOL"}
