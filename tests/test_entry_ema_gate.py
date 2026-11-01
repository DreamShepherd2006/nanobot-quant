"""EMA 折价入场门（2026-11-01）测试。

语义：``entry_ema_enabled=True`` 时，BUY 额外要求「收盘 ≤ EMA_n × (1 − 偏移%)」——
只在价格跌到长期均线下方才买 9 建仓。默认关 → 零行为变化。

覆盖：
- 默认关闭：BUY 照常（零行为变化）
- 开启 + 价格在 EMA 上方（强趋势后回调）→ 拦 BUY，计 entry_ema skip
- 开启 + 价格跌破 EMA 超过偏移 → 放行
- ``_ema_fetch_len()`` = period + 5τ（关闭时 0）
- 场景参数注入与全局回退
- 只拦 BUY：SELL 路径不受门影响（结构性）
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from nanobot_quant.environment.sensors import ema_warmup_bars
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _bars(closes: list[float]) -> "object":
    from lumibot.entities import Bars

    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.002 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="min"),
    )
    return Bars(df, "GATE", None)


def _bars_above_ema() -> "object":
    """长期上涨（EMA 滞后）后 12 根下跌 → 买9，但收盘价仍远高于 EMA。"""
    closes = [100.0 + i * 10 for i in range(41)]  # 100 → 500
    closes += [500.0 - i for i in range(1, 13)]   # 499 → 488
    return _bars(closes)


def _bars_below_ema() -> "object":
    """长期下跌（EMA 滞后在上方）+ 横盘重置 setup + 末段 buy9 → 价格远低于 EMA。"""
    closes = [500.0 - i * 10 for i in range(41)]   # 500 → 100（建立高位 EMA）
    closes += [100.0] * 45                          # 横盘：让 setup 归零
    closes += [100.0 - i for i in range(1, 13)]     # 99 → 88（重新数到买9）
    return _bars(closes)


def _mock_order(quantity=1.0):
    """lumibot Order 最小 stub（镜像 v4.5.78：custom_params 默认 None）。"""
    return type("Order", (), {
        "identifier": "mock-id",
        "quantity": quantity,
        "error": None,
        "status": "unsubmitted",
        "custom_params": None,
        "is_filled": lambda self=None: False,
        "set_error": lambda self, e: setattr(self, "error", e),
    })()


def _make_strategy(bars, **params) -> TdSequentialStrategy:
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-entry-ema-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = bars
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars

    captured: dict = {}

    def _create_order(asset, quantity, action):
        captured["order"] = (asset, quantity, action)
        return _mock_order(quantity=quantity)

    s.create_order = _create_order
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = None  # 非分批模式 → 走全局下单分支
    s.initialize()
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9
    s._home_account = "acc-home"
    s._captured = captured
    return s


def _activate(s, scene_params: dict) -> None:
    s.broker = object()
    s._activate_scene("high", {"params": scene_params})


# ── 默认关：零行为变化 ───────────────────────────────────────────


def test_default_off_allows_buy():
    s = _make_strategy(_bars_above_ema())
    s._evaluate_symbol()
    assert "order" in s._captured, "默认关闭时 BUY 必须照常（零行为变化）"
    assert s._skip_counts.get("entry_ema", 0) == 0


def test_default_values():
    from nanobot_quant.exec_params import DEFAULT_SCENES, PARAM_META

    assert TdSequentialStrategy.parameters["entry_ema_enabled"] is False
    assert TdSequentialStrategy.parameters["entry_ema_period"] == 610
    assert TdSequentialStrategy.parameters["entry_ema_offset_pct"] == 0.1
    for sc in ("high", "mid", "low"):
        assert DEFAULT_SCENES[sc]["entry_ema_enabled"] is False
        assert str(DEFAULT_SCENES[sc]["entry_ema_period"]) == "610"
        assert DEFAULT_SCENES[sc]["entry_ema_offset_pct"] == 0.1
    # 下拉必须是完整斐波那契族（从 1 开始，用户 2026-11-01 要求）
    enum = PARAM_META["entry_ema_period"]["enum"]
    assert enum[0] == "1", "下拉应从 1 开始"
    for need in ("1", "2", "3", "5", "8", "13", "21", "34", "55", "89",
                 "144", "233", "377", "610", "987", "1597"):
        assert need in enum, f"斐波那契族缺 {need}"
    # 默认值必须在下拉选项内，否则保存会被校验拒绝
    assert str(PARAM_META["entry_ema_period"]["std"]) in enum


# ── 门控行为 ─────────────────────────────────────────────────────


def test_blocks_buy_when_price_above_ema():
    """强趋势后回调：买9 成立，但价格仍在 EMA21 上方 → 拦。"""
    s = _make_strategy(_bars_above_ema())
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "21",
                  "entry_ema_offset_pct": 0.1})
    s._evaluate_symbol()
    assert "order" not in s._captured, "价格在 EMA 上方时应拦 BUY"
    assert s._skip_counts.get("entry_ema", 0) >= 1


def test_allows_buy_when_price_below_ema():
    """长期下跌：价格远低于 EMA21 → 放行。"""
    s = _make_strategy(_bars_below_ema())
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "21",
                  "entry_ema_offset_pct": 0.1})
    s._evaluate_symbol()
    assert "order" in s._captured, "价格跌破 EMA 后应放行 BUY"


def test_offset_boundary():
    """偏移放大后，原本放行的边界单应被拦。"""
    s = _make_strategy(_bars_above_ema())
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "21",
                  "entry_ema_offset_pct": 0.0})
    s._evaluate_symbol()
    blocked_zero = "order" not in s._captured

    s2 = _make_strategy(_bars_above_ema())
    _activate(s2, {"entry_ema_enabled": True, "entry_ema_period": "21",
                   "entry_ema_offset_pct": 5.0})
    s2._evaluate_symbol()
    blocked_big = "order" not in s2._captured
    assert (not blocked_zero) or blocked_big, "偏移更大不应反而放行"


# ── 取数窗口 ─────────────────────────────────────────────────────


def test_ema_fetch_len_off_is_zero():
    s = _make_strategy(_bars_above_ema())
    _activate(s, {"entry_ema_enabled": False})
    assert s._ema_fetch_len() == 0


def test_ema_fetch_len_on_uses_period_plus_warmup():
    s = _make_strategy(_bars_above_ema())
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "144"})
    assert s._ema_fetch_len() == 144 + ema_warmup_bars(144)


def test_ema_fetch_len_610():
    s = _make_strategy(_bars_above_ema())
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "610"})
    assert s._ema_fetch_len() == 610 + ema_warmup_bars(610) == 610 + 1527


# ── 场景注入与全局回退 ───────────────────────────────────────────


def test_scene_params_override_global():
    s = _make_strategy(_bars_above_ema(), entry_ema_enabled=False)
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "89",
                  "entry_ema_offset_pct": 0.2})
    assert s._entry_ema_enabled is True
    assert s._entry_ema_period == 89
    assert s._entry_ema_offset_pct == pytest.approx(0.2)


def test_scene_falls_back_to_global_when_missing():
    s = _make_strategy(_bars_above_ema(), entry_ema_enabled=True,
                       entry_ema_period=377, entry_ema_offset_pct=0.3)
    _activate(s, {})
    assert s._entry_ema_enabled is True
    assert s._entry_ema_period == 377
    assert s._entry_ema_offset_pct == pytest.approx(0.3)


# ── 只拦 BUY ────────────────────────────────────────────────────


def test_gate_only_applies_to_buy_signal():
    """门只在 buy_signal 为真时计算：无买9 的 bar 不进 entry_ema_na 计数。"""
    s = _make_strategy(_bars([100.0] * 60))  # 全平 → 无 setup
    _activate(s, {"entry_ema_enabled": True, "entry_ema_period": "21"})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_ema", 0) == 0
    assert s._skip_counts.get("entry_ema_na", 0) == 0


# ── 回测侧接线（预热窗口）─────────────────────────────────────────────
# 回测是「一次性预切数据再逐 bar 回放」，不像实盘每轮动态多拉 ⇒ driver 必须
# 自己把预热算进取数窗口，否则 EMA 还在收敛期、折价值失真且不显眼。

def _bt_params(**over):
    p = {
        "scenes": {
            "high": {
                "enabled": True, "sleeptime": "1m", "symbols": ["SOL"],
                "batches": 1, "sub_accounts": ["gate_bot1"], "entry_setup": 9,
            }
        },
        "td_bars": 300, "fee_rate": 0.001,
    }
    p.update(over)
    return p


def test_driver_warmup_includes_entry_ema_when_enabled():
    """门开启时取数窗口 = max(趋势闸门预热, EMA 折价门预热)。"""
    from nanobot_quant.backtest.driver import BacktestDriver
    from nanobot_quant.environment.sensors import ema_warmup_bars

    d = BacktestDriver(
        scene="high", params=_bt_params(),
        overrides={"entry_ema_enabled": True, "entry_ema_period": 610},
    )
    expect = 610 + ema_warmup_bars(610)          # 610 + 1527 = 2137
    assert d.entry_ema_n == 610
    assert d.entry_ema_warmup == expect
    assert d.warmup_bars >= expect


def test_driver_no_extra_warmup_when_gate_off():
    """门关闭时预热不受影响（零行为变化）。"""
    from nanobot_quant.backtest.driver import BacktestDriver

    off = BacktestDriver(scene="high", params=_bt_params())
    on = BacktestDriver(
        scene="high", params=_bt_params(),
        overrides={"entry_ema_enabled": True, "entry_ema_period": 610},
    )
    assert off.entry_ema_warmup == 0
    assert on.warmup_bars > off.warmup_bars


def test_driver_injects_entry_ema_params_into_strategy():
    """_activate_scene 场景值缺席时，parameters 兜底必须带上门参数。"""
    from pathlib import Path

    src = Path("src/nanobot_quant/backtest/driver.py").read_text()
    for key in ("entry_ema_enabled", "entry_ema_period", "entry_ema_offset_pct"):
        assert f'"{key}":' in src, f"driver 未注入 {key}"


# ── 回测页覆盖入口（A+B 的 B 部分）──────────────────────────────────────
# 面上三个字段：开关 / 周期 / 偏移；默认留空 = 跟随场景配置。

def test_overrides_coerced_for_entry_ema_fields():
    from nanobot_quant.backtest_handlers import _coerce_overrides

    got = _coerce_overrides({
        "entry_ema_enabled": "on",
        "entry_ema_period": "987",
        "entry_ema_offset_pct": "0.5",
    })
    assert got == {
        "entry_ema_enabled": True,
        "entry_ema_period": 987,
        "entry_ema_offset_pct": 0.5,
    }
    assert _coerce_overrides({"entry_ema_enabled": "off"})["entry_ema_enabled"] is False
    assert _coerce_overrides({"entry_ema_period": "", "entry_ema_offset_pct": None}) == {}


def test_scenes_effective_exposes_entry_ema_defaults():
    """页面 placeholder 跟随场景值 ⇒ _effective 必须含这三键。"""
    from nanobot_quant.backtest_handlers import _scenes

    for scene in ("high", "mid", "low"):
        eff = (_scenes().get(scene) or {}).get("_effective") or {}
        for key in ("entry_ema_enabled", "entry_ema_period", "entry_ema_offset_pct"):
            assert key in eff, f"{scene}._effective 缺 {key}"


def test_backtest_page_exposes_entry_ema_controls():
    """结构性锁定：三个输入框 + OV_KEYS 联动 + 结果区生效行。"""
    from pathlib import Path

    html = Path("src/nanobot_quant/backtest_page.html").read_text()
    for el_id in ("entry_ema_enabled", "entry_ema_period", "entry_ema_offset_pct"):
        assert f'id="{el_id}"' in html, f"页面缺输入框 {el_id}"
    # 三个 id 都要进 OV_KEYS（placeholder 才跟随场景）
    block = html.split("const OV_KEYS = [", 1)[1].split("];", 1)[0]
    for key in ("entry_ema_enabled", "entry_ema_period", "entry_ema_offset_pct"):
        assert f'"{key}"' in block, f"OV_KEYS 缺 {key}"
    assert "EMA 折价入场门" in html          # 结果区「实际生效」行
    # 斐波那契 16 档齐全（首项 1）
    for v in (1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597):
        assert f'<option value="{v}">{v}</option>' in html, f"周期下拉缺 {v}"
