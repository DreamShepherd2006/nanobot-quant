"""Step 1 (方案 B 回测): BacktestBroker 模拟撮合单测（无网络）。

Covered:
- buy/sell filled：花费/到账/手续费/avg_price/余额正确（fee_rate 从所得币扣）
- fail-closed：无价格 / min_quote 不足 / 资金不足 / 持仓不足 → set_error
- slippage 生效：买贵、卖便宜
- 余额/持仓接口形状：_balances / _get_balances_at_broker / _pull_positions
- snapshot / _query_order / _pull_broker_order
"""

from types import SimpleNamespace

import pytest

from nanobot_quant.brokers.backtest_broker import BacktestBroker

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


def _asset(symbol="CRCLX"):
    from lumibot.entities import Asset

    return Asset(symbol=symbol, asset_type="crypto")


def _mk_order(side="buy", quantity=0.05, symbol="CRCLX"):
    return _Order(
        asset=_asset(symbol),
        side=side,
        quantity=quantity,
        identifier=None,
        custom_params={},
        error=None,
        filled=False,
        status="new",
    )


class _PriceSource:
    """确定性价格游标：symbol → 当前 bar 收盘价（0.0=fail-closed）。"""

    def __init__(self, price=100.0, error=False):
        self.price = price
        self.error = error
        self.calls = []

    def __call__(self, symbol):
        self.calls.append(symbol)
        if self.error:
            raise RuntimeError("ticker down")
        return self.price


def _broker(**kwargs):
    kwargs.setdefault("tokens_json", TOKENS)
    return BacktestBroker(**kwargs)


class TestBuy:
    def test_filled_cash_and_fee(self):
        px = _PriceSource(67.0)
        b = _broker(initial_quote=100.0, price_source=px)
        order = _mk_order(side="buy", quantity=0.05)
        out = b._submit_order(order)
        assert out.filled is True
        assert out.status == "fill"
        assert out.error is None
        # 花费 0.05×67 = 3.35 USDT；到账 0.05×(1-0.001) = 0.04995 CRCLX
        assert b._cash == pytest.approx(100.0 - 3.35)
        assert b._positions["CRCLX"] == pytest.approx(0.05 * (1 - 0.001))
        # avg 含手续费摊薄 = 3.35 / 0.04995 ≈ 67.067
        assert out.custom_params["cex"]["avg_price"] == pytest.approx(
            3.35 / (0.05 * (1 - 0.001))
        )
        assert out.identifier.startswith("bt")

    def test_insufficient_balance_fail_closed(self):
        b = _broker(initial_quote=100.0, price_source=_PriceSource(5000.0))
        order = _mk_order(side="buy", quantity=0.05)  # 需 250 USDT > 100
        out = b._submit_order(order)
        assert out.filled is False
        assert out.error and "insufficient" in out.error.lower()
        assert b._cash == pytest.approx(100.0)  # 资金未动

    def test_min_quote_fail_closed(self):
        b = _broker(initial_quote=100.0, price_source=_PriceSource(10.0))
        order = _mk_order(side="buy", quantity=0.1)  # 0.1×10 = 1 < 3
        out = b._submit_order(order)
        assert out.filled is False
        assert "min order amount" in out.error
        assert b._cash == pytest.approx(100.0)

    def test_min_quote_disabled(self):
        b = _broker(initial_quote=100.0, price_source=_PriceSource(10.0),
                    min_quote_amount=0)
        out = b._submit_order(_mk_order(side="buy", quantity=0.1))
        assert out.filled is True

    def test_no_price_fail_closed(self):
        b = _broker(initial_quote=100.0, price_source=_PriceSource(0.0))
        out = b._submit_order(_mk_order(side="buy", quantity=0.05))
        assert out.filled is False
        assert "no price" in out.error

    def test_price_source_error_fail_closed(self):
        b = _broker(initial_quote=100.0, price_source=_PriceSource(error=True))
        out = b._submit_order(_mk_order(side="buy", quantity=0.05))
        assert out.filled is False
        assert "no price" in out.error

    def test_invalid_side_and_quantity(self):
        b = _broker(price_source=_PriceSource(67.0))
        out = b._submit_order(_mk_order(side="hodl", quantity=1))
        assert "Unsupported side" in out.error
        out = b._submit_order(_mk_order(side="buy", quantity=0))
        assert "Invalid quantity" in out.error

    def test_slippage_makes_buy_costlier(self):
        px = _PriceSource(67.0)
        b = _broker(initial_quote=100.0, price_source=px, slippage=0.01)
        out = b._submit_order(_mk_order(side="buy", quantity=0.05))
        assert out.filled is True
        # 花费 0.05×67×1.01 = 3.3835
        assert b._cash == pytest.approx(100.0 - 0.05 * 67.0 * 1.01)


