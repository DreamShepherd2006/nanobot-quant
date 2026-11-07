"""实盘参数快照（2026-11-07 PR 2）单元测试。"""

from __future__ import annotations

import pytest


def _seed_scene(**over):
    from nanobot_quant.exec_params import DEFAULT_SCENES, save_exec_params

    sc = dict(DEFAULT_SCENES["high"])
    sc.update(
        {
            "enabled": bool(over.pop("enabled", False)),
            "execution_channel": "okx_cex",
            "sub_accounts": ["DreamShepherdbot2"],
            "symbols": ["SOL", "BNB"],
            "capital_total": 1000.0,
            "td_fixed_amount": 4.0,
            "batches": 3,
            "entry_setup": 8,
            "take_profit_pct": 0.02,
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


# ---------------------------------------------------------------------------
# PR 3：恢复快照
# ---------------------------------------------------------------------------


def _save_high_snapshot(name="live_high_apply"):
    from nanobot_quant.exec_presets import build_snapshot, save_user_preset

    save_user_preset(name, build_snapshot("high"))
    return name


def test_apply_rejects_while_td_running():
    from nanobot_quant import td_live_state
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene()
    name = _save_high_snapshot()
    td_live_state.set_loop(True)
    try:
        with pytest.raises(ValueError, match="TD 循环仍在运行"):
            apply_snapshot(name)
    finally:
        td_live_state.set_loop(False)


def test_apply_rejects_when_scene_enabled():
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene(enabled=True)  # 启用中（即使线程没跑）
    name = _save_high_snapshot()
    with pytest.raises(ValueError, match="TD 循环仍在运行"):
        apply_snapshot(name)


def test_apply_rejects_channel_mismatch():
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene()
    name = _save_high_snapshot()
    ep = load_exec_params()
    ep["execution_channel"] = "gate"  # 快照记录 okx_cex
    ep["scenes"]["high"]["enabled"] = False
    save_exec_params(ep)
    with pytest.raises(ValueError, match="执行通道不一致"):
        apply_snapshot(name)


def test_apply_unknown_snapshot():
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene()
    with pytest.raises(ValueError, match="不存在"):
        apply_snapshot("no_such_snapshot")


def test_apply_restores_values_backs_up_and_keeps_scene_disabled():
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import apply_snapshot, load_user_presets

    _seed_scene()
    name = _save_high_snapshot()

    # 改坏：entry_setup 9 → 7、止盈 2% → 5%
    ep = load_exec_params()
    ep["scenes"]["high"].update({"enabled": False, "entry_setup": 7})
    ep["take_profit_pct"] = 0.05  # 扁平值会覆盖 scenes.high（既有约定）⇒ 改扁平才落盘
    save_exec_params(ep)

    res = apply_snapshot(name)
    assert res["ok"] and res["scene"] == "high"
    assert res["applied"]["entry_setup"] == {"old": 7, "new": 8}
    assert res["applied"]["take_profit_pct"]["new"] == pytest.approx(0.02)
    assert res["backup"].startswith("before_restore_")
    assert res["backup"] in load_user_presets()          # 备份真的落盘

    after = load_exec_params()["scenes"]["high"]
    assert after["entry_setup"] == 8
    assert after["enabled"] is False                     # 恢复后必须由用户显式启动


def test_apply_skips_sub_accounts_by_default_and_writes_when_asked():
    from nanobot_quant.exec_params import load_exec_params
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene(sub_accounts=["DreamShepherdbot2", "DreamShepherdbot3"])
    name = _save_high_snapshot()

    res = apply_snapshot(name)
    assert res["sub_accounts"] == "skipped"
    assert load_exec_params()["scenes"]["high"]["sub_accounts"] == [
        "DreamShepherdbot2",
        "DreamShepherdbot3",
    ]

    res2 = apply_snapshot(name, include_sub_accounts=True)
    assert res2["sub_accounts"] == "written"
    assert res2["applied"]["sub_accounts"]["new"] == [
        "DreamShepherdbot2",
        "DreamShepherdbot3",
    ]


def test_apply_reports_global_fields_without_writing_them():
    from nanobot_quant.exec_params import load_exec_params
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene()
    name = _save_high_snapshot()
    res = apply_snapshot(name)
    # fee_rate / slippage 是跨场景字段：只报告、不写
    assert "fee_rate" in res["global_skipped"]
    assert "fee_rate" not in res["applied"]


def test_snapshot_puts_symbols_in_overrides():
    """标的池必须进 overrides（自包含），否则实盘恢复读不到（2026-11-07 修）。"""
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import build_snapshot

    _seed_scene()
    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL", "BNB", "BTC"]
    ep["td_symbols"] = ["SOL", "BNB", "BTC"]
    save_exec_params(ep)

    snap = build_snapshot("high")
    assert snap["overrides"]["symbols"] == ["SOL", "BNB", "BTC"]
    assert snap["run"]["symbols"] == ["SOL", "BNB", "BTC"]  # 展示用，保留


def test_apply_restores_symbols():
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_scene()
    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL", "BNB", "BTC"]
    ep["td_symbols"] = ["SOL", "BNB", "BTC"]
    save_exec_params(ep)
    name = _save_high_snapshot("live_high_symbols")

    # 改坏：只剩 SOL
    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL"]
    ep["td_symbols"] = ["SOL"]
    save_exec_params(ep)

    res = apply_snapshot(name)
    assert res["applied"]["symbols"] == {"old": ["SOL"], "new": ["SOL", "BNB", "BTC"]}
    after = load_exec_params()
    assert after["scenes"]["high"]["symbols"] == ["SOL", "BNB", "BTC"]
    assert after["td_symbols"] == ["SOL", "BNB", "BTC"]  # 扁平键同步


def test_apply_restores_symbols_from_legacy_run_only():
    """旧快照（overrides 里没有 symbols）走 run.symbols 回退，不能静默丢标的。"""
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import apply_snapshot, build_snapshot, save_user_preset

    _seed_scene()
    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL", "ETH"]
    ep["td_symbols"] = ["SOL", "ETH"]
    save_exec_params(ep)

    snap = build_snapshot("high")
    snap["overrides"].pop("symbols", None)          # 模拟第 2 批保存的旧快照
    assert snap["run"]["symbols"] == ["SOL", "ETH"]
    save_user_preset("legacy_snapshot", snap)

    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL"]
    ep["td_symbols"] = ["SOL"]
    save_exec_params(ep)

    apply_snapshot("legacy_snapshot")
    assert load_exec_params()["scenes"]["high"]["symbols"] == ["SOL", "ETH"]


def test_apply_never_restores_batches():
    """batches 与台账 slot 强绑定 ⇒ 明确不恢复（改了会触发拒绝重建）。"""
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import apply_snapshot, build_snapshot, save_user_preset

    _seed_scene()
    ep = load_exec_params()
    ep["scenes"]["high"]["batches"] = 10
    ep["td_batches"] = 10
    save_exec_params(ep)
    snap = build_snapshot("high")
    save_user_preset("batch_snapshot", snap)

    ep = load_exec_params()
    ep["scenes"]["high"]["batches"] = 3
    ep["td_batches"] = 3
    save_exec_params(ep)

    res = apply_snapshot("batch_snapshot")
    assert "batches" not in res["applied"]
    assert load_exec_params()["scenes"]["high"]["batches"] == 3  # 保持人工设定


# ---------------------------------------------------------------- 内置预设应用

def _seed_okx():
    from nanobot_quant.exec_params import DEFAULT_SCENES, save_exec_params

    sc = dict(DEFAULT_SCENES["high"])
    sc.update({"enabled": False, "symbols": ["SOL"], "batches": 4, "td_fixed_amount": 4.0,
               "entry_setup": 9, "entry_periods": "1", "eod_enabled": False})
    save_exec_params({"execution_channel": "okx_cex", "scenes": {"high": sc}})


def test_preview_builtin_separates_changes_hints_skipped():
    """内置预设：决策参数进 changes，标的/批次/单仓只进 hints，跨场景进 skipped。"""
    from nanobot_quant.exec_presets import preview_preset

    _seed_okx()
    pv = preview_preset("low9_multi_h9_minentry_eod_20261106", kind="builtin")

    changed = {c["key"] for c in pv["changes"]}
    assert "entry_periods" in changed and "eod_enabled" in changed
    assert "fixed_amount" not in changed and "symbols" not in changed and "batches" not in changed

    hints = {h["key"]: h for h in pv["hints"]}
    assert set(hints) == {"symbols", "batches", "fixed_amount"}
    assert hints["symbols"]["preset"] == ["ZEC", "NEAR", "DASH"]
    assert hints["fixed_amount"]["preset"] == 200.0 and hints["fixed_amount"]["current"] == 4.0

    skipped = {s["key"] for s in pv["skipped"]}
    assert "fee_rate" in skipped or "slippage" in skipped
    assert not pv["warnings"]  # 通道一致（okx_cex）


def test_preview_warns_on_channel_mismatch():
    from nanobot_quant.exec_params import DEFAULT_SCENES, save_exec_params
    from nanobot_quant.exec_presets import preview_preset

    save_exec_params({"execution_channel": "okx_dex", "scenes": {"high": dict(DEFAULT_SCENES["high"])}})
    pv = preview_preset("low9_multi_h9_minentry_eod_20261106", kind="builtin")
    assert pv["warnings"] and "okx_dex" in pv["warnings"][0]


def test_apply_builtin_writes_params_but_never_symbols_batches_amount():
    from nanobot_quant.exec_params import load_exec_params
    from nanobot_quant.exec_presets import apply_snapshot

    _seed_okx()
    res = apply_snapshot("low9_multi_h9_minentry_eod_20261106", kind="builtin")
    after = load_exec_params()["scenes"]["high"]

    # 决策参数写入
    assert after["entry_periods"] == "1,2,3,4,5,8"
    assert after["eod_enabled"] is True
    assert after["exit_order"] == "min_entry"
    # 三项用户自填项保持不变
    assert after["symbols"] == ["SOL"]
    assert after["batches"] == 4
    assert after["td_fixed_amount"] == 4.0
    assert "symbols" not in res["applied"]
    assert after["enabled"] is False  # 应用后必须由用户重新启用


def test_preview_user_snapshot_keeps_symbols_in_changes():
    """用户快照的标的池仍属"将写入"（与内置预设的只提示区分）。"""
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import build_snapshot, preview_preset, save_user_preset

    _seed_okx()
    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL", "ETH"]
    ep["td_symbols"] = ["SOL", "ETH"]
    save_exec_params(ep)
    save_user_preset("snap_syms", build_snapshot("high"))

    ep = load_exec_params()
    ep["scenes"]["high"]["symbols"] = ["SOL"]
    ep["td_symbols"] = ["SOL"]
    save_exec_params(ep)

    pv = preview_preset("snap_syms", kind="user")
    keys = {c["key"] for c in pv["changes"]}
    assert "symbols" in keys
    assert not pv["hints"]


def test_preview_numeric_string_equivalence():
    """'610' 与 610 视为同值，不应记为一次改动。"""
    from nanobot_quant.exec_params import load_exec_params, save_exec_params
    from nanobot_quant.exec_presets import build_snapshot, preview_preset, save_user_preset

    _seed_okx()
    ep = load_exec_params()
    ep["scenes"]["high"]["entry_ema_period"] = "610"
    save_exec_params(ep)
    save_user_preset("snap_str", build_snapshot("high"))

    pv = preview_preset("snap_str", kind="user")
    assert "entry_ema_period" not in {c["key"] for c in pv["changes"]}
