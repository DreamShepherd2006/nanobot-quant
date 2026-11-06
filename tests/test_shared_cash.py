"""共享现金池（shared_cash）——2026-11-06 用户规则回测新增。

语义：``shared_cash=True`` 时所有 slot 共用一份 quote 余额（真实交易所
「一个子账号一个余额池」），多个逻辑 slot 只是记账标签；``False``（默认）
= 每 slot 独立资金（现有行为、零变化）。

池额 = ``initial_quote × batches``，与独立模式同一 ``initial_total``，
ROI 分母不变、两组可直接对照。

关键不变量：共享池下**净值聚合必须去重**（现金只算一次）——各 slot broker
读的是同一个池，逐 slot 累加会重复计 N 次（2026-10-31 曾因此跑出虚假
+33.2% ROI）。本文件的 ``test_snapshot_cash_identical_across_slots`` 就是
这条不变量的护栏。
"""

from types import SimpleNamespace

import pytest

from nanobot_quant.brokers.backtest_broker import BacktestBroker, CashPool

TOKENS = [
    {
        "symbol": "CRCLX",
        "chain": "solana",
        "address": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
        "gate_symbol": "CRCLX",
        "okx_symbol": "XCRCL",
    }
]


class _Order(SimpleNamespace):
    def set_error(self, msg):
        self.error = msg

    def set_filled(self):
        self.filled = True

    def set_identifier(self, oid):
        self.identifier = oid


def _mk_order(side="buy", quantity=0.05, symbol="CRCLX"):
    from lumibot.entities import Asset

    return _Order(
        asset=Asset(symbol=symbol, asset_type="crypto"),
        side=side,
        quantity=quantity,
        identifier=None,
        custom_params={},
        error=None,
        filled=False,
        status="new",
    )


class _Px:
    def __init__(self, price: float = 100.0):
        self.price = price

    def __call__(self, symbol):  # noqa: ARG002
        return self.price


def _broker(price=100.0, pool=None, initial_quote=100.0):
    return BacktestBroker(
        initial_quote=initial_quote,
        price_source=_Px(price),
        tokens_json=TOKENS,
        cash_pool=pool,
    )


# ── 池共享语义 ────────────────────────────────────────────────────────────

def test_pool_shared_between_slots():
    pool = CashPool(1000.0)
    a = _broker(pool=pool)
    b = _broker(pool=pool)
    assert a._cash == pytest.approx(1000.0)
    assert b._cash == pytest.approx(1000.0)

    a._submit_order(_mk_order(quantity=1.0))  # 花 1×100 = 100 USDT
    assert a._cash == pytest.approx(900.0)
    # 同一份钱：B 的可用余额同步减少（这正是「共享」的定义）
    assert b._cash == pytest.approx(900.0)


def test_pool_receives_sell_proceeds():
    pool = CashPool(500.0)
    a = _broker(pool=pool)
    a._submit_order(_mk_order(quantity=1.0))
    after_buy = a._cash
    a._submit_order(_mk_order(side="sell", quantity=0.999))
    assert a._cash > after_buy
    assert pool.amount == pytest.approx(a._cash)


def test_pool_initial_quote_ignored_when_pool_given():
    """传了池就以池为准，不被 per-slot initial_quote 覆盖。"""
    pool = CashPool(333.0)
    a = _broker(pool=pool, initial_quote=100.0)
    assert a._cash == pytest.approx(333.0)
    assert a._own_cash == 0.0


def test_pool_exhausted_fail_closed():
    pool = CashPool(50.0)
    a = _broker(pool=pool)
    out = a._submit_order(_mk_order(quantity=1.0))  # 需 100 USDT
    assert out.filled is not True
    assert out.error
    assert pool.amount == pytest.approx(50.0)  # 失败不动池


# ── 默认（独立资金）零行为变化 ─────────────────────────────────────────────

def test_independent_by_default():
    a = _broker()
    b = _broker()
    a._submit_order(_mk_order(quantity=0.5))
    assert a._cash == pytest.approx(50.0)
    assert b._cash == pytest.approx(100.0)  # B 不受影响
    assert a._cash_pool is None


# ── 净值去重护栏 ─────────────────────────────────────────────────────────

def test_snapshot_cash_identical_across_slots():
    """共享池下两个 slot 的 snapshot.cash 完全相等 ⇒ 聚合必须去重。"""
    pool = CashPool(500.0)
    a = _broker(pool=pool)
    b = _broker(pool=pool)
    a._submit_order(_mk_order(quantity=1.0))

    sa, sb = a.snapshot(), b.snapshot()
    assert sa["cash"] == sb["cash"] == pytest.approx(pool.amount)
    # 若按 slot 累加 total：现金会被计 2 次（旧 bug），持仓市值只该计 1 次
    naive_total = sa["total"] + sb["total"]
    dedup_total = pool.amount + sa["positions_value"] + sb["positions_value"]
    assert naive_total > dedup_total  # 现金被重复计了一次
    assert dedup_total == pytest.approx(
        pool.amount + 1.0 * 100.0 * (1 - 0.001)
    )


def test_positions_stay_per_slot():
    """共享现金，但持仓仍按 slot 独立记账（否则会重复卖出）。"""
    pool = CashPool(1000.0)
    a = _broker(pool=pool)
    b = _broker(pool=pool)
    a._submit_order(_mk_order(quantity=1.0))
    assert a._positions.get("CRCLX", 0.0) > 0
    assert b._positions.get("CRCLX", 0.0) == 0.0
