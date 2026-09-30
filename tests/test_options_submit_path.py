"""期权线提交路径回归（2026-09-29 实测的「假成功」根因 + 三档判定）。

背景一（根因）：lumibot v4.5.78 的 ``Strategy.create_order`` **只创建 Order 对象、
不提交**（docstring: "Once created, an order must still be submitted."）。期权线漏了
``self.submit_order(order)``，于是日志报「已提交卖出/已买回」而交易所在所无单、
无持仓、台账无行（13:31 的 261002-114-P 与更早 20+ 次「已买回」全是幻影）。

背景二（三档）：提交结果必须区分 **成交 / 在途未定案 / 失败** —— 实测 14:05 的
IOC 在 5s 轮询窗口内报 live、随后成交，却被记成「卖出失败」；若据此不占本周期，
下轮就会重复提交同一合约（在途不算失败，也不报成交）。
"""

from __future__ import annotations

import types

import pytest

from nanobot_quant import okx_options_strategy as st
from nanobot_quant import okx_options_trade as ot
from nanobot_quant.strategies.okx_options_put_strategy import OkxOptionsPutStrategy

_INST = "SOL-USD_UM-261002-114-P"


def _strategy() -> OkxOptionsPutStrategy:
    s = OkxOptionsPutStrategy()
    s.parameters = {**dict(OkxOptionsPutStrategy.parameters), "live_mode": False}
    s._cycle_state = {}
    return s


def _order(error=None, opt_status=None):
    """伪 Order：error 与 broker 透传的 opt_status（custom_params）。"""
    cp = {} if opt_status is None else {"opt_status": opt_status}
    return types.SimpleNamespace(error=error, _error=None, custom_params=cp)


def _prepare_submit(monkeypatch, order):
    seen: dict = {}
    monkeypatch.setattr(OkxOptionsPutStrategy, "_asset_for",
                        lambda self, d: "ASSET", raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "create_order",
                        lambda self, a, q, side, **kw: (seen.update(created=(a, q, side))
                                                        or order), raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "submit_order",
                        lambda self, o, **kw: seen.update(submitted=o), raising=False)
    return seen


def _submit(monkeypatch, order):
    _prepare_submit(monkeypatch, order)
    return _strategy()._submit_option(types.SimpleNamespace(inst_id=_INST, sz=1), {})


# ─────────────────────── 根因：必须提交 ───────────────────────

def test_submit_option_must_call_submit_order(monkeypatch):
    order = _order()
    seen = _prepare_submit(monkeypatch, order)

    state, err = _strategy()._submit_option(
        types.SimpleNamespace(inst_id=_INST, sz=1), {})

    assert seen["created"] == ("ASSET", 1, "sell")   # (asset, qty, side) 顺序不变
    assert seen.get("submitted") is order, "必须调 submit_order（create_order 不提交）"
    assert (state, err) == ("filled", None)


# ─────────────────────── 成本锚 C 随订单下传（2026-09-30 根因） ───────────────────────

def test_submit_option_stamps_cost_basis_for_call(monkeypatch):
    """卖 call 决策算出的成本锚 C 必须写到 order.custom_params 上（broker 据此校验保本门）。

    2026-09-30 实测：策略算出了 C，但 ``create_order`` 不带 kwarg、也没写
    custom_params → broker 拿到 None → 自动卖 call 每轮被保本门 fail-closed。
    """
    order = _order()
    order.custom_params = None          # 真实 v4.5.78 默认即 None（写入前需先置 dict）
    _prepare_submit(monkeypatch, order)
    dec = types.SimpleNamespace(inst_id=_INST.replace("-P", "-C"), sz=1, cost_basis=122.37)

    state, err = _strategy()._submit_option(dec, {})

    assert order.custom_params == {"cost_basis": 122.37}, "C 必须随订单下传"
    assert (state, err) == ("filled", None)


def test_submit_option_no_cost_basis_key_when_absent(monkeypatch):
    """卖 put（无 C）不得凭空塞 cost_basis；买回（closing）同样不塞。"""
    order = _order()
    _prepare_submit(monkeypatch, order)
    _strategy()._submit_option(types.SimpleNamespace(inst_id=_INST, sz=1), {})
    assert "cost_basis" not in order.custom_params

    order2 = _order()
    _prepare_submit(monkeypatch, order2)
    _strategy()._submit_option(
        types.SimpleNamespace(inst_id=_INST, sz=1, cost_basis=122.37), {}, closing=True)
    assert "cost_basis" not in order2.custom_params


