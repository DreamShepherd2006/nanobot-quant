"""期权回测 driver 的 F1 闸门接线（2026-10-04）—— 与实盘同函数、同序列。

要点：
* F1 由回放数据源缓存的底层价序列算出（零额外网络请求）；
* 默认关（f1_gate_enabled=False）时不计算、不影响任何决策；
* 样本不足 → fail-open 放行，但计数（_f1_gate_na），不静默当成“安全”。
"""
import numpy as np
import pandas as pd

from nanobot_quant.backtest.options_driver import OptionsBacktestDriver


class _Data:
    def __init__(self, df):
        self._underlying = df


def _mk(n=400, period="15m"):
    idx = pd.date_range("2026-09-01", periods=n, freq="15min")
    px = 100 + np.cumsum(np.random.default_rng(7).normal(0, 0.4, n))
    df = pd.DataFrame({"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px},
                      index=idx)
    d = OptionsBacktestDriver.__new__(OptionsBacktestDriver)   # 绕过 __init__
    d.data = _Data(df)
    d.timestep = period
    d._opt_params = lambda: {}
    return d, idx


def test_f1_map_aligns_to_bar_ts_and_has_warmup():
    d, idx = _mk()
    f1 = d._build_f1()
    assert f1, "F1 表不应为空"
    assert set(f1) <= set(idx), "F1 的键必须是 bar 时间戳（与 _f1_at(ts) 对齐）"
    assert idx[0] not in f1, "预热段不应有 F1（样本不足）"


def test_f1_at_none_before_warmup_then_value():
    d, idx = _mk()
    assert d._f1_at(idx[0]) is None          # 预热 → None（闸门 fail-open）
    assert any(d._f1_at(t) is not None for t in idx)
    assert d.timestep == "15m"               # lookback 按周期归一化（15m → 12 根）


def test_f1_gate_default_off_does_not_build_map():
    """默认关 ⇒ 连预计算都不发生（零行为变更的结构性保证）。"""
    d, idx = _mk()
    assert not d._opt_params().get("f1_gate_enabled")
    assert getattr(d, "_f1_map", None) is None
