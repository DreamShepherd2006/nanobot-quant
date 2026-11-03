"""加仓最小间距闸 `entry_min_gap_pct`（2026-11-03）测试。

语义：该标的已有 open 批次时，新买入价必须比「现有 open 批次里**最低**的
entry_price」再低 >= gap%（price <= min_cost * (1-gap%)）才建仓。
默认 0 = 关闭（零行为变化）；无持仓放行；台账异常 fail-open + 计数；
只拦 BUY（setup 与 cd 两条通道走同一 if 链），SELL/止损/止盈完全不受影响。

动因（11-02 实盘复盘）：缓跌中 setup 反复 9->0 重置，周期门控每次放行；
BTC 5 笔挤在 0.11% 价格带宽内（78107–78197），摊薄 0.06% < 手续费 0.1%。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ── 夹具 ────────────────────────────────────────────────────────────────


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


def _bars_buy9() -> "object":
    """41 根上涨 + 12 根下跌 → 末根买9，收盘 488。"""
    closes = [100.0 + i * 10 for i in range(41)]  # 100 → 500
    closes += [500.0 - i for i in range(1, 13)]   # 499 → 488
    return _bars(closes)


class _FakeBM:
    """批次台账最小替身（只提供本闸需要的接口）。"""

    def __init__(self, costs=None, *, boom: bool = False, slots=None):
        self._costs = list(costs or [])
        self._boom = boom
        self._slots = list(slots or [])

    def open_slots(self):
        if self._boom:
            raise RuntimeError("台账读取失败（模拟 I/O 抖动）")
        return [
            {"slot": i + 1, "status": "open",
             "lot": {"qty": 1.0, "entry_price": c}}
            for i, c in enumerate(self._costs)
        ]

    def any_open(self):
        return bool(self._costs)

    def check_exit(self, *a, **k):
        return []

    def pick_exit_slot(self, *a, **k):
        return None

    def scan_buy_slots(self, start=1):
        return self._slots  # 空 = 无可用 slot（本测试不关心下单）


def _mock_order(quantity=1.0):
    return type("Order", (), {
        "identifier": "mock-id",
        "quantity": quantity,
        "error": None,
        "status": "unsubmitted",
        "custom_params": None,
        "is_filled": lambda self=None: False,
        "set_error": lambda self, e: setattr(self, "error", e),
    })()


def _make_strategy(bars, bm=None, **params) -> TdSequentialStrategy:
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("td-entry-gap-test")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s._bars = bars
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars
    captured: dict = {}
    s.create_order = lambda asset, quantity, action: (
        captured.update(order=(asset, quantity, action)) or _mock_order(quantity)
    )
    s.submit_order = lambda order: captured.setdefault("submitted", order)
    s.batch_manager = bm
    s.initialize()
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9
    s._home_account = "acc-home"
    s._buy_on_slot = lambda *a, **k: "stubbed-slot-buy"
    s._captured = captured
    return s


def _activate(s, scene_params: dict) -> None:
    s.broker = object()
    s._activate_scene("high", {"params": scene_params})


# ── 默认值 / 零行为变化 ────────────────────────────────────────────────


def test_default_values():
    from nanobot_quant.exec_params import DEFAULT_SCENES, PARAM_META

    assert TdSequentialStrategy.parameters["entry_min_gap_pct"] == 0.0
    for sc in ("high", "mid", "low"):
        assert DEFAULT_SCENES[sc]["entry_min_gap_pct"] == 0.0, sc
    meta = PARAM_META["entry_min_gap_pct"]
    assert meta["group"] == "scene"
    assert meta["min"] == 0.0 and meta["max"] == 10.0
    assert meta["std"] == 0.0, "默认必须为 0（零行为变化）"


def test_scene_only_fields_include_gap():
    from nanobot_quant.exec_params import SCENE_ONLY_FIELDS, SCENE_FIELD_ORDER

    assert "entry_min_gap_pct" in SCENE_ONLY_FIELDS
    assert "entry_min_gap_pct" in SCENE_FIELD_ORDER


def test_default_off_never_blocks():
    """gap=0（默认）+ 有持仓且价格远高于成本 → 依然放行（零行为变化）。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[100.0]))
    _activate(s, {})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 0
    assert s._skip_counts.get("entry_gap_na", 0) == 0


# ── 拦截 / 放行 ────────────────────────────────────────────────────────


def test_blocks_when_price_above_threshold():
    """成本 450、gap=0.3% → 门槛 448.65；收盘 488 明显高于门槛 → 拦。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[450.0]))
    _activate(s, {"entry_min_gap_pct": 0.3})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 1
    assert "order" not in s._captured, "被拦时不得下单"


def test_allows_when_price_below_threshold():
    """成本 500、gap=0.3% → 门槛 498.5；收盘 488 <= 门槛 → 放行。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[500.0]))
    _activate(s, {"entry_min_gap_pct": 0.3})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 0


