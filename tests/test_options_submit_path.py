"""期权线提交路径回归（2026-09-29 实测的「假成功」根因）。

背景：lumibot v4.5.78 的 ``Strategy.create_order`` **只创建 Order 对象、不提交**
（docstring: "Once created, an order must still be submitted."）。期权线策略漏了
``self.submit_order(order)``，于是整条线变成：日志报「已提交卖出/已买回」→ 而
交易所在所无单、无持仓、台账无行（118-P 之后的 20+ 次「✅ 已买回」、13:31 的
「已提交卖出 261002-114-P」全是幻影）。现货线一直显式 submit_order，故只有期权线中招。
"""

from __future__ import annotations

import types

import pytest

from nanobot_quant import okx_options_strategy as st
from nanobot_quant import okx_options_trade as ot
from nanobot_quant.strategies.okx_options_put_strategy import OkxOptionsPutStrategy


def _strategy() -> OkxOptionsPutStrategy:
    s = OkxOptionsPutStrategy()
    s.parameters = {**dict(OkxOptionsPutStrategy.parameters), "live_mode": False}
    s._cycle_state = {}
    return s


def test_submit_option_must_call_submit_order(monkeypatch):
    """没有 submit_order 就永远不会到 broker —— 这是本轮的根因回归。"""
    s = _strategy()
    seen: dict = {}
    fake_order = types.SimpleNamespace(error=None, _error=None)
    monkeypatch.setattr(OkxOptionsPutStrategy, "_asset_for",
                        lambda self, d: "ASSET", raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "create_order",
                        lambda self, a, q, side, **kw: (seen.update(created=(a, q, side))
                                                        or fake_order), raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "submit_order",
                        lambda self, o, **kw: seen.update(submitted=o), raising=False)

    dec = types.SimpleNamespace(inst_id="SOL-USD_UM-261002-114-P", sz=1)
    ok, err = s._submit_option(dec, {})

    assert seen["created"] == ("ASSET", 1, "sell")   # 参数顺序仍然是 (asset, qty, side)
    assert seen.get("submitted") is fake_order, "必须调用 submit_order（create_order 不提交）"
    assert ok is True and err is None


def test_submit_option_reports_error_from_order(monkeypatch):
    """broker 在未成交时 set_error → 策略必须报失败（不静默）。"""
    s = _strategy()
    fake_order = types.SimpleNamespace(error="未成交（IOC 未匹配盘口，已自动撤单）", _error=None)
    monkeypatch.setattr(OkxOptionsPutStrategy, "_asset_for", lambda self, d: "A", raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "create_order",
                        lambda self, a, q, side, **kw: fake_order, raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "submit_order",
                        lambda self, o, **kw: None, raising=False)

    ok, err = s._submit_option(types.SimpleNamespace(inst_id="X-P", sz=1), {})

    assert ok is False and "未成交" in err


def _entries_ctx(monkeypatch, submit_result):
    s = _strategy()
    monkeypatch.setattr(OkxOptionsPutStrategy, "_td_signal",
                        lambda self, family, base, p: {"setup_buy": 2, "cd_buy": 12,
                                                       "setup_sell": 0, "cd_sell": 0,
                                                       "price": 121.0, "recommendation": "HOLD"},
                        raising=False)
    monkeypatch.setattr(ot, "usdc_avail", lambda account="", ccy="USDC": 1000.0)
    monkeypatch.setattr(ot, "has_pending_ledger", lambda inst_id, kinds: False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "submit_order",
                        lambda self, o, **kw: None, raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "_submit_option",
                        lambda self, dec, p, closing=False: submit_result, raising=False)
    dec = types.SimpleNamespace(inst_id="SOL-USD_UM-261002-114-P", sz=1,
                                to_event=lambda: {"family": "SOL",
                                                  "inst_id": "SOL-USD_UM-261002-114-P", "sz": 1})
    monkeypatch.setattr(st, "evaluate_entry", lambda *a, **k: (dec, None))
    p = {"put_enabled": True, "families": ["SOL-USD_UM"], "entry_setup": 9,
         "entry_countdown": 12, "live_mode": False}
    return s, p


def test_failed_submit_does_not_consume_the_cycle(monkeypatch):
    """提交失败 = 交易所在所无单 → 不能置「本周期已建仓」，否则一次 IOC 未成交
    就吃掉整轮信号周期（13:32 起被门控拦截的实测现象）。"""
    s, p = _entries_ctx(monkeypatch, (False, "未成交（IOC 未匹配盘口，已自动撤单）"))
    recs = s._entries("", p, False, {}, 0, [])

    assert recs and recs[0]["status"] == "failed"
    assert not (s._cycle_state.get("SOL-USD_UM") or {}).get("bought"), "失败不应置位"


def test_successful_submit_marks_the_cycle(monkeypatch):
    """成交/在途 → 照旧置位（防同周期重复开仓）。"""
    s, p = _entries_ctx(monkeypatch, (True, None))
    recs = s._entries("", p, False, {}, 0, [])

    assert recs and recs[0]["status"] == "sold"
    assert (s._cycle_state.get("SOL-USD_UM") or {}).get("bought") is True
