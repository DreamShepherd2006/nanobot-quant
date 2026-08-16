"""Tests for the official gate-api SDK wrapper (nanobot_quant.gate_sdk).

The wrapper converts SDK model objects to plain dicts and ApiException to
RuntimeError; these tests exercise parameter construction and error
conversion using fake SpotApi/WalletApi objects (gate_api stub comes from
tests/conftest.py).
"""

import sys

import pytest

from nanobot_quant import gate_sdk

sys.path.insert(0, "src")  # noqa: E402  (not needed at runtime; keeps IDE happy)

import gate_api  # noqa: E402


class _FakeSpotApi:
    def __init__(self):
        self.calls = []

    def create_order(self, order, **kwargs):
        self.calls.append(("create_order", order))
        return order

    def get_order(self, order_id, currency_pair, **kwargs):
        self.calls.append(("get_order", order_id, currency_pair))
        return gate_api.Order(id=order_id, currency_pair=currency_pair, status="closed")

    def cancel_order(self, order_id, currency_pair, **kwargs):
        self.calls.append(("cancel_order", order_id, currency_pair))
        return gate_api.Order(id=order_id, currency_pair=currency_pair, status="cancelled")

    def get_currency_pair(self, currency_pair, **kwargs):
        self.calls.append(("get_currency_pair", currency_pair))
        return gate_api.CurrencyPair(
            currency_pair=currency_pair,
            trade_status="tradable",
            min_quote_amount="3",
            amount_precision=3,
            precision=2,
        )


class _FakeWalletApi:
    def __init__(self):
        self.calls = []

    def transfer_with_sub_account(self, sub_account_transfer, **kwargs):
        self.calls.append(("transfer", sub_account_transfer))
        return sub_account_transfer

    def list_sub_account_balances(self, sub_account_id=None, currency=None, **kwargs):
        self.calls.append(("balances", sub_account_id))
        return [
            gate_api.SubAccountBalance(
                currency="USDT", available="10.0", locked="0", sub_account_id=sub_account_id
            )
        ]


@pytest.fixture
def fake_spot(monkeypatch):
    fake = _FakeSpotApi()
    monkeypatch.setattr(gate_sdk, "make_spot_api", lambda k, s: fake)
    return fake


@pytest.fixture
def fake_wallet(monkeypatch):
    fake = _FakeWalletApi()
    monkeypatch.setattr(gate_sdk, "make_wallet_api", lambda k, s: fake)
    return fake


def _order_arg(fake):
    return fake.calls[-1][1]


class TestCreateOrder:
    def test_market_order_params(self, fake_spot):
        gate_sdk.create_order("k", "s", "CRCLX_USDT", "buy", "10", order_type="market")
        assert fake_spot.calls[0][0] == "create_order"
        order = _order_arg(fake_spot)
        assert order.currency_pair == "CRCLX_USDT"
        assert order.side == "buy"
        assert order.amount == "10"
        assert order.type == "market"
        # market order must NOT carry time_in_force (Gate rejects gtc) —
        # assert on the serialized dict, which is what the SDK sends
        assert "time_in_force" not in order.to_dict()

    def test_limit_order_requires_price(self, fake_spot):
        with pytest.raises(RuntimeError, match="限价单必须提供 price"):
            gate_sdk.create_order("k", "s", "CRCLX_USDT", "buy", "10", order_type="limit")
        gate_sdk.create_order("k", "s", "CRCLX_USDT", "buy", "10", order_type="limit", price="70.5")
        order = _order_arg(fake_spot)
        assert order.price == "70.5"
        assert order.type == "limit"

    def test_missing_required_params(self, fake_spot):
        with pytest.raises(RuntimeError, match="缺少必要参数"):
            gate_sdk.create_order("k", "s", "", "buy", "10")

    def test_market_sell_passes_amount_through(self, fake_spot):
        gate_sdk.create_order("k", "s", "SOL_USDT", "sell", "0.5", order_type="market")
        order = _order_arg(fake_spot)
        assert order.side == "sell"
        assert order.amount == "0.5"


class TestQuery:
    def test_get_order(self, fake_spot):
        out = gate_sdk.get_order("k", "s", "1114139348977", "CRCLX_USDT")
        assert out["status"] == "closed"
        assert fake_spot.calls[0] == ("get_order", "1114139348977", "CRCLX_USDT")

    def test_cancel_order(self, fake_spot):
        out = gate_sdk.cancel_order("k", "s", "1114139348977", "CRCLX_USDT")
        assert out["status"] == "cancelled"

    def test_get_currency_pair(self, fake_spot):
        out = gate_sdk.get_currency_pair("k", "s", "CRCLX_USDT")
        assert out["currency_pair"] == "CRCLX_USDT"
        assert out["min_quote_amount"] == "3"


class TestTransfer:
    def test_transfer_to_sub(self, fake_wallet):
        out = gate_sdk.transfer_to_sub("k", "s", "USDT", "59175220", "5")
        assert fake_wallet.calls[0][0] == "transfer"
        t = fake_wallet.calls[0][1]
        assert t.currency == "USDT"
        assert t.sub_account == "59175220"
        assert t.amount == "5"
        assert t.direction == "deposit"
        assert out["sub_account"] == "59175220"

    def test_transfer_direction_withdraw(self, fake_wallet):
        gate_sdk.transfer_to_sub("k", "s", "USDT", "59175220", "5", direction="withdraw")
        assert fake_wallet.calls[0][1].direction == "withdraw"

    def test_transfer_missing_params(self, fake_wallet):
        with pytest.raises(RuntimeError, match="缺少必要参数"):
            gate_sdk.transfer_to_sub("k", "s", "USDT", "", "5")

    def test_sub_account_balances(self, fake_wallet):
        out = gate_sdk.sub_account_balances("k", "s", "59175220")
        assert fake_wallet.calls[0] == ("balances", "59175220")
        assert out[0]["currency"] == "USDT"
        assert out[0]["sub_account_id"] == "59175220"


class TestErrors:
    def test_missing_key_raises(self):
        with pytest.raises(RuntimeError, match="未配置"):
            gate_sdk.create_order("", "", "CRCLX_USDT", "buy", "10")

    def test_api_exception_converted(self, fake_spot, monkeypatch):
        def boom(*args, **kwargs):
            raise gate_api.ApiException(status=400, reason="INVALID_CURRENCY_PAIR")

        monkeypatch.setattr(fake_spot, "get_currency_pair", boom)
        with pytest.raises(RuntimeError, match="get_currency_pair failed: HTTP 400 INVALID_CURRENCY_PAIR"):
            gate_sdk.get_currency_pair("k", "s", "CRCLX_USDT")

    def test_spot_accounts_delegates(self, monkeypatch):
        called = {}

        def fake_fetch(api_key, api_secret):
            called["k"] = api_key
            return {"USDT": {"available": 1.0, "locked": 0.0}}

        monkeypatch.setattr(gate_sdk, "fetch_spot_balances", fake_fetch)
        out = gate_sdk.spot_accounts("k", "s")
        assert out == {"USDT": {"available": 1.0, "locked": 0.0}}
        assert called["k"] == "k"
