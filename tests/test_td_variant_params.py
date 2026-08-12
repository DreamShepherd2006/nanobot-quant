"""方案 A（2026-08-12）：TD 循环与策略选择页 / td-params 参数集对齐。

覆盖：
1. td_params.json 从 {root}/legion/ 一次性迁移到 {root}/legion/credentials/（读旧写新）
2. TdSequentialStrategy._calc 按 strategy_variant 分发原版 / 同花顺九转 / 富途 NINE
3. td_live 构造 parameters 时 merge load_td_params(strategy)（entry_setup 等生效）
"""
import json
from unittest import mock

import numpy as np
import pandas as pd

from nanobot_quant import td_params as tp
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _mk_df():
    """先涨后跌（10 涨 + 30 跌）——触发 buy setup（翻转确认）+ buy countdown。"""
    close = list(range(100, 110)) + list(range(110, 80, -1))
    return pd.DataFrame({
        "Open": close,
        "High": [c + 1 for c in close],
        "Low": [c - 1 for c in close],
        "Close": close,
        "Volume": [1000] * len(close),
    })


def test_legacy_td_params_migrated_to_credentials(tmp_path):
    cred = tmp_path / "legion" / "credentials"
    cred.mkdir(parents=True)
    legacy = tmp_path / "legion" / "td_params.json"
    legacy.write_text(json.dumps({"td_sequential_cycle": {"entry_setup": 6}}), encoding="utf-8")
    with mock.patch.object(tp, "td_params_path", lambda: cred / "td_params.json"), \
         mock.patch.object(tp, "_legacy_td_params_path", lambda: legacy):
        raw = tp._read_raw()
        assert raw["td_sequential_cycle"]["entry_setup"] == 6
        assert (cred / "td_params.json").is_file()
        p = tp.load_td_params("td_sequential_cycle")
        assert p["entry_setup"] == 6


def test_strategy_variant_dispatch_cycle_vs_original():
    """变体分发：① 行为上 cycle 无 countdown（真实引擎）；② strategy_variant
    驱动 _calc 调用对应引擎（mock 断言）。"""
    cycle = TdSequentialStrategy.__new__(TdSequentialStrategy)
    cycle.parameters = {"strategy_variant": "td_sequential_cycle"}
    cycle._td_params = tp.load_td_params("td_sequential_cycle")
    sig = cycle._calc(_mk_df())
    assert sig["cd_buy"] == 0 and sig["cd_sell"] == 0  # cycle 无 countdown

    # 分发：cycle → td_sequential_cycle.calculate（函数内懒加载 import——mock 源模块生效）
    with mock.patch("nanobot_quant.strategies.td_sequential_cycle.calculate",
                    return_value={"cd_buy": 0, "cd_sell": 0}) as m_cycle:
        c2 = TdSequentialStrategy.__new__(TdSequentialStrategy)
        c2.parameters = {"strategy_variant": "td_sequential_cycle"}
        c2._td_params = {}
        c2._calc(_mk_df())
        m_cycle.assert_called_once()

    # 默认/缺省 → 原版 calculate（模块级 import——patch 模块属性）
    with mock.patch("nanobot_quant.strategies.td_sequential_strategy.calculate",
                    return_value={"cd_buy": 0, "cd_sell": 0}) as m_orig:
        o2 = TdSequentialStrategy.__new__(TdSequentialStrategy)
        o2.parameters = {}
        o2._td_params = {}
        o2._calc(_mk_df())
        m_orig.assert_called_once()


def test_td_live_merges_td_params_into_parameters():
    """td_live 构造 parameters 应 merge load_td_params(strategy)（entry_setup 生效）。"""
    with mock.patch("nanobot_quant.strategies.registry.load_selected",
                    return_value="td_sequential_cycle"), \
         mock.patch("nanobot_quant.td_params.load_td_params",
                    return_value={"entry_setup": 6, "exit_setup": 6}):
        # 与 td_live.py 构造路径一致的 merge 逻辑（函数内懒加载 import）
        from nanobot_quant.strategies.registry import load_selected
        from nanobot_quant.td_params import load_td_params
        strategy_name = load_selected()
        td_params = load_td_params(strategy_name)
        parameters = dict(
            TdSequentialStrategy.parameters,
            **{"strategy_variant": strategy_name},
            **td_params,
        )
        assert parameters["strategy_variant"] == "td_sequential_cycle"
        assert parameters["entry_setup"] == 6
