"""规则预设（C118）/ 覆盖参数校验（C119）单测。

盯的是两件容易静默失效的事：

1. **preset 必须自包含** —— 缺一个键就会回退场景值，跑出来与参数表不符；
2. **参数入口不能分叉** —— 顶层字段（quantity_mode）、overrides、preset
   三条路最后必须汇成同一个决定，且非法值 fail-closed（不能当默认分支跑）。
"""

from __future__ import annotations

import threading

import pytest

from nanobot_quant.backtest.presets import (
    REQUIRED_OVERRIDE_KEYS,
    expand_preset,
    get_preset,
    preset_names,
    presets_full,
    presets_meta,
)
from nanobot_quant.tools import tools_backtest as tb


# ── preset 表本身 ────────────────────────────────────────────────────────
def test_preset_is_self_contained():
    """每个已注册 preset 都必须覆盖全部决策键（缺键 = 静默回退场景值）。"""
    for name in preset_names():
        p = get_preset(name)
        missing = REQUIRED_OVERRIDE_KEYS - set(p["overrides"])
        assert not missing, f"{name} 缺键: {sorted(missing)}"


def test_unknown_preset_fails_closed():
    with pytest.raises(ValueError) as ei:
        get_preset("no_such_preset")
    assert "未知 preset" in str(ei.value)


def test_presets_meta_and_full_shapes():
    meta = presets_meta()
    assert meta and {"name", "version", "label"} <= set(meta[0])
    assert "overrides" not in meta[0]  # 下拉只要轻量元信息

    full = presets_full()
    assert full and full[0]["overrides"] and full[0]["run"]


def test_expand_preset_explicit_override_wins():
    merged, meta = expand_preset("low9_multi_h9_minentry_eod", {"entry_setup": 8})
    assert merged["entry_setup"] == 8
    # 未被覆盖的键仍来自 preset
    assert merged["exit_order"] == "min_entry"
    assert meta["overridden"] == {"entry_setup": 8}
    assert meta["run"]["shared_cash"] is True


def test_expand_preset_no_diff_when_same_value():
    base = get_preset("low9_multi_h9_minentry_eod")["overrides"]
    _, meta = expand_preset("low9_multi_h9_minentry_eod", {"entry_setup": base["entry_setup"]})
    assert meta["overridden"] == {}


# ── 参数归一化（C119）───────────────────────────────────────────────────
def test_coerce_enum_rejects_illegal_value():
    with pytest.raises(ValueError) as ei:
        tb.coerce_overrides({"eod_parity": "evan"})  # 拼错不能被当 even 跑
    assert "eod_parity" in str(ei.value) and "even" in str(ei.value)


def test_coerce_enum_accepts_valid_values():
    for k, v in {
        "quantity_mode": "fixed_amount",
        "exit_order": "min_entry",
        "eod_parity": "both",
        "eod_mode": "profit_only",
    }.items():
        assert tb.coerce_overrides({k: v})[k] == v


def test_coerce_bool_string_false_is_false():
    """entry_periods_partial / tdst_filter 必须走 bool 表：字符串 \"false\"
    若原样透传，策略侧 `if parameters.get(...)` 会当真实值处理。"""
    out = tb.coerce_overrides({"entry_periods_partial": "false", "tdst_filter": "off"})
    assert out["entry_periods_partial"] is False
    assert out["tdst_filter"] is False


def test_coerce_float_new_keys():
    out = tb.coerce_overrides({"fee_rate": "0.001", "gate_red_min": "0.45"})
    assert out["fee_rate"] == pytest.approx(0.001)
    assert out["gate_red_min"] == pytest.approx(0.45)


def test_required_keys_cover_new_controls():
    """页面上能改的键，preset 必须能表达（防止新参数只加在 UI）。"""
    for k in ("tdst_filter", "fee_rate", "exit_first_cross", "quantity_mode",
              "shared_cash" if False else "eod_parity"):
        assert k in REQUIRED_OVERRIDE_KEYS, f"{k} 不在 preset 必填清单里"


