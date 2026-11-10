"""C138（2026-11-10）：自适应加仓间距按 `(scene, symbol)` 隔离。

背景：原实现把已定案命中列表 / 当前档位 / 滞回计数器 / 统计快照都挂在
scene 键上，而 gap 门本身是按标的（`_min_open_cost(该标的的批次)`）判定的
—— 粒度错配导致：① 不同标的 hit_rate 混算互相稀释 ② 统计快照被最后评估
的标的覆盖写 ③ 滞回 `_AD_HYST` 是 scene 级却每轮每标的各调用一次，一轮
5 个标的就给同一判定 +5，两轮内必切、防抖失效。

口径（不变）：hit≥50% → 0.1%，30~50% → 0.2%，<30% → 0.4%；slot 占用
≥80% 再 +0.1（只收缩）；滞回 2 次；定案样本 < 5（本文件缩到 2）回退静态。

本文件锁三条契约：
1. 两标的 hit_rate / 档位互不影响（同一实例切换 symbol 也隔离）；
2. 同轮多标的各调一次不放大滞回计数（每个 (scene, symbol) 只 +1）；
3. 统计快照与档位来源属该标的自身（不被其他标的覆盖）。
"""

from __future__ import annotations

import pandas as pd

from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

SYMS = ["SOL", "BNB", "BTC", "ETH", "CRCL"]


def _mk(symbol: str = "SOL", *, static: float = 0.0, m: int = 2):
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters)
    s.symbol = symbol
    s._current_scene = "high"
    s._entry_min_gap_pct = static
    s._adaptive_gap_enabled = True
    s._adaptive_m = m
    s._adaptive_watch = {}
    s._adaptive_done = {}
    s._adaptive_gap_cur = {}
    s._adaptive_gap_pend = {}
    s._adaptive_gap_src = {}
    s._adaptive_last_stat = {}
    s._adaptive_na_logged = set()
    s._AD_MIN_SAMPLES = 2
    s._AD_K = 4
    return s


def _feed(s, high: float, close: float, buy_signal: bool = False, bm=None) -> float:
    df = pd.DataFrame({"High": [high], "Close": [close]})
    return s._adaptive_gap_observe(df, buy_signal, bm)


def _close_one_signal(s, *, hit: bool, entry: float = 100.0, m: int = 2) -> None:
    """在 s.symbol 名下产生一个已定案信号：登记 → 推进 m 根 → 定案。"""
    _feed(s, high=entry, close=entry, buy_signal=True)
    for _ in range(m):
        top = entry * 1.005 if hit else entry * 0.999
        _feed(s, high=top, close=entry, buy_signal=False)


# ── ① hit_rate / 档位按标的隔离 ──────────────────────────────────────


def test_hit_rate_and_gap_isolated_per_symbol():
    """同一实例切换 symbol：SOL 全未命中 → 0.4%，ETH 全命中 → 0.1%，互不影响。"""
    s = _mk("SOL")
    for _ in range(3):
        _close_one_signal(s, hit=False)
    assert _feed(s, 100.0, 100.0) == 0.4, "SOL：hit_rate 0 → 收紧档"

    s.symbol = "ETH"
    for _ in range(2):
        _close_one_signal(s, hit=True)
    assert _feed(s, 100.0, 100.0) == 0.1, "ETH 自己的样本，不被 SOL 的 0% 拖低"

    s.symbol = "SOL"
    assert _feed(s, 100.0, 100.0) == 0.4, "切回 SOL 仍是它自己的 0.4%"
    assert s._adaptive_gap_cur[("high", "SOL")] == 0.4
    assert s._adaptive_gap_cur[("high", "ETH")] == 0.1


# ── ② 同轮多标的不放大滞回计数 ──────────────────────────────────────


def test_hysteresis_not_amplified_by_multiple_symbols():
    """一轮内 5 个标的各调一次 = 每个 (scene, symbol) 只 +1，不立刻切档。

    旧实现（scene 级 pend）在一轮内就被 5 个标的加到 n=5 ≥ _AD_HYST(2)，
    下一次调用即切档 —— 防抖形同虚设。
    """
    s = _mk("SOL")
    # 先让每个标的都落在 0.1% 档（高命中）
    for sym in SYMS:
        s.symbol = sym
        s._adaptive_done[("high", sym)] = [True] * 4
        assert _feed(s, 100.0, 100.0) == 0.1
    # 全部反转成低命中，同一轮依次评估
    for sym in SYMS:
        s.symbol = sym
        s._adaptive_done[("high", sym)] = [False] * 4
        assert _feed(s, 100.0, 100.0) == 0.1, "滞回未满，维持原档"
    for sym in SYMS:
        assert s._adaptive_gap_pend[("high", sym)]["n"] == 1, "每个标的只 +1"
    # 第二轮：各标的自己的第 2 次确认 → 各自切到 0.4%
    for sym in SYMS:
        s.symbol = sym
        assert _feed(s, 100.0, 100.0) == 0.4
        assert ("high", sym) not in s._adaptive_gap_pend


# ── ③ 统计快照 / 档位来源按标的隔离 ────────────────────────────────


def test_stat_snapshot_and_src_are_symbol_scoped():
    """ETH 的评估不得覆盖 SOL 的统计快照与档位来源（旧实现会覆盖 scene 键）。"""
    s = _mk("SOL", static=0.3)
    s._adaptive_done[("high", "SOL")] = [True] * 4
    s._adaptive_done[("high", "ETH")] = [False] * 4

    s.symbol = "SOL"
    assert _feed(s, 100.0, 100.0) == 0.1
    s.symbol = "ETH"
    assert _feed(s, 100.0, 100.0) == 0.4

    sol_stat = s._adaptive_last_stat[("high", "SOL")]
    eth_stat = s._adaptive_last_stat[("high", "ETH")]
    assert sol_stat["hit_rate"] == 1.0 and sol_stat["n"] == 4
    assert eth_stat["hit_rate"] == 0.0 and eth_stat["n"] == 4
    assert s._adaptive_gap_src[("high", "SOL")] == "自适应"
    assert s._adaptive_gap_src[("high", "ETH")] == "自适应"


# ── ④ 冷启动按标的各自回退静态 ──────────────────────────────────────


def test_cold_start_falls_back_to_static_per_symbol():
    """SOL 已生效、ETH 样本不足 → ETH 回退静态值（不影响 SOL 的档位）。"""
    s = _mk("SOL", static=0.3)
    s._adaptive_done[("high", "SOL")] = [True] * 4
    s.symbol = "SOL"
    assert _feed(s, 100.0, 100.0) == 0.1

    s.symbol = "ETH"
    assert _feed(s, 100.0, 100.0) == 0.3, "无定案样本 → 用自己的静态兜底"
    assert s._adaptive_gap_src[("high", "ETH")] == "自适应·回退静态"
    assert s._adaptive_gap_src[("high", "SOL")] == "自适应"
    assert s._adaptive_gap_cur[("high", "SOL")] == 0.1
