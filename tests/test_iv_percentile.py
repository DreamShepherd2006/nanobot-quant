"""IV 分位（滚动序列 → 分位）单测 —— 零网络。

覆盖：
* 参考档选择（目标带宽内取最近、带宽外退到 ≥最短档、无档 → None）
* 滚动分位（trailing 窗口、只用历史、样本不足 → None、空值不参与）
* 参考 IV（曲面微笑在 K=现货 处插值；曲面/微笑/取值缺失一律 None）
* **接线回归**：``_try_entry`` 必须把分位喂进 ``evaluate_entry`` —— IV 轴曾是
  「死门」（阈值填任何值都不生效、IV 0 与 70 的回测结果逐对相同）
"""

from __future__ import annotations

import types

import pytest

import test_options_driver as T
from nanobot_quant.iv_percentile import (
    DAY_MS,
    min_samples_for,
    percentile_rank,
    pick_ref_expiry,
    ref_atm_iv,
    rolling_percentile,
    window_bars_for,
)

_SIG = T._SIG


# ── 参考档选择 ────────────────────────────────────────────────

def _ms(days: float, t_ms: int = 0) -> int:
    return int(t_ms + days * DAY_MS)


def test_pick_ref_expiry_prefers_target_inside_band():
    """带宽内有 2 天与 7.2 天 → 目标 7 天应选 7.2 天（周线），不是最近的日到期。"""
    t = 1_700_000_000_000
    assert pick_ref_expiry([_ms(2, t), _ms(7.2, t), _ms(30, t)], t) == _ms(7.2, t)


def test_pick_ref_expiry_falls_back_to_nearest_above_min():
    """带宽内为空（只有 0.5 天与 30 天）→ 退到 ≥1 天里离目标最近的档（30 天）。"""
    t = 1_700_000_000_000
    assert pick_ref_expiry([_ms(0.5, t), _ms(30, t)], t) == _ms(30, t)
    # 已过期/无未来档 → None
    assert pick_ref_expiry([_ms(-1, t)], t) is None
    assert pick_ref_expiry([], t) is None


# ── 分位 ─────────────────────────────────────────────────────

def test_percentile_rank_basic():
    assert percentile_rank([1, 2, 3, 4], 4) == 100.0
    assert percentile_rank([1, 2, 3, 4], 1) == 25.0
    assert percentile_rank([], 1) == 0.0


def test_rolling_percentile_is_trailing_only():
    """只用历史：当前值是窗口内最大值 → 100 分。"""
    out = rolling_percentile([1.0, 2.0, 3.0], window=10, min_samples=1)
    assert out == [100.0, 100.0, 100.0]
    out = rolling_percentile([3.0, 2.0, 1.0], window=10, min_samples=1)
    assert out[0] == 100.0 and out[1] == 50.0 and out[2] == pytest.approx(100 / 3)


def test_rolling_percentile_min_samples_and_gaps():
    """样本不足 → None（fail-open 交给调用方）；空值不参与窗口。"""
    out = rolling_percentile([1.0, 2.0, 3.0], window=10, min_samples=3)
    assert out[:2] == [None, None] and out[2] == 100.0
    out = rolling_percentile([1.0, None, 2.0], window=10, min_samples=2)
    assert out[1] is None and out[2] == 100.0      # 窗口里只剩 2 个有效点


def test_window_and_min_samples_helpers():
    assert window_bars_for(7, 3600) == 168          # 7 天 1H
    assert window_bars_for(7, 900) == 672           # 7 天 15m
    assert window_bars_for(0, 3600) == 168          # 0/缺省 → 默认 7 天
    assert min_samples_for(168) == 33               # max(20, 168//5)
    assert min_samples_for(10) == 20                # 下限 20


# ── 参考 IV ──────────────────────────────────────────────────

class _Smile:
    def __init__(self, iv):
        self._iv = iv

    def iv(self, k):        # noqa: ARG002
        return self._iv


class _RiseSmile:
    """只改 ``iv()``，其余（定价等）转发给真微笑 —— 换掉曲面不能影响选链。"""

    def __init__(self, inner, iv):
        self._inner, self._iv = inner, iv

    def iv(self, k):        # noqa: ARG002
        return self._iv

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RiseSurf:
    """合成曲面：IV 随 bar 单调上升 → 分位必然走高到 100（其余转发真曲面）。"""

    def __init__(self, real, ts_to_iv):
        self._real, self._m = real, ts_to_iv

    def smile_at(self, t_ms, exp_ms, forward=None):
        inner = self._real.smile_at(t_ms, exp_ms, forward=forward)
        if inner is None:
            return None
        return _RiseSmile(inner, self._m.get(t_ms, 0.6))

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Surf:
    def __init__(self, iv):
        self.iv = iv
        self.calls: list[tuple] = []

    def smile_at(self, t_ms, exp_ms, forward=None):
        self.calls.append((t_ms, exp_ms, forward))
        return _Smile(self.iv)