# ── run_backtest 入口：preset 的 run 段兜底 + 顶层 quantity_mode ─────────
def _capture_driver_call(monkeypatch):
    seen: dict = {}
    done = threading.Event()

    def fake(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        done.set()

    monkeypatch.setattr(tb, "_auto_backtest_driver", fake)
    return seen, done


def test_preset_fills_run_level_params(monkeypatch):
    seen, done = _capture_driver_call(monkeypatch)
    res = tb.run_backtest(engine="driver", preset="low9_multi_h9_minentry_eod")
    assert done.wait(10), f"driver 未启动: {res}"
    (run_id, scene, syms, start, end, initial_quote, batches, slippage,
     fixed_amount, overrides, source, periods, shared_cash, preset_meta) = seen["args"]
    assert scene == "high"
    assert syms == ["ZEC", "NEAR", "DASH"]
    assert start == "2026-08-01" and end == "2026-10-31"
    assert initial_quote == pytest.approx(200.0)
    assert batches == 50
    assert slippage == pytest.approx(0.0)
    assert fixed_amount == pytest.approx(200.0)
    assert shared_cash is True
    assert source == "okx_cex"
    assert periods == ["1m"]
    assert overrides["entry_ema_enabled"] is True
    assert overrides["quantity_mode"] == "fixed_amount"
    assert preset_meta["name"] == "low9_multi_h9_minentry_eod"
    assert preset_meta["version"] == "v1"


def test_preset_run_level_params_can_be_overridden(monkeypatch):
    seen, done = _capture_driver_call(monkeypatch)
    tb.run_backtest(
        engine="driver",
        preset="low9_multi_h9_minentry_eod",
        symbols=["SOL"],
        start="2026-11-01",
        initial_quote=1000.0,
        shared_cash=False,
    )
    assert done.wait(10)
    args = seen["args"]
    assert args[2] == ["SOL"]          # 页面填了就以页面为准
    assert args[3] == "2026-11-01"
    assert args[5] == pytest.approx(1000.0)
    assert args[12] is False


def test_top_level_quantity_mode_is_applied(monkeypatch):
    """C119①：顶层 quantity_mode 不能被静默忽略。"""
    seen, done = _capture_driver_call(monkeypatch)
    tb.run_backtest(engine="driver", quantity_mode="fixed_amount")
    assert done.wait(10)
    assert seen["args"][9]["quantity_mode"] == "fixed_amount"


def test_top_level_quantity_mode_illegal_fails_closed():
    res = tb.run_backtest(engine="driver", quantity_mode="bogus")
    assert "error" in res and "quantity_mode" in res["error"]


def test_preset_with_bad_override_returns_error(monkeypatch):
    _capture_driver_call(monkeypatch)
    res = tb.run_backtest(engine="driver", preset="low9_multi_h9_minentry_eod",
                          overrides={"eod_parity": "wild"})
    assert "error" in res and "eod_parity" in res["error"]


def test_unknown_preset_returns_error(monkeypatch):
    _capture_driver_call(monkeypatch)
    res = tb.run_backtest(engine="driver", preset="nope")
    assert "error" in res and "未知 preset" in res["error"]


# ── 页面 / 快照可观测性 ─────────────────────────────────────────────────
def test_page_has_preset_control_and_binds_it():
    from nanobot_quant.backtest_handlers import _load_template

    html = _load_template("backtest_page.html")
    assert 'id="preset"' in html
    assert "__PRESETS__" in html
    assert "applyPreset" in html
    # 预设必须随请求提交，否则后端展开的 preset 与页面显示无关
    assert 'preset: $("preset")' in html
    # 富途变体必需的两个参数控件（缺了就只能在 preset 里"隐式"生效）
    assert 'id="tdst_filter"' in html
    assert 'id="fee_rate"' in html


def test_driver_config_records_preset_and_shared_cash():
    import inspect

    from nanobot_quant.backtest import driver as drv

    src = inspect.getsource(drv)
    for marker in ('"preset": self.preset_meta.get("name")',
                   '"preset_version": self.preset_meta.get("version")',
                   '"preset_overrides": dict(',
                   '"shared_cash": bool(self.shared_cash)'):
        assert marker in src, f"backtest_config 缺 {marker}"


def test_driver_config_records_visibility_keys():
    """上线实测发现的两个静默项（2026-11-06）：都已生效、但快照里没有。

    - ``tdst_filter``：富途变体必须关，开了会永久拦入场，看不到就无从确认；
    - ``sleeptime``：只有 ``timestep``，场景周期不可见；
    - 运行时 ``entry_periods``：只列非 base 档，「1:<根数>」缺失 ⇒
      无法核对多周期预热是否被数据源深度截断。
    """
    import inspect

    from nanobot_quant.backtest import driver as drv

    src = inspect.getsource(drv)
    for marker in ('"tdst_filter": bool(',
                   '"sleeptime": str(self.scene_cfg.get("sleeptime"',
                   "1: len(net_values),"):
        assert marker in src, f"缺 {marker}"


def test_mcp_exposes_list_backtest_presets():
    from nanobot_quant import signal_mcp_server as srv

    assert "list_backtest_presets" in srv._TOOL_DISPATCH
    out = srv._TOOL_DISPATCH["list_backtest_presets"]()
    assert out["presets"] and out["presets"][0]["name"]
