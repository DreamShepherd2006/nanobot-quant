"""自适应加仓间距 `adaptive_gap_enabled`（2026-11-03 研究线）测试。

反馈量 = 最近 K 个已定案**信号**中，入场后 M 根内最高价曾摸到
entry×(1+0.3%) 的比例（信号口径：被 gap 门拦掉的信号同样计入，避免
「收紧 → 买得少 → 样本少」的正反馈锁死）。

档位：hit≥50% → 0.1%；30%~50% → 0.2%；<30% → 0.4%；滞回 2 次；
slot 占用 ≥80% 再 +0.1（只收缩不扩张）。默认关 → 零行为变化。
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


def _mk(*, enabled: bool = True, m: int = 2, static: float = 0.0) -> TdSequentialStrategy:
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters)
    s.symbol = "SOL"
    s._current_scene = "high"
    s._entry_min_gap_pct = static
    s._adaptive_gap_enabled = enabled
    s._adaptive_m = m
    s._adaptive_watch = {}
    s._adaptive_done = {}
    s._adaptive_gap_cur = {}
    s._adaptive_gap_pend = {}
    # 2026-11-04 夹具补齐：这三个属性在 initialize() 里初始化，而本夹具直接
    # TdSequentialStrategy() 不走 initialize（lumibot 契约里由 executor 调用）
    # ——缺 _adaptive_gap_src 会让 entry_gap 事件标注档位来源时 AttributeError
    # （生产无影响，仅测试路径）。
    s._adaptive_gap_src = {}
    s._adaptive_last_stat = {}
    s._adaptive_na_logged = set()
    # 缩小样本门槛/窗口便于用例构造
    s._AD_MIN_SAMPLES = 2
    s._AD_K = 4
    return s


def _feed(s, high: float, close: float, buy_signal: bool = False, bm=None) -> float:
    df = pd.DataFrame({"High": [high], "Close": [close]})
    return s._adaptive_gap_observe(df, buy_signal, bm)


def _close_one_signal(s, *, hit: bool, entry: float = 100.0, m: int = 2, bm=None):
    """产生一个已定案信号：登记 → 推进 m 根 → 定案。"""
    _feed(s, high=entry, close=entry, buy_signal=True, bm=bm)
    top = entry * 1.005 if hit else entry * 0.995
    for _ in range(m):
        _feed(s, high=top if hit else entry * 0.999, close=entry, buy_signal=False, bm=bm)


class _BM:
    def __init__(self, open_n: int = 0, total: int = 10):
        self.slots = [{"slot": i + 1} for i in range(total)]
        self._open = open_n

    def open_slots(self):
        return [{"slot": i + 1} for i in range(self._open)]


# ── 零行为变化 / fail-closed ─────────────────────────────────────────


def test_disabled_returns_static_gap():
    s = _mk(enabled=False, static=0.3)
    assert _feed(s, 100.0, 100.0, buy_signal=True) == 0.3
    # 关闭时完全不动追踪状态
    assert s._adaptive_watch == {} and s._adaptive_done == {}


def test_insufficient_samples_falls_back_to_static():
    s = _mk(static=0.3)
    _close_one_signal(s, hit=True)  # 只有 1 个定案样本 < min_samples(2)
    assert _feed(s, 100.0, 100.0) == 0.3


def test_unusable_bar_price_falls_back():
    s = _mk(static=0.25)
    df = pd.DataFrame({"High": [], "Close": []})
    assert s._adaptive_gap_observe(df, True, None) == 0.25


# ── 档位映射 ────────────────────────────────────────────────────────


def test_high_hit_rate_loosens():
    s = _mk(static=0.3)
    for _ in range(3):
        _close_one_signal(s, hit=True)
    gap = _feed(s, 100.0, 100.0)
    assert gap == 0.1, "全部命中 → 放宽到 0.1"


def test_low_hit_rate_tightens():
    s = _mk(static=0.3)
    for _ in range(3):
        _close_one_signal(s, hit=False)
    gap = _feed(s, 100.0, 100.0)
    assert gap == 0.4, "全部未命中 → 收紧到 0.4"


def test_mid_hit_rate_uses_middle_gap():
    s = _mk(static=0.3)
    _close_one_signal(s, hit=True)
    _close_one_signal(s, hit=False)  # 1/2 = 50% → 仍是放宽档
    _close_one_signal(s, hit=False)  # 1/3 = 33% → 中间档
    gap = _feed(s, 100.0, 100.0)
    assert gap == 0.2


# ── 滞回 / 护栏 ─────────────────────────────────────────────────────


def test_hysteresis_requires_two_confirmations():
    s = _mk(static=0.3)
    s._adaptive_done["high"] = [False, False, False, False]  # 低命中 → 收紧档
    assert _feed(s, 100.0, 100.0) == 0.4  # 首次直接设定
    s._adaptive_done["high"] = [True, True, True, True]  # 反转成高命中
    assert _feed(s, 100.0, 100.0) == 0.4, "第一次仅登记 pending，维持原档"
    assert _feed(s, 100.0, 100.0) == 0.1, "连续第二次才切换"


def test_slot_load_forces_tighter():
    s = _mk(static=0.3)
    s._adaptive_done["high"] = [True, True, True, True]
    bm = _BM(open_n=8, total=10)  # 80% 占用 → 触发护栏（0.1 + 0.1）
    assert _feed(s, 100.0, 100.0, bm=bm) == 0.2


# ── 观测窗口语义 ────────────────────────────────────────────────────


def test_watch_expires_after_m_bars():
    s = _mk(m=3)
    _feed(s, 100.0, 100.0, buy_signal=True)
    key = ("high", "SOL")
    assert key in s._adaptive_watch
    _feed(s, 100.1, 100.0)
    _feed(s, 100.2, 100.0)
    _feed(s, 100.3, 100.0)  # 第 3 根 → 定案
    assert key not in s._adaptive_watch
    assert s._adaptive_done["high"] == [True]  # 摸到 +0.3%


def test_no_duplicate_register_while_in_flight():
    s = _mk(m=5)
    _feed(s, 100.0, 100.0, buy_signal=True)
    for _ in range(4):
        _feed(s, 100.0, 100.0, buy_signal=True)  # 连续信号不重复登记
    assert len(s._adaptive_watch) == 1
    assert len(s._adaptive_done.get("high", [])) == 0
