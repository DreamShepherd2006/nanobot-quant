"""两臂对照（EMA 闸门开 vs 关）摘要计算 + 参数白名单（2026-10-20）。

设计要点：两臂跑同一段行情、同一批信号，是**两条路径**而不是两个独立
样本 ⇒ 结果只能展示，禁配对推断、不得把差值当闸门效应量（C73 纪律）。
这里锁住两件事：① 摘要算术；② `ema_compare_baseline` 能被 overrides 接收。
"""
import pytest

from nanobot_quant import backtest_handlers as bh
from nanobot_quant.tools.tools_backtest import _arm_summary, _arms_compare


def _res(roi, fills, skip=None, eg=None):
    return {
        "roi": roi,
        "final_net": 1000.0 * (1 + roi),
        "fills": fills,
        "skip_counts": skip or {},
        "ema_gate": eg or {},
    }


def test_arm_summary_extracts_comparable_fields():
    s = _arm_summary(
        _res(0.0123, 8, {"ema_gate": 3}, {"pass": 5, "block": 3, "na": 1})
    )
    assert s["roi_pct"] == pytest.approx(1.23)
    assert s["final_net"] == pytest.approx(1012.3)
    assert s["fills"] == 8
    assert s["ema_pass"] == 5 and s["ema_block"] == 3 and s["ema_na"] == 1
    assert s["skip_total"] == 3


def test_arm_summary_tolerates_missing_fields():
    s = _arm_summary({})
    assert s["roi_pct"] == 0.0
    assert s["fills"] is None
    assert s["skip_total"] == 0


def test_arms_compare_deltas_and_no_paired_inference_note():
    gated = _res(0.0123, 8, {"ema_gate": 3}, {"pass": 5, "block": 3})
    base = _res(-0.004, 12)
    out = _arms_compare(gated, base)
    assert out["delta_roi_pct"] == pytest.approx(1.63)
    assert out["delta_fills"] == -4
    # 结论纪律写进结果本身，避免被当成效应量引用
    assert "禁配对推断" in out["note"]
    assert "非独立样本" in out["note"]


def test_overrides_accept_two_arm_flag():
    assert bh._coerce_overrides({"ema_compare_baseline": "on"}) == {
        "ema_compare_baseline": True
    }
    assert bh._coerce_overrides({"ema_compare_baseline": False}) == {
        "ema_compare_baseline": False
    }
