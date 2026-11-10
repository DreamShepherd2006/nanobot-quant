"""持仓入场快照与浮盈极值（2026-11-10，docs §33.64 C136）。

覆盖：
- ``BatchManager.open_lot`` 新增的 5 个可选字段（只在传入时写入 →
  旧台账字节级不变）
- 策略侧三个取值 helper（入场信号来源 / 触发周期 / 手续费折算 USD）
- EMA 快照（门关闭不计算、门开启记录 ``_cur_ema``）
- 平仓事件附建仓以来最高/最低浮盈
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from nanobot_quant.batches import BatchManager
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ────────────────────────── open_lot 字段写入 ──────────────────────────

def _bm(tmp_path):
    return BatchManager("SOL", ["bt-s1", "bt-s2"], path=tmp_path / "batches.json")


def test_open_lot_records_entry_meta(tmp_path):
    bm = _bm(tmp_path)
    lot = bm.open_lot(
        qty=0.05, entry_price=86.03, entry_time="2026-11-10T05:00:00", slot=1,
        entry_reason="setup_buy=9", entry_period=2,
        entry_ema_period=610, entry_ema_value=86.31, fee_usd=0.0037,
    )["lot"]
    assert lot["entry_reason"] == "setup_buy=9"
    assert lot["entry_period"] == 2
    assert lot["entry_ema_period"] == 610
    assert pytest.approx(lot["entry_ema_value"]) == 86.31
    assert pytest.approx(lot["fee_usd"]) == 0.0037
    # 落盘可读回（open_lot 自身不写盘，需显式 save）
    bm.save()
    data = json.loads((tmp_path / "batches.json").read_text())
    saved = [s for s in data["slots"] if s["slot"] == 1][0]["lot"]
    assert saved["entry_reason"] == "setup_buy=9"


def test_open_lot_omits_fields_when_not_given(tmp_path):
    """不传任何新参数时不落字段——旧台账/DEX 路径零影响。"""
    bm = _bm(tmp_path)
    lot = bm.open_lot(qty=1.0, entry_price=10.0, entry_time="2026-11-10T05:00:00", slot=1)["lot"]
    for key in ("entry_reason", "entry_period", "entry_ema_period",
                "entry_ema_value", "fee_usd"):
        assert key not in lot, key


# ────────────────────────── 策略 helper ──────────────────────────

def _mk(**over):
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL"
    s.parameters = {"live_mode": False}
    s._fee_rate = 0.001
    s._cur_ema = {}
    s._pos_extremes = {}
    s._entry_ema_enabled = True
    s._entry_ema_period = 610
    s._entry_ema_offset_pct = 1.3
    for k, v in over.items():
        setattr(s, k, v)
    return s


class _Order:
    def __init__(self, custom_params=None):
        self.custom_params = custom_params


def test_entry_reason_prefers_setup():
    s = _mk()
    assert s._entry_reason_of(9, 0, 9, 13) == "setup_buy=9"
    # setup 累加期同样记实际计数
    assert s._entry_reason_of(11, 0, 9, 13) == "setup_buy=11"
    # setup 未达阈值 → 归 cd 通路
    assert s._entry_reason_of(3, 13, 9, 13) == "cd_buy=13"


def test_cex_fee_usd_quote_ccy():
    s = _mk()
    assert s._cex_fee_usd(_Order({"cex": {"fee": 0.0037, "fee_ccy": "USDC"}})) == pytest.approx(0.0037)
    assert s._cex_fee_usd(_Order({"cex": {"fee": -0.0037, "fee_ccy": "usdt"}})) == pytest.approx(0.0037)


def test_cex_fee_usd_base_ccy_converted_by_avg_price():
    s = _mk()
    o = _Order({"cex": {"fee": 0.0001, "fee_ccy": "SOL", "avg_price": 86.0}})
    assert s._cex_fee_usd(o) == pytest.approx(0.0086)


def test_cex_fee_usd_missing_returns_none():
    s = _mk()
    assert s._cex_fee_usd(_Order({"cex": {"avg_price": 86.0}})) is None
    assert s._cex_fee_usd(_Order({})) is None
    assert s._cex_fee_usd(None) is None


def test_ema_snapshot_disabled_returns_none_and_skips_math():
    s = _mk(_entry_ema_enabled=False)
    df = pd.DataFrame({"Close": [1.0, 2.0, 3.0]})
    assert s._ema_snapshot(df) == (None, None)
    assert s._cur_ema == {}  # 门关闭不计算、不缓存


def test_ema_snapshot_records_cur_ema():
    s = _mk()
    df = pd.DataFrame({"Close": [100.0 + i for i in range(10)]})
    n, val = s._ema_snapshot(df)
    assert n == 610 and val > 0
    assert s._cur_ema["SOL"] == (610, val)


def test_ema_snapshot_bad_df_returns_none():
    s = _mk()
    assert s._ema_snapshot(pd.DataFrame({"Close": []})) == (None, None)
    assert s._ema_snapshot(pd.DataFrame({"x": [1.0]})) == (None, None)


def test_exit_pnl_fields_include_extremes():
    s = _mk()
    s._pos_extremes[("SOL", 2)] = {"max": 0.012, "min": -0.081}
    out = s._exit_pnl_fields({
        "slot": 2, "entry_price": 86.0, "actual_price": 87.0, "qty": 0.05,
    })
    assert out["max_pnl_pct"] == 0.012
    assert out["min_pnl_pct"] == -0.081
    # 净收益率仍按原口径
    assert out["pnl_net_ratio"] == pytest.approx(87.0 * 0.999 / 86.0 - 1.0)


def test_exit_pnl_fields_without_extremes_unchanged():
    s = _mk()
    out = s._exit_pnl_fields({"slot": 9, "entry_price": 86.0, "actual_price": 87.0})
    assert "max_pnl_pct" not in out and "min_pnl_pct" not in out