def test_boundary_equal_price_passes():
    """正好等于门槛 → 放行（price <= need 即通过）。"""
    need = 488.0 / 0.997  # 使 488 == min_cost*(1-0.3%)
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[need]))
    _activate(s, {"entry_min_gap_pct": 0.3})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 0


def test_no_position_allows_first_entry():
    """无 open 批次 → 首笔不受限。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[]))
    _activate(s, {"entry_min_gap_pct": 5.0})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 0
    assert s._skip_counts.get("entry_gap_na", 0) == 0


def test_non_batch_mode_allows():
    """batch_manager=None（非分批）→ 无成本可比 → 放行。"""
    s = _make_strategy(_bars_buy9(), bm=None)
    _activate(s, {"entry_min_gap_pct": 5.0})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 0


# ── 基准口径：最低成本，不是最高 / 不是上一笔 ──────────────────────────


def test_uses_min_cost_not_max_cost():
    """成本 [500, 450]：用最低 450 → 门槛 448.65 → 拦；
    若错用最高 500 → 门槛 498.5 → 会放行。以「拦」证明取最低。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(costs=[500.0, 450.0]))
    _activate(s, {"entry_min_gap_pct": 0.3})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap", 0) == 1


def test_min_open_cost_helper():
    s = _make_strategy(_bars_buy9())
    assert s._min_open_cost(None) is None
    assert s._min_open_cost(_FakeBM(costs=[])) is None
    assert s._min_open_cost(_FakeBM(costs=[500.0, 450.0, 470.0])) == 450.0
    # 无有效 entry_price 的批次被忽略
    bm = _FakeBM()
    bm.open_slots = lambda: [
        {"slot": 1, "lot": {"qty": 1.0, "entry_price": 0}},
        {"slot": 2, "lot": {"qty": 1.0, "entry_price": 460.0}},
    ]
    assert s._min_open_cost(bm) == 460.0


# ── 异常路径：fail-open + 计数 ─────────────────────────────────────────


def test_ledger_error_fails_open():
    """台账读取异常 → 放行 + entry_gap_na 计数（数据不足不拦单）。"""
    s = _make_strategy(_bars_buy9(), bm=_FakeBM(boom=True))
    _activate(s, {"entry_min_gap_pct": 0.3})
    s._evaluate_symbol()
    assert s._skip_counts.get("entry_gap_na", 0) == 1
    assert s._skip_counts.get("entry_gap", 0) == 0


# ── 回测侧接线（死门防线）──────────────────────────────────────────────


def test_backtest_driver_injects_entry_min_gap():
    src = Path("src/nanobot_quant/backtest/driver.py").read_text()
    assert '"entry_min_gap_pct": self.entry_min_gap_pct' in src
    assert "self.entry_min_gap_pct = float(_eg)" in src


def test_overrides_coerced_for_entry_min_gap():
    from nanobot_quant.backtest_handlers import _coerce_overrides

    assert _coerce_overrides({"entry_min_gap_pct": "0.3"}) == {
        "entry_min_gap_pct": 0.3
    }
    assert _coerce_overrides({"entry_min_gap_pct": ""}) == {}
    # 0 是合法值（关闭），不能被当空值丢弃
    assert _coerce_overrides({"entry_min_gap_pct": "0"}) == {"entry_min_gap_pct": 0.0}


def test_scenes_effective_exposes_entry_min_gap():
    from nanobot_quant.backtest_handlers import _scenes

    for scene in ("high", "mid", "low"):
        eff = (_scenes().get(scene) or {}).get("_effective") or {}
        assert "entry_min_gap_pct" in eff, f"{scene}._effective 缺 entry_min_gap_pct"


def test_backtest_page_exposes_entry_min_gap_control():
    html = Path("src/nanobot_quant/backtest_page.html").read_text()
    assert 'id="entry_min_gap_pct"' in html
    block = html.split("const OV_KEYS = [", 1)[1].split("];", 1)[0]
    assert '"entry_min_gap_pct"' in block, "OV_KEYS 缺 entry_min_gap_pct"
    assert "加仓最小间距" in html, "结果区缺「实际生效」行"


def test_driver_snapshot_exposes_entry_min_gap():
    src = Path("src/nanobot_quant/backtest/driver.py").read_text()
    # backtest_config 快照里也要出现（关也要写，不静默省略）
    cfg_block = src.split('"backtest_config"', 1)[1][:6000] if '"backtest_config"' in src else src
    assert '"entry_min_gap_pct"' in cfg_block or '"entry_min_gap_pct": self.' in src


def test_strategy_source_documents_gate():
    """结构性：门判定必须写事件（不能只 print）——EMA 门静默拦截的教训。"""
    src = Path("src/nanobot_quant/strategies/td_sequential_strategy.py").read_text()
    assert 'status="entry_gap_blocked"' in src
    assert "ENTRY GAP BLOCK" in src
    assert "entry_gap_na" in src