def test_broker_reads_cost_basis_from_order_not_asset(monkeypatch):
    """broker 侧：C 从 order.custom_params 读（旧代码读 asset → 永远为空）。"""
    from nanobot_quant.brokers.okx_options_broker import OkxOptionsBroker

    class _Asset:
        symbol = "SOL"
        custom_params = {"cost_basis": 999.0}   # asset 上的同名值不得生效

    b = OkxOptionsBroker.__new__(OkxOptionsBroker)
    b._cost_basis_map = {"SOL": 101.0}
    assert b._cost_basis_for(types.SimpleNamespace(
        asset=_Asset(), custom_params={"cost_basis": 122.37})) == 122.37
    # order 上没有 → 回退构造期 map（兼容手动建 broker 的老用法）
    assert b._cost_basis_for(types.SimpleNamespace(asset=_Asset(), custom_params=None)) == 101.0
    b._cost_basis_map = {}
    assert b._cost_basis_for(types.SimpleNamespace(asset=_Asset(), custom_params=None)) is None


# ─────────────────────── 三档判定 ───────────────────────

@pytest.mark.parametrize("raw", ["pending", "unknown", "live", "open",
                                 "partially_filled", "submitted"])
def test_in_flight_status_is_pending_not_failed(monkeypatch, raw):
    """已受理、未定案 → 在途（不算成交、也不算失败）。"""
    state, err = _submit(monkeypatch, _order(error=f"订单未成交（状态 {raw}）",
                                             opt_status=raw))
    assert state == "pending"
    assert err and raw in err


@pytest.mark.parametrize("raw", ["cancelled", "canceled", "failed", ""])
def test_real_failure_is_failed(monkeypatch, raw):
    """撤销/拒单/空状态 → 失败（下轮可重试）。"""
    state, err = _submit(monkeypatch, _order(error="IOC 未匹配盘口，已自动撤单",
                                             opt_status=raw))
    assert state == "failed" and err


def test_submit_exception_is_failed_not_silent(monkeypatch):
    monkeypatch.setattr(OkxOptionsPutStrategy, "_asset_for",
                        lambda self, d: "ASSET", raising=False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "create_order",
                        lambda self, a, q, side, **kw: (_ for _ in ()).throw(
                            RuntimeError("boom")), raising=False)
    state, err = _strategy()._submit_option(
        types.SimpleNamespace(inst_id=_INST, sz=1), {})
    assert state == "failed" and "RuntimeError" in err and "boom" in err


# ─────────────────────── 周期占位语义 ───────────────────────

def _entries_ctx(monkeypatch, submit_result):
    s = _strategy()
    monkeypatch.setattr(OkxOptionsPutStrategy, "_td_signal",
                        lambda self, family, base, p: {"setup_buy": 2, "cd_buy": 12,
                                                       "setup_sell": 0, "cd_sell": 0,
                                                       "price": 121.0,
                                                       "recommendation": "HOLD"},
                        raising=False)
    monkeypatch.setattr(ot, "usdc_avail", lambda account="", ccy="USDC": 1000.0)
    monkeypatch.setattr(ot, "has_pending_ledger", lambda inst_id, kinds: False)
    monkeypatch.setattr(OkxOptionsPutStrategy, "_submit_option",
                        lambda self, dec, p, closing=False: submit_result, raising=False)
    dec = types.SimpleNamespace(inst_id=_INST, sz=1,
                                to_event=lambda: {"family": "SOL", "inst_id": _INST,
                                                  "sz": 1})
    monkeypatch.setattr(st, "evaluate_entry", lambda *a, **k: (dec, None))
    p = {"put_enabled": True, "families": ["SOL-USD_UM"], "entry_setup": 9,
         "entry_countdown": 12, "live_mode": False}
    return s, p


def test_failed_submit_does_not_consume_the_cycle(monkeypatch):
    """失败 = 交易所在所无单 → 不置「本周期已建仓」，下轮可重试。"""
    s, p = _entries_ctx(monkeypatch, ("failed", "IOC 未匹配盘口，已自动撤单"))
    recs = s._entries("", p, False, {}, 0, [])

    assert recs and recs[0]["status"] == "failed"
    assert not (s._cycle_state.get("SOL-USD_UM") or {}).get("bought"), "失败不应置位"


def test_pending_submit_consumes_the_cycle_but_is_not_a_sale(monkeypatch):
    """在途：照旧占位（防重复提交），但**不计成交**（单独显示为「在途」）。"""
    s, p = _entries_ctx(monkeypatch, ("pending", "订单未成交（状态 live）"))
    recs = s._entries("", p, False, {}, 0, [])

    assert recs and recs[0]["status"] == "pending", "在途不得记成 sold"
    assert (s._cycle_state.get("SOL-USD_UM") or {}).get("bought") is True


def test_successful_submit_marks_the_cycle(monkeypatch):
    s, p = _entries_ctx(monkeypatch, ("filled", None))
    recs = s._entries("", p, False, {}, 0, [])

    assert recs and recs[0]["status"] == "sold"
    assert (s._cycle_state.get("SOL-USD_UM") or {}).get("bought") is True