def test_ref_atm_iv_uses_target_expiry_and_spot_forward():
    t = 1_700_000_000_000
    s = _Surf(0.62)
    assert ref_atm_iv(s, [_ms(2, t), _ms(7.2, t)], t, 100.0) == pytest.approx(0.62)
    assert s.calls[0][1] == _ms(7.2, t) and s.calls[0][2] == 100.0


def test_ref_atm_iv_fail_closed():
    t = 1_700_000_000_000
    assert ref_atm_iv(None, [_ms(7, t)], t, 100.0) is None
    assert ref_atm_iv(_Surf(0.6), [_ms(7, t)], t, 0.0) is None          # 无现货
    assert ref_atm_iv(_Surf(None), [_ms(7, t)], t, 100.0) is None       # 微笑取不到
    assert ref_atm_iv(_Surf(0.6), [], t, 100.0) is None                 # 无在售档


# ── driver 接线（防「死门」回归）────────────────────────────────

class _RiseSurfLegacy:
    """（保留旧名占位，实际用 _RiseSurf 包装真曲面）"""

    def __init__(self, ts_to_iv):
        self._m = ts_to_iv

    def smile_at(self, t_ms, exp_ms, forward=None):   # noqa: ARG002
        return _Smile(self._m[t_ms])


def _driver_with_synthetic_iv() -> T.OptionsBacktestDriver:
    d = T._driver()
    bt = list(d.data.bar_times)
    ts_ms = [int(t.timestamp() * 1000) for t in bt]
    real = d.data._surface
    d.data._surface = _RiseSurf(real, {ms: 0.40 + 0.004 * i
                                       for i, ms in enumerate(ts_ms)})
    d.data.expiries_at = lambda ts=None: [int(ts.timestamp() * 1000) + int(7 * DAY_MS)]
    d.data.spot_at = lambda ts=None: 100.0
    return d


def test_build_iv_pct_series_and_meta():
    d = _driver_with_synthetic_iv()
    d._build_iv_pct()
    bt = list(d.data.bar_times)
    meta = d._iv_pct_meta
    assert meta["bars"] == len(bt) and meta["obs"] == len(bt)
    assert meta["window_bars"] == 168 and meta["min_samples"] == 33
    # 前 min_samples-1 根样本不足 → None；单调上升 → 末尾 100 分
    assert d._iv_pct_at(bt[0]) is None
    assert d._iv_pct_at(bt[-1]) == 100.0
    assert meta["ready"] == len(bt) - 32


def test_try_entry_feeds_percentile_into_gate(monkeypatch):
    """接线回归：分位必须真的传进 ``evaluate_entry``（否则 IV 闸门是死门）。"""
    d = _driver_with_synthetic_iv()
    d._build_iv_pct()
    d.opt_params = {"entry_setup": 9, "entry_countdown": 13,
                    "iv_min_percentile": 70, "max_contracts_per_family": 1,
                    "max_contracts_total": 1}
    d._opt_cache = None
    seen: dict = {}

    def _fake_entry(*a, **kw):
        seen.update(kw)
        return None, "IV 闸门：测试拦截"

    monkeypatch.setattr("nanobot_quant.okx_options_strategy.evaluate_entry",
                        _fake_entry)
    ts = list(d.data.bar_times)[-1]
    d.data.seek(ts)          # price_of() 吃当前时刻，不 seek 会早退
    d._try_entry(ts, [], [], 1000.0, sig=_SIG)
    assert seen.get("iv_percentile") == 100.0
    assert d._iv_pct_report()["entries"] == 0        # 被闸门拦住 → 不计入入场
    assert d._iv_pct_report()["gate"] == 70

    # 成功入场时把分位记录下来（报告要能回答「入场时分位多少」）
    monkeypatch.setattr(
        "nanobot_quant.okx_options_strategy.evaluate_entry",
        lambda *a, **kw: (types.SimpleNamespace(
            strike=100.0, sz=1, bid=0.5,
            inst_id="SOL-USD_UM-260920-100-P", entry_reason="buy9",
            iv=0.6, delta=-0.2, days=5.0, net_yield_pct=0.4), "ok"))
    d._try_entry(ts, [], [], 1000.0, sig=_SIG)
    rep = d._iv_pct_report()
    assert rep["entries"] == 1 and rep["entry_pct_median"] == 100.0


def test_try_entry_counts_na_when_gate_on_without_samples(monkeypatch):
    """闸门开着但算不出分位 → fail-open 放行，同时必须计数（不得静默）。"""
    d = T._driver()
    d.opt_params = {"entry_setup": 9, "entry_countdown": 13, "iv_min_percentile": 70}
    d._opt_cache = None
    d._iv_pct_map, d._iv_pct_entries, d._iv_gate_na = {}, [], 0
    monkeypatch.setattr("nanobot_quant.okx_options_strategy.evaluate_entry",
                        lambda *a, **kw: (None, "无合格候选"))
    ts = list(d.data.bar_times)[-1]
    d.data.seek(ts)
    d._try_entry(ts, [], [], 1000.0, sig=_SIG)
    assert d._iv_gate_na == 1
    assert d._iv_pct_report()["signal_na"] == 1
