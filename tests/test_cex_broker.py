"""P1: CexBroker unit tests (mock gate-api SDK — no network).

Covered:
- _submit_order: filled / pending / create-error paths
- _get_balances_at_broker / _pull_positions (cash vs position separation)
- cancel_order, sub-account credential selection
- SDK parameter contract (currency_pair / side / amount / order_type / tif)
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


class _FakePriceSource:
    """数据源注册表 stub：按注册表名返回可配置价格（0.0=fail-closed）。"""

    def __init__(self, name, price, calls, error=False):
        self.name = name
        self.price = price
        self.calls = calls
        self.error = error

    def get_price(self, symbol):
        self.calls.append(self.name)
        if self.error:
            raise RuntimeError("ticker down")
        return self.price or 0.0


def _broker(**kwargs):
    return CexBroker(credentials=CREDS, tokens_json=TOKENS, **kwargs)


def _fake_sdk(monkeypatch, responses):
    """Patch gate_sdk functions inside cex_broker module namespace.

    The broker binds ``from nanobot_quant.gate_sdk import ...`` at import
    time, so we must patch the module attributes (not gate_sdk itself).
    ``responses`` is a list consumed per call (or a single value returned
    for every call); entries may be dicts (success) or exceptions (API
    failure — gate_sdk._call wraps ApiException into RuntimeError).
    """
    state = {"calls": []}

    def make(label):
        def fake(*args, **kwargs):
            state["calls"].append((label, args, kwargs))
            if isinstance(responses, list):
                return responses.pop(0)
            return responses

        return fake

    monkeypatch.setattr(mod, "sdk_get_currency_pair", make("pair_meta"))
    monkeypatch.setattr(mod, "sdk_create_order", make("create"))
    monkeypatch.setattr(mod, "sdk_get_order", make("query"))
    monkeypatch.setattr(mod, "sdk_cancel_order", make("cancel"))
    return state


class TestSubmitOrder:
    # CRCLX_USDT on Gate: amount_precision=3, precision=2, min_quote_amount=3 (2026-08-14 实测)
    _PAIR_META = {
        "id": "CRCLX_USDT", "base": "CRCLX", "quote": "USDT",
        "trade_status": "tradable", "amount_precision": 3,
        "precision": 2, "min_quote_amount": 3,
    }

    @pytest.fixture(autouse=True)
    def _clear_pair_meta_cache(self):
        # class-level cache leaks across tests (500s TTL) — isolate per test
        CexBroker._pair_meta_cache.clear()
        yield
        CexBroker._pair_meta_cache.clear()

    def test_filled(self, monkeypatch):
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            {"id": "123", "status": "closed", "left": "0",
             "filled_amount": "0.05", "avg_deal_price": "74.9",
             "finish_as": "filled"},  # create_order
            {"id": "123", "status": "closed", "left": "0",
             "filled_amount": "0.05", "avg_deal_price": "74.9"},  # get_order
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is True
        assert out.identifier == "123"
        assert out.custom_params["cex"]["pair"] == "CRCLX_USDT"
        # SDK contract: get_currency_pair(key, secret, pair)
        label, args, kwargs = state["calls"][0]
        assert label == "pair_meta" and args[2] == "CRCLX_USDT"
        # SDK contract: create_order(key, secret, pair, side, amount, ...)
        label, args, kwargs = state["calls"][1]
        assert label == "create"
        assert args[2] == "CRCLX_USDT" and args[3] == "buy"
        # market BUY: amount = quote 金额 (USDT), 0.05 x 67.0 = 3.35, precision=2
        assert args[4] == "3.35"
        assert kwargs["order_type"] == "market"
        assert kwargs["time_in_force"] == "ioc"
        assert kwargs["text"].startswith("t-nq")

    def test_sell_amount_is_base_quantity(self, monkeypatch):
        # market SELL: amount = base 数量 (CRCLX), amount_precision=3
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            {"id": "55", "status": "closed", "left": "0",
             "filled_amount": "0.05", "avg_deal_price": "67.1"},  # create_order
            {"id": "55", "status": "closed", "left": "0",
             "filled_amount": "0.05", "avg_deal_price": "67.1"},  # get_order
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(side="sell")
        out = b._submit_order(order)
        assert out.filled is True
        label, args, kwargs = state["calls"][1]
        assert label == "create" and args[3] == "sell"
        assert args[4] == "0.050"

    def test_create_error(self, monkeypatch):
        _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            RuntimeError("create_order failed: HTTP 400 INVALID_REQUEST_PARAMETER"),
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.error is not None
        assert "INVALID_REQUEST_PARAMETER" in out.error
        assert out.filled is False

    def test_query_retry_until_closed(self, monkeypatch):
        # Gate 市价单结算异步：下单后立即查询仍 open（SELL 实测），轮询后 closed
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            {"id": "124", "status": "open", "left": "0.05",
             "filled_amount": "0", "avg_deal_price": "0"},  # create_order
            {"id": "124", "status": "open", "left": "0.05",
             "filled_amount": "0", "avg_deal_price": "0"},  # get_order #1: open
            {"id": "124", "status": "closed", "left": "0",
             "filled_amount": "0.05", "avg_deal_price": "74.9"},  # get_order #2: closed
        ])
        monkeypatch.setattr("time.sleep", lambda _: None)
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is True
        assert b._tracked["124"]["filled"] == 0.05
        assert b._tracked["124"]["avg_price"] == 74.9
        # pair_meta → create → get_order#1(open) → get_order#2(closed)：共 4 次 SDK 调用
        assert len(state["calls"]) == 4
        assert [c[0] for c in state["calls"]] == ["pair_meta", "create", "query", "query"]

    def test_pending(self, monkeypatch):
        # create + 10 次轮询均 open → 最终仍 pending（不误报 error）
        _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            {"id": "9", "status": "open", "left": "0.05"},  # create_order
            *[{"id": "9", "status": "open", "left": "0.05"} for _ in range(10)],
        ])
        monkeypatch.setattr("time.sleep", lambda _: None)
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is False
        assert out.error is None

    def test_min_quote_reject(self, monkeypatch):
        _fake_sdk(monkeypatch, [self._PAIR_META])
        b = _broker()
        # price 67.0 × qty 0.02 = $1.34 < min_quote 3 → fail-closed reject
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(quantity=0.02)
        out = b._submit_order(order)
        assert out.error is not None
        assert "below min order amount 3 USDT" in out.error
        assert "1.34" in out.error
        assert out.filled is False

    def test_buy_no_price_rejects(self, monkeypatch):
        _fake_sdk(monkeypatch, [self._PAIR_META])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 0.0)  # price unknown
        order = _mk_order()
        out = b._submit_order(order)
        assert out.error is not None
        assert "no price for CRCLX" in out.error
        assert out.filled is False

    def test_price_of_gate_ticker_first(self, monkeypatch):
        """同所取价：gate_cex 源有价 → 直接用，不调 OKX。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, 67.2 if name == "gate_cex" else None, calls))
        b = _broker()
        assert b._price_of("CRCLX") == 67.2
        assert calls == ["gate_cex"]

    def test_price_of_okx_fallback(self, monkeypatch):
        """Gate 取价失败 → OKX CEX 兜底（注册表 okx_cex 源）。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, None if name == "gate_cex" else 68.5, calls))
        b = _broker()
        assert b._price_of("CRCLX") == 68.5
        assert calls == ["gate_cex", "okx_cex"]

    def test_price_of_gate_error_falls_to_okx(self, monkeypatch):
        """Gate ticker 抛异常 → OKX 兜底。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, None if name == "gate_cex" else 68.5, calls,
            error=(name == "gate_cex")))
        b = _broker()
        assert b._price_of("CRCLX") == 68.5
        assert calls == ["gate_cex", "okx_cex"]

    def test_meta_unavailable_fail_closed(self, monkeypatch):
        # pair meta fetch fails → fail-closed: refuse to place blind order
        CexBroker._pair_meta_cache.clear()  # isolate from earlier tests
        _fake_sdk(monkeypatch, [
            RuntimeError("get_currency_pair failed: HTTP 500 SERVER_ERROR"),
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(quantity=0.02)
        out = b._submit_order(order)
        assert out.error is not None
        assert "pair metadata unavailable" in out.error
        assert out.filled is False

    def test_invalid_quantity(self, monkeypatch):
        state = _fake_sdk(monkeypatch, [])
        b = _broker()
        order = _mk_order(quantity=0)
        out = b._submit_order(order)
        assert out.error is not None
        assert state["calls"] == []  # no request sent

    def test_unsupported_side(self, monkeypatch):
        _fake_sdk(monkeypatch, [])
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
        state = _fake_sdk(monkeypatch, {"id": "123", "status": "cancelled"})
        b = _broker()
        b._tracked["123"] = {"pair": "CRCLX_USDT", "symbol": "CRCLX"}
        order = SimpleNamespace(identifier="123")
        b.cancel_order(order)
        label, args, kwargs = state["calls"][0]
        assert label == "cancel"
        assert args[2] == "123" and args[3] == "CRCLX_USDT"

    def test_cancel_unknown_order(self, monkeypatch):
        state = _fake_sdk(monkeypatch, [])
        b = _broker()
        b.cancel_order(SimpleNamespace(identifier="nope"))
        assert state["calls"] == []
