"""策略变体固定（2026-11-06，C118 补丁）。

原版 / 同花顺 / 富途三变体的 setup 累加、countdown、score 量纲完全不同：

* ``td_sequential``：9 后进 countdown，score 满分 28.75
* ``td_sequential_cycle``：翻转确认、数到 9 停止，无 countdown
* ``td_sequential_futu``：9 后继续累加、无 countdown、score = count/period

所以「同一个 preset」在不同策略页面下会跑出完全不同的结果。旧实现里
``_build_strategy`` 直接读 ``strategy.json``，而快照又不记录变体名 ⇒
结果不可复现、事后无法归因（8/27 原版与 11/02 之后富途的 preset 结果
没法对账）。本批修复：preset 必须固定变体，driver 强制按它构造引擎，
并把变体名 + 来源写进 ``backtest_config``。
"""

import inspect

import pytest

from nanobot_quant.backtest import driver as drv
from nanobot_quant.backtest import presets as ps


PRESET = "low9_multi_h9_minentry_eod"


def _params(**kw):
    """最小可用场景配置（够 BacktestDriver 构造，不触发任何取数）。"""
    return {
        "scenes": {
            "high": {
                "enabled": True,
                "sleeptime": "1m",
                "symbols": ["SOL"],
            }
        },
        **kw,
    }


# ── preset 层 ────────────────────────────────────────────────────────────


def test_preset_pins_strategy_variant():
    """内置 preset 必须写明策略变体，且与实盘当前选择一致（富途 NINE）。"""
    p = ps.get_preset(PRESET)
    assert p["run"]["strategy_variant"] == "td_sequential_futu"


def test_preset_full_exposes_variant_for_page():
    """页面「一键填充」拿到的完整参数里必须带变体（否则又回到跟随 strategy.json）。"""
    full = {p["name"]: p for p in ps.presets_full()}
    assert full[PRESET]["run"]["strategy_variant"] == "td_sequential_futu"


def test_preset_without_variant_is_rejected(monkeypatch):
    """preset 缺 strategy_variant ⇒ fail-fast（不能静默跟随空间配置）。"""
    good = ps.get_preset(PRESET)
    broken = {
        PRESET: {
            "version": good["version"],
            "description": good["description"],
            "run": {"scene": good["run"]["scene"]},  # 故意去掉变体
            "overrides": good["overrides"],
        }
    }
    monkeypatch.setattr(ps, "_PRESETS", broken)
    with pytest.raises(ValueError, match="strategy_variant"):
        ps.get_preset(PRESET)


def test_preset_variant_must_be_non_empty(monkeypatch):
    good = ps.get_preset(PRESET)
    run = dict(good["run"], strategy_variant="   ")
    broken = {
        PRESET: {
            "version": good["version"],
            "description": good["description"],
            "run": run,
            "overrides": good["overrides"],
        }
    }
    monkeypatch.setattr(ps, "_PRESETS", broken)
    with pytest.raises(ValueError, match="strategy_variant"):
        ps.get_preset(PRESET)


# ── driver 层 ────────────────────────────────────────────────────────────


def test_driver_uses_preset_variant_over_strategy_json(monkeypatch, capsys):
    """preset 指定变体时必须以它为准，并与空间选择不一致时打显式告警。"""
    monkeypatch.setattr(drv, "load_selected", lambda: "td_sequential_cycle")
    d = drv.BacktestDriver(
        scene="high", params=_params(), strategy_variant="td_sequential_futu"
    )
    assert d.strategy_variant == "td_sequential_futu"
    assert d.strategy_variant_source == "preset"

    err = capsys.readouterr().err
    assert "策略变体与空间当前选择不一致" in err
    assert "td_sequential_futu" in err and "td_sequential_cycle" in err


def test_driver_follows_strategy_json_when_unspecified(monkeypatch):
    """不用 preset 的老调用路径保持零行为变化：仍跟随 strategy.json。"""
    monkeypatch.setattr(drv, "load_selected", lambda: "td_sequential_cycle")
    d = drv.BacktestDriver(scene="high", params=_params())
    assert d.strategy_variant == "td_sequential_cycle"
    assert d.strategy_variant_source == "strategy.json"


def test_driver_silent_when_variant_matches(monkeypatch, capsys):
    """变体与空间一致时不应有告警噪音。"""
    monkeypatch.setattr(drv, "load_selected", lambda: "td_sequential_futu")
    drv.BacktestDriver(
        scene="high", params=_params(), strategy_variant="td_sequential_futu"
    )
    assert "不一致" not in capsys.readouterr().err


def test_driver_rejects_unknown_variant():
    """非法变体 fail-closed（不静默落回默认引擎）。"""
    with pytest.raises(ValueError, match="未知策略变体"):
        drv.BacktestDriver(
            scene="high", params=_params(), strategy_variant="no_such_strategy"
        )


def test_build_strategy_reads_pinned_variant():
    """结构性契约：_build_strategy 必须用固定变体，不得再读 load_selected()。"""
    src = inspect.getsource(drv.BacktestDriver._build_strategy)
    assert "self.strategy_variant" in src
    assert "load_selected()" not in src


def test_backtest_config_records_variant():
    """快照必须记录变体名与来源（否则页面/报告看不出用了哪个引擎）。"""
    src = inspect.getsource(drv.BacktestDriver.run)
    assert '"strategy_variant"' in src
    assert '"strategy_variant_source"' in src


# ── 入口层 ───────────────────────────────────────────────────────────────


def test_run_backtest_passes_variant_to_thread():
    """MCP/页面入口必须把 preset 的变体一路传到 driver（否则 preset 白写）。"""
    from nanobot_quant.tools import tools_backtest as tb

    src = inspect.getsource(tb.run_backtest)
    assert 'strategy_variant' in src
    assert '_run.get("strategy_variant")' in src or '"strategy_variant"' in src

    sig = inspect.signature(tb._auto_backtest_driver)
    assert "strategy_variant" in sig.parameters


def test_markdown_reports_variant():
    """报告必须能看出变体（与页面同源，避免「复制给人看时缺口径」）。"""
    from nanobot_quant import backtest_markdown as bm

    md = bm._variant_txt(
        {"strategy_variant": "td_sequential_futu", "strategy_variant_source": "preset"}
    )
    assert "td_sequential_futu" in md and "preset" in md
    assert "旧记录" in bm._variant_txt({})
    # 变体口径提示（可填但不生效）也必须写进报告
    assert "—" == bm._variant_notes_txt({})
    txt = bm._variant_notes_txt(
        {"strategy_variant_notes": ["无 countdown", "无 TDST"]}
    )
    assert "无 countdown" in txt and "无 TDST" in txt


# ── 变体参数有效性（可填但不生效）─────────────────────────────────────


def test_variant_notes_cover_futu_dead_params():
    from nanobot_quant.strategies.registry import variant_notes

    notes = " ".join(variant_notes("td_sequential_futu"))
    for key in (
        "entry_countdown",
        "exit_countdown",
        "cd_entry_setup_gap",
        "cd_exit_min_profit",
        "cd_exit_all",
        "cd_stall_n",
        "countdown_period",
        "weight_",
        "tdst_filter",
        "sell_only_profit_low",
    ):
        assert key in notes, f"{key} 未在富途变体提示里"


def test_variant_notes_empty_for_original():
    from nanobot_quant.strategies.registry import variant_notes

    assert variant_notes("td_sequential") == []
    assert variant_notes("no_such_variant") == []
