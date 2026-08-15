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
        state = _fake_request(monkeypatch, [
            (200, self._PAIR_META),  # GET pair meta
            (201, {"id": "123", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "74.9",
                   "finish_as": "filled"}),  # POST create (Gate returns 201)
            (200, {"id": "123", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "74.9"}),  # GET query
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is True
        assert out.identifier == "123"
        assert out.custom_params["cex"]["pair"] == "CRCLX_USDT"
        method, path, query, body = state["calls"][0]
        assert method == "GET" and "currency_pairs" in path
        method, path, query, body = state["calls"][1]
        assert method == "POST" and path == "/api/v4/spot/orders"
        assert "CRCLX_USDT" in body and '"side":"buy"' in body
        # market BUY: amount = quote 金额 (USDT), 0.05 x 67.0 = 3.35, precision=2
        assert '"amount":"3.35"' in body

    def test_sell_amount_is_base_quantity(self, monkeypatch):
        # market SELL: amount = base 数量 (CRCLX), amount_precision=3
        state = _fake_request(monkeypatch, [
            (200, self._PAIR_META),  # GET pair meta
            (201, {"id": "55", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "67.1"}),  # POST create
            (200, {"id": "55", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "67.1"}),  # GET query
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(side="sell")
        out = b._submit_order(order)
        assert out.filled is True
        method, path, query, body = state["calls"][1]
        assert method == "POST" and '"side":"sell"' in body
        assert '"amount":"0.050"' in body

    def test_create_error(self, monkeypatch):
        _fake_request(monkeypatch, [
            (200, self._PAIR_META),  # GET pair meta
            (400, {"label": "INVALID_REQUEST_PARAMETER"}),  # POST create
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
        state = _fake_request(monkeypatch, [
            (200, self._PAIR_META),  # GET pair meta
            (201, {"id": "124", "status": "open", "left": "0.05",
                   "filled_amount": "0", "avg_deal_price": "0"}),  # POST create
            (200, {"id": "124", "status": "open", "left": "0.05",
                   "filled_amount": "0", "avg_deal_price": "0"}),  # query #1: open
            (200, {"id": "124", "status": "closed", "left": "0",
                   "filled_amount": "0.05", "avg_deal_price": "74.9"}),  # query #2: closed
        ])
        monkeypatch.setattr("time.sleep", lambda _: None)
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is True
        assert b._tracked["124"]["filled"] == 0.05
        assert b._tracked["124"]["avg_price"] == 74.9
        # POST → query#1(open) → query#2(closed)：共 3 次请求在 create 之后
        assert len(state["calls"]) == 4

    def test_pending(self, monkeypatch):
        # POST create + 10 次轮询均 open → 最终仍 pending（不误报 error）
        _fake_request(monkeypatch, [
            (200, self._PAIR_META),  # GET pair meta
            (200, {"id": "9", "status": "open", "left": "0.05"}),  # POST create
            *[(200, {"id": "9", "status": "open", "left": "0.05"}) for _ in range(10)],
        ])
        monkeypatch.setattr("time.sleep", lambda _: None)
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order()
        out = b._submit_order(order)
        assert out.filled is False
        assert out.error is None

    def test_min_quote_reject(self, monkeypatch):
        _fake_request(monkeypatch, [(200, self._PAIR_META)])
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
        _fake_request(monkeypatch, [(200, self._PAIR_META)])
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
        _fake_request(monkeypatch, [(500, {"label": "SERVER_ERROR"})])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(quantity=0.02)
        out = b._submit_order(order)
        assert out.error is not None
        assert "pair metadata unavailable" in out.error
        assert out.filled is False

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
        b._tracked["123"] = {"pair": "CRCLX_USDT", "symbol": "CRCLX"}
        order = SimpleNamespace(identifier="123")
        b.cancel_order(order)
        method, path, query, body = state["calls"][0]
        assert method == "DELETE" and "123" in path and "CRCLX_USDT" in query

    def test_cancel_unknown_order(self, monkeypatch):
        state = _fake_request(monkeypatch, [])
        b = _broker()
        b.cancel_order(SimpleNamespace(identifier="nope"))
        assert state["calls"] == []

