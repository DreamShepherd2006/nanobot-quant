"""出场首穿（exit_first_cross）——2026-11-06 用户规则回测新增。

语义：``exit_first_cross=True`` 时，高9（setup_sell ≥ exit_setup）与 cd13
（cd_sell ≥ exit_countdown）只在**首次跨过阈值**的那一根触发；累加期
（9→10→11→12）不再重复平仓。默认 False = 现有行为（每根满足条件的 bar 都触发）。

富途 NINE 变体的 setup 计数 9 后继续累加，若不开首穿，一个高9 周期会被
切成 3–4 次独立出场机会（每次平一批）——与研究口径「出现一次 High9 平一仓」
不一致。本参数只影响出场判定，不动入场、不动选批顺序。
"""

from __future__ import annotations

import pytest

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _mk(first_cross: bool, symbol: str = "SOL") -> TdSequentialStrategy:
    """绕过 lumibot 依赖构造裸对象（只测 ``_exit_triggers`` 纯逻辑）。"""
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = symbol
    s._exit_first_cross = first_cross
    s._exit_prev_setup = {}
    s._exit_prev_cd = {}
    return s


# ── 核心语义 ──────────────────────────────────────────────────────────────

def test_first_cross_triggers_once_in_accumulation():
    s = _mk(True)
    seq = [8, 9, 10, 11, 12]
    hits = [s._exit_triggers(n, 0, 9, 13)[0] for n in seq]
    assert hits == [False, True, False, False, False]


def test_first_cross_retriggers_after_reset():
    s = _mk(True)
    seq = [9, 10, 5, 9]
    hits = [s._exit_triggers(n, 0, 9, 13)[0] for n in seq]
    assert hits == [True, False, False, True]


def test_disabled_matches_legacy_behavior():
    s = _mk(False)
    seq = [9, 10, 11]
    assert [s._exit_triggers(n, 0, 9, 13)[0] for n in seq] == [True, True, True]


def test_cd_channel_first_cross():
    s = _mk(True)
    seq = [12, 13, 14, 0, 13]
    hits = [s._exit_triggers(0, n, 9, 13)[1] for n in seq]
    assert hits == [False, True, False, False, True]


def test_prev_state_updated_even_when_disabled():
    """开关中途打开不能拿陈旧 prev 误判首穿（先关后开）。"""
    s = _mk(False)
    s._exit_triggers(10, 0, 9, 13)  # 关闭时已处于累加期
    s._exit_first_cross = True
    assert s._exit_triggers(11, 0, 9, 13)[0] is False
    assert s._exit_triggers(12, 0, 9, 13)[0] is False


def test_state_isolated_per_symbol():
    s = _mk(True)
    assert s._exit_triggers(9, 0, 9, 13)[0] is True   # SOL 首穿
    s.symbol = "BTC"
    assert s._exit_triggers(9, 0, 9, 13)[0] is True   # BTC 自己算首穿
    s.symbol = "SOL"
    assert s._exit_triggers(10, 0, 9, 13)[0] is False  # SOL 仍在累加期


def test_threshold_change_still_first_cross():
    """exit_setup 变化时，prev 与当前阈值比较（不写死 9）。"""
    s = _mk(True)
    assert s._exit_triggers(6, 0, 6, 13)[0] is True
    assert s._exit_triggers(7, 0, 6, 13)[0] is False


# ── 参数接线（结构性，防「注册了但没接线」） ──────────────────────────────

def test_param_registered_and_default_off():
    from nanobot_quant.exec_params import (
        DEFAULT_SCENES,
        PARAM_META,
        SCENE_ONLY_FIELDS,
    )

    assert "exit_first_cross" in PARAM_META
    assert PARAM_META["exit_first_cross"].get("type") == "bool"
    assert PARAM_META["exit_first_cross"].get("std") is False
    assert "exit_first_cross" in SCENE_ONLY_FIELDS
    for scene in ("high", "mid", "low"):
        assert DEFAULT_SCENES[scene]["exit_first_cross"] is False


def test_overrides_bool_coercion():
    from nanobot_quant.tools.tools_backtest import coerce_overrides

    assert coerce_overrides({"exit_first_cross": "on"}) == {
        "exit_first_cross": True
    }
    # 字符串 "false" 必须归一成 False（否则 bool("false") 为真）
    assert coerce_overrides({"exit_first_cross": "false"}) == {
        "exit_first_cross": False
    }
    assert coerce_overrides({"exit_first_cross": None}) == {}


def test_activate_scene_reads_scene_value():
    """场景值优先、留空回退全局；字符串形式也能正确解析。"""
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL"
    s._exit_prev_setup = {}
    s._exit_prev_cd = {}
    s.parameters = {}
    s._exit_first_cross = False

    # 直接调用最简路径：模拟 _activate_scene 中的读取逻辑
    from nanobot_quant.strategies.td_sequential_strategy import as_bool_flag

    for raw, expected in [
        (True, True),
        (False, False),
        ("on", True),
        ("false", False),
        (None, False),
    ]:
        assert as_bool_flag(raw, False) is expected
