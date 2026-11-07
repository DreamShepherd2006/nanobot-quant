"""EOD 强平豁免标的（2026-11-07）。

需求：日界强平时「选中的标的不强制平仓」——用于保留想长期持有 / 等解套的
仓位，不被单双号日界机械清掉。

锁死的语义（改动前先看这几条）：
- 豁免名单是**场景级**参数 `eod_exempt_symbols`，默认 `[]` = 全部参与强平
  （零行为变化）。
- 豁免是**标的级**：该标的的所有批次都跳过，不看 `eod_mode`。
- 跳过必须**留痕**：`eod_exempt` 计数 + SKIP 事件（拦截类逻辑不得静默）。
- 名单必须 ⊆ 本场景标的池，非法值 fail-closed 并在错误里列出可选值。
- 空列表是**合法值**（≠ 通用 list 校验要求的非空），必须单独放行。
"""
from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.batches import BatchManager
from nanobot_quant.exec_params import (
    DEFAULT_SCENES,
    _apply_scenes_from_params,
    validate_exec_param,
)
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy
from nanobot_quant.tools.tools_backtest import coerce_overrides


def _bm(tmp_path, lots):
    accounts = [f"acct{i}" for i in range(1, len(lots) + 1)]
    bm = BatchManager(symbol="SOL/USDT", account_ids=accounts,
                      path=tmp_path / "b.json")
    for slot, px in lots:
        bm.open_lot(1.0, px, entry_time="2026-01-01T00:00:00", slot=slot)
    return bm


def _strat(bm, *, exempt=(), enabled=True, parity="even", mode="all", hour=0):
    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s.symbol = "SOL/USDT"
    s.batch_manager = bm
    s._exit_order = "fifo"
    s._sell_all_max = 20
    s._eod_enabled = enabled
    s._eod_parity = parity
    s._eod_mode = mode
    s._eod_hour_utc = hour
    s._eod_last_day = {}
    # 2026-11-07 新增状态：豁免名单（frozenset）
    s._eod_exempt = frozenset(exempt)
    s._sold = []
    s._skip_counts = {}
    s._events = []
    s._sell_lot = lambda slot, price, signal, reason: s._sold.append(
        (slot["slot"], reason))
    s._record = lambda *a, **k: s._events.append(a)
    s._bump_skip = lambda k: s._skip_counts.__setitem__(
        k, s._skip_counts.get(k, 0) + 1)
    return s


def _tick(s, ts, price=101.0):
    s._current_bar_time = pd.Timestamp(ts)
    return s._maybe_eod_close(price, {"price": price})


# ── 参数层 ────────────────────────────────────────────────────────────


def test_default_scenes_have_empty_exempt_list():
    for key in ("high", "mid", "low"):
        assert DEFAULT_SCENES[key]["eod_exempt_symbols"] == []
        # 默认关 ⇒ 豁免名单即使存在也不产生影响
        assert DEFAULT_SCENES[key]["eod_enabled"] is False


def test_validate_accepts_empty_list():
    """空 = 全部参与，是合法值（通用 list 校验要求非空，必须特例放行）。"""
    assert validate_exec_param("eod_exempt_symbols", []) is None
    assert validate_exec_param("eod_exempt_symbols", None) is None


def test_validate_rejects_non_list_and_bad_items():
    assert validate_exec_param("eod_exempt_symbols", "SOL") == "必须是标的列表"
    assert validate_exec_param("eod_exempt_symbols", ["SOL", ""]) == "列表项必须是非空字符串"


def test_validate_params_fails_when_symbol_not_in_scene_pool():
    """名单里的标的必须在本场景标的池内——非法值 fail-closed 并列出可选值。"""
    err = _apply_scenes_from_params({"scenes": {"high": {
        "symbols": ["SOL", "BNB"],
        "eod_exempt_symbols": ["SOL", "BTC"],
    }}}, {})
    assert isinstance(err, dict) and err.get("ok") is False
    msg = err["error"]
    assert "BTC" in msg and "SOL,BNB" in msg


def test_validate_params_accepts_subset():
    assert _apply_scenes_from_params({"scenes": {"high": {
        "symbols": ["SOL", "BNB"],
        "eod_exempt_symbols": ["SOL"],
    }}}, {}) is None


# ── 回测覆盖归一化 ────────────────────────────────────────────────────


def test_coerce_overrides_parses_comma_text_and_list():
    assert coerce_overrides({"eod_exempt_symbols": "SOL, CRCL"}) == {
        "eod_exempt_symbols": ["SOL", "CRCL"]}
    assert coerce_overrides({"eod_exempt_symbols": ["SOL", " CRCL "]}) == {
        "eod_exempt_symbols": ["SOL", "CRCL"]}
    # 空串 = 跟随场景，不进 overrides（与其它覆盖项同语义）
    assert coerce_overrides({"eod_exempt_symbols": ""}) == {}


# ── 策略层：豁免真的跳过、且留痕 ──────────────────────────────────────


def test_exempt_symbol_skips_eod_close(tmp_path):
    bm = _bm(tmp_path, [(1, 100.0)])
    s = _strat(bm, exempt=("SOL/USDT",))
    _tick(s, "2026-01-02 23:59")  # 首根只记录 prev，不触发
    assert _tick(s, "2026-01-03 00:01") is False  # 刚结束 1/2 = 双号，本应强平
    assert s._sold == [], "豁免标的不应被强平"
    assert s._skip_counts.get("eod_exempt", 0) == 1
    assert s._events, "豁免跳过必须写 SKIP 事件（不得静默）"
    assert "eod_exempt" in s._events[0][1]


def test_non_exempt_symbol_still_closes(tmp_path):
    bm = _bm(tmp_path, [(1, 100.0)])
    s = _strat(bm, exempt=("BNB/USDT",))
    _tick(s, "2026-01-01 23:59")
    assert _tick(s, "2026-01-02 00:01") is False  # 刚结束 1/1 = 单号
    _tick(s, "2026-01-02 23:59")
    assert _tick(s, "2026-01-03 00:01") is True  # 刚结束 1/2 = 双号 → 全平
    assert len(s._sold) == 1
    assert s._skip_counts.get("eod_exempt", 0) == 0


def test_exempt_does_not_affect_non_parity_days(tmp_path):
    """只有「本来要强平」的日子才记豁免——否则每根跨日 bar 都刷噪音。"""
    bm = _bm(tmp_path, [(1, 100.0)])
    s = _strat(bm, exempt=("SOL/USDT",))
    _tick(s, "2026-01-02 00:01")  # 首根，记录 prev=1/2
    _tick(s, "2026-01-02 12:00")  # 同日，不跨日
    _tick(s, "2026-01-03 00:01")  # 跨日：刚结束 1/2 = 双号 → 记一次豁免
    _tick(s, "2026-01-03 12:00")
    _tick(s, "2026-01-04 00:01")  # 刚结束 1/3 = 单号 → 本就不强平，不记
    assert s._skip_counts.get("eod_exempt", 0) == 1
    assert s._sold == []
