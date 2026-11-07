"""实盘参数快照（2026-11-07 PR 2）单元测试。"""

from __future__ import annotations

import pytest


def _seed_scene(**over):
    from nanobot_quant.exec_params import DEFAULT_SCENES, save_exec_params

    sc = dict(DEFAULT_SCENES["high"])
    sc.update(
        {
            "enabled": True,
            "execution_channel": "okx_cex",
            "sub_accounts": ["DreamShepherdbot2"],
            "symbols": ["SOL", "BNB"],
            "capital_total": 1000.0,
            "td_fixed_amount": 4.0,
            "batches": 3,
            "entry_setup": 8,
        }
    )
    sc.update(over)
    save_exec_params(
        {
            "execution_channel": "okx_cex",
            "quote_ccy": "USDC",
            "scenes": {"high": sc},
        }
    )
    return sc


def test_build_snapshot_is_self_contained_and_excludes_runtime_fields():
    from nanobot_quant.backtest.presets import REQUIRED_OVERRIDE_KEYS
    from nanobot_quant.exec_presets import build_snapshot

    _seed_scene()
    snap = build_snapshot("high")

    ov = snap["overrides"]
    missing = REQUIRED_OVERRIDE_KEYS - set(ov)
    assert not missing, f"快照不自包含: {sorted(missing)}"
    # 运行时/环境字段不进 overrides
    for key in ("enabled", "execution_channel", "sub_accounts"):
        assert key not in ov
    # 场景值优先
    assert ov["entry_setup"] == 8
    # 资金与执行环境记录在 run 里（只读对照，不参与恢复）
    assert snap["run"]["capital_total"] == 1000.0
    assert snap["run"]["source"] == "okx_cex"
    assert snap["run"]["symbols"] == ["SOL", "BNB"]
    assert snap["run"]["batches"] == 3
    assert snap["run"]["fixed_amount"] == 4.0
    assert snap["source"] == "live_snapshot"


def test_build_snapshot_default_scene_self_contained():
    """mid/low 即使没显式配置也存在于 scenes（load 时补默认），快照应能自包含。"""
    from nanobot_quant.backtest.presets import REQUIRED_OVERRIDE_KEYS
    from nanobot_quant.exec_presets import build_snapshot

    _seed_scene()
    snap = build_snapshot("mid")
    assert REQUIRED_OVERRIDE_KEYS - set(snap["overrides"]) == set()
    assert snap["run"]["scene"] == "mid"


def test_save_and_load_roundtrip():
    from nanobot_quant.exec_presets import (
        build_snapshot,
        default_snapshot_name,
        get_user_preset,
        load_user_presets,
        save_user_preset,
    )

    _seed_scene()
    name = default_snapshot_name("high")
    save_user_preset(name, build_snapshot("high"))

    assert name in load_user_presets()
    assert get_user_preset(name)["run"]["scene"] == "high"


def test_duplicate_name_rejected():
    from nanobot_quant.exec_presets import build_snapshot, save_user_preset

    _seed_scene()
    save_user_preset("snap_dup_20261107", build_snapshot("high"))
    with pytest.raises(ValueError, match="已存在"):
        save_user_preset("snap_dup_20261107", build_snapshot("high"))


def test_builtin_name_rejected():
    from nanobot_quant.backtest.presets import preset_names
    from nanobot_quant.exec_presets import build_snapshot, save_user_preset

    _seed_scene()
    builtin = preset_names()[0]
    with pytest.raises(ValueError, match="内置"):
        save_user_preset(builtin, build_snapshot("high"))


def test_invalid_name_rejected():
    from nanobot_quant.exec_presets import build_snapshot, save_user_preset

    _seed_scene()
    with pytest.raises(ValueError, match="只能包含"):
        save_user_preset("坏 名字/带空格", build_snapshot("high"))


def test_incomplete_overrides_rejected():
    from nanobot_quant.exec_presets import save_user_preset

    with pytest.raises(ValueError, match="不自包含"):
        save_user_preset("snap_bad_20261107", {"overrides": {"entry_setup": 9}})


def test_presets_meta_and_get_preset_see_snapshot():
    from nanobot_quant.backtest.presets import get_preset, preset_names, presets_meta
    from nanobot_quant.exec_presets import build_snapshot, save_user_preset

    _seed_scene()
    name = "live_high_20261107"
    save_user_preset(name, build_snapshot("high"))

    metas = {m["name"]: m for m in presets_meta()}
    assert metas[name]["source"] == "user"
    assert any(m["source"] == "builtin" for m in metas.values())
    # 内置在前、快照在后
    assert preset_names().index(name) > 0
    assert get_preset(name)["run"]["scene"] == "high"
