"""P1: CexBroker unit tests (mock signed REST — no network).

Covered:
- _submit_order: filled / pending / create-error paths
- _get_balances_at_broker / _pull_positions (cash vs position separation)
- cancel_order, sub-account credential selection
"""

from types import SimpleNamespace

import pytest

from nanobot_quant.brokers import cex_broker as mod
from nanobot_quant.brokers.cex_broker import CexBroker

CREDS = {
    "main": {"api_key": "k", "api_secret": "s", "uid": "15119093"},
    "sub_accounts": {
        "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"},
    },
}
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
    )


def _broker(**kwargs):
    return CexBroker(credentials=CREDS, tokens_json=TOKENS, **kwargs)


def _fake_request(monkeypatch, responses):
    """Patch signed_request inside cex_broker module namespace.

    ``from ... import signed_request`` binds the name at import time, so we
    must patch the module attribute (not gate_credentials.signed_request).
    """
    state = {"calls": []}

    def fake(method, path, query="", body="", api_key="", api_secret="", timeout=15):
        state["calls"].append((method, path, query, body))
        if isinstance(responses, list):
            return responses.pop(0)
        return responses

    monkeypatch.setattr(mod, "signed_request", fake)
    return state


class TestSubmitOrder:
    def test_filled(self, monkeypatch):
        state = _fake_request(monkeypatch, [
            (200, {"id": "123", "status": "open", "left": "0.05"}),  # POST create
            (200, {"id": "123", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "74.9"}),  # GET query
        ])
        b = _broker()
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is True
        assert out.identifier == "123"
        assert out.custom_params["cex"]["pair"] == "CRCLXUSDT"
        method, path, query, body = state["calls"][0]
        assert method == "POST" and path == "/api/v4/spot/orders"
        assert "CRCLXUSDT" in body and '"side":"buy"' in body

    def test_create_error(self, monkeypatch):
        _fake_request(monkeypatch, (400, {"label": "INVALID_REQUEST_PARAMETER"}))
        b = _broker()
        order = _mk_order()
        out = b._submit_order(order)
        assert out.error is not None
        assert "INVALID_REQUEST_PARAMETER" in out.error
        assert out.filled is False

    def test_pending(self, monkeypatch):
        _fake_request(monkeypatch, [
            (200, {"id": "9", "status": "open", "left": "0.05"}),  # POST create
            (200, {"id": "9", "status": "open", "left": "0.05"}),  # GET query
        ])
        b = _broker()
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is False
        assert out.error is None

    def test_invalid_quantity(self, monkeypatch):
        state = _fake_request(monkeypatch, [])
        b = _broker()
        order = _mk_order(quantity=0)
        out = b._submit_order(order)
        assert out.error is not None
        assert state["calls"] == []  # no request sent

    def test_unsupported_side(self, monkeypatch):
        _fake_request(monkeypatch, [])
        b = _broker()
        order = _mk_order(side="short")
        out = b._submit_order(order)
        assert "Unsupported side" in out.error

    def test_sub_account_key_used(self):
        b = _broker(sub_account="gate_bot1")
        assert b._api_key == "k1"
        assert b._uid == "59175220"


class TestBalances:
    def test_cash_position_split(self, monkeypatch):
        b = _broker()
        monkeypatch.setattr(
            b, "_balances",
            lambda: {"USDT": {"available": 10.0, "locked": 0.0},
                     "CRCLX": {"available": 0.5, "locked": 0.0}},
        )
        monkeypatch.setattr(b, "_price_of", lambda s: 70.0)
        quote = SimpleNamespace(symbol="USDT")
        cash, pos, total = b._get_balances_at_broker(quote, None)
        assert cash == pytest.approx(10.0)
        assert pos == pytest.approx(35.0)
        assert total == pytest.approx(45.0)

    def test_empty_balances(self, monkeypatch):
        b = _broker()
        monkeypatch.setattr(b, "_balances", lambda: {})
        cash, pos, total = b._get_balances_at_broker(SimpleNamespace(symbol="USDT"), None)
        assert (cash, pos, total) == (0.0, 0.0, 0.0)

    def test_pull_positions(self, monkeypatch):
        b = _broker()
        monkeypatch.setattr(
            b, "_balances",
            lambda: {"USDT": {"available": 10.0, "locked": 0.0},
                     "CRCLX": {"available": 0.5, "locked": 0.0},
                     "VSC": {"available": 24000.0, "locked": 0.0}},
        )
        monkeypatch.setattr(b, "_price_of", lambda s: {"CRCLX": 70.0, "VSC": 0.0}[s])
        positions = b._pull_positions(strategy=None)
        symbols = sorted(p.asset.symbol for p in positions)
        assert symbols == ["CRCLX", "VSC"]


class TestCancelOrder:
    def test_cancel(self, monkeypatch):
        state = _fake_request(monkeypatch, (200, {"id": "123", "status": "cancelled"}))
        b = _broker()
        b._tracked["123"] = {"pair": "CRCLXUSDT", "symbol": "CRCLX"}
        order = SimpleNamespace(identifier="123")
        b.cancel_order(order)
        method, path, query, body = state["calls"][0]
        assert method == "DELETE" and "123" in path and "CRCLXUSDT" in query

    def test_cancel_unknown_order(self, monkeypatch):
        state = _fake_request(monkeypatch, [])
        b = _broker()
        b.cancel_order(SimpleNamespace(identifier="nope"))
        assert state["calls"] == []