class TestSell:
    def _bought(self, **kwargs):
        px = _PriceSource(80.0)
        b = _broker(initial_quote=100.0, price_source=px, **kwargs)
        b._submit_order(_mk_order(side="buy", quantity=0.05))
        return b, px

    def test_filled_proceeds_and_fee(self):
        b, _ = self._bought()
        order = _mk_order(side="sell", quantity=0.04)
        out = b._submit_order(order)
        assert out.filled is True
        assert out.error is None
        # 买 0.05×80=4.0（到账 0.04995）；卖 0.04×80×(1-0.001)≈3.1968；持仓剩 0.00995
        assert b._cash == pytest.approx(100.0 - 4.0 + 0.04 * 80.0 * (1 - 0.001))
        assert b._positions["CRCLX"] == pytest.approx(0.05 * (1 - 0.001) - 0.04)

    def test_insufficient_position_fail_closed(self):
        b, _ = self._bought()
        order = _mk_order(side="sell", quantity=0.1)  # 持仓只有 ~0.04995
        out = b._submit_order(order)
        assert out.filled is False
        assert "insufficient" in out.error.lower()
        # 持仓/现金未动
        assert b._positions["CRCLX"] == pytest.approx(0.05 * (1 - 0.001))
        assert b._cash == pytest.approx(100.0 - 4.0)

    def test_sell_empty_position_fail_closed(self):
        b = _broker(price_source=_PriceSource(80.0))
        out = b._submit_order(_mk_order(side="sell", quantity=0.05))
        assert out.filled is False
        assert "insufficient" in out.error.lower()

    def test_slippage_makes_sell_cheaper(self):
        b, _ = self._bought(slippage=0.01)
        out = b._submit_order(_mk_order(side="sell", quantity=0.04))
        assert out.filled is True
        # 买入同样吃 slippage：花费 0.05×80×1.01 = 4.04
        assert b._cash == pytest.approx(
            100.0 - 0.05 * 80.0 * 1.01 + 0.04 * 80.0 * (1 - 0.01) * (1 - 0.001)
        )


class TestBalancesPositions:
    def _bought(self, symbol="CRCLX", price=67.0):
        px = _PriceSource(price)
        b = _broker(initial_quote=100.0, price_source=px)
        b._submit_order(_mk_order(side="buy", quantity=0.05, symbol=symbol))
        return b, px

    def test_balances_shape(self):
        b, _ = self._bought()
        bal = b._balances()
        assert bal["USDT"]["available"] == pytest.approx(100.0 - 3.35)
        assert bal["USDT"]["locked"] == 0.0
        assert bal["CRCLX"]["available"] == pytest.approx(0.05 * (1 - 0.001))

    def test_get_balances_at_broker(self):
        b, px = self._bought(price=70.0)  # 当前 bar 价 70
        cash, pv, total = b._get_balances_at_broker(None, None)
        # buy 0.05×70 = 3.5（价格游标 70，非 67）
        assert cash == pytest.approx(100.0 - 0.05 * 70.0)
        assert pv == pytest.approx(0.05 * (1 - 0.001) * 70.0)
        assert total == pytest.approx(cash + pv)

    def test_pull_positions(self):
        b, px = self._bought()
        positions = b._pull_positions(None)
        assert len(positions) == 1
        pos = positions[0]
        assert pos.asset.symbol == "CRCLX"
        assert pos.quantity == pytest.approx(0.05 * (1 - 0.001))
        assert pos.current_price == pytest.approx(67.0)

    def test_snapshot(self):
        b, px = self._bought(price=70.0)
        snap = b.snapshot()
        assert snap["cash"] == pytest.approx(100.0 - 0.05 * 70.0)
        assert snap["positions_value"] == pytest.approx(0.05 * (1 - 0.001) * 70.0)
        assert snap["total"] == pytest.approx(snap["cash"] + snap["positions_value"])
        assert snap["positions"]["CRCLX"] == pytest.approx(0.05 * (1 - 0.001))


class TestOrderRecon:
    def test_query_order_filled(self):
        b = _broker(price_source=_PriceSource(67.0))
        b._submit_order(_mk_order(side="buy", quantity=0.05))
        oid = b._tracked and next(iter(b._tracked))
        status, filled, left, avg = b._query_order(oid, "CRCLX_USDT")
        assert status == "filled"
        assert filled == pytest.approx(0.05)
        assert left == 0.0
        assert avg == pytest.approx(3.35 / (0.05 * (1 - 0.001)))

    def test_query_order_unknown_failed(self):
        b = _broker(price_source=_PriceSource(67.0))
        status, filled, left, avg = b._query_order("nope", "CRCLX_USDT")
        assert status == "failed"

    def test_pull_broker_order(self):
        b = _broker(price_source=_PriceSource(67.0))
        out = b._submit_order(_mk_order(side="buy", quantity=0.05))
        recon = b._pull_broker_order(out.identifier)
        assert recon is not None
        assert recon.status == "fill" or recon.filled is True

    def test_cancel_and_modify_noop(self):
        b = _broker(price_source=_PriceSource(67.0))
        out = b._submit_order(_mk_order(side="buy", quantity=0.05))
        b.cancel_order(out)  # 不应抛
        b._modify_order(out)  # 不应抛
