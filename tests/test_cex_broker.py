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
        # 实际成交均价回填（avg_deal_price，含手续费摊薄）——交易记录「成交价」列
        assert out.custom_params["cex"]["avg_price"] == 74.9
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

    def test_sell_amount_floors_not_rounds(self, monkeypatch):
        # BALANCE_NOT_ENOUGH 根因回归（2026-08-20 实测）：3.07 买入扣 0.1% 手续费后
        # 实际到账 3.06693；round 到 3 位小数得 3.067 > 余额 → Gate 拒单。
        # 修复：SELL amount 向下取整（floor）到 amount_precision=3 → 3.066。
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META,  # get_currency_pair
            {"id": "55", "status": "closed", "left": "0",
             "filled_amount": "3.066", "avg_deal_price": "1.3029"},  # create_order
            {"id": "55", "status": "closed", "left": "0",
             "filled_amount": "3.066", "avg_deal_price": "1.3029"},  # get_order
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 1.30)
        order = _mk_order(side="sell", quantity=3.06693)
        out = b._submit_order(order)
        assert out.filled is True
        label, args, kwargs = state["calls"][1]
        assert label == "create" and args[3] == "sell"
        assert args[4] == "3.066"  # floor，非 round 的 3.067

    def test_sell_amount_below_decimals_rejects(self, monkeypatch):
        # amount 小于 1/10^ap（floor 后为 0）→ fail-closed 拒绝，不发单。
        # 注：min_quote>0 时该分支被 min_quote 检查先行拦截，此处用 min_quote=0
        # 的交易对元数据直接覆盖 floor-0 分支。
        meta = dict(self._PAIR_META, min_quote_amount=0)
        state = _fake_sdk(monkeypatch, [meta])  # 只查 pair meta，不应调 create
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 67.0)
        order = _mk_order(side="sell", quantity=0.0004)
        out = b._submit_order(order)
        assert out.error is not None
        assert "below 3 decimals" in out.error
        assert out.filled is False
        labels = [c[0] for c in state["calls"]]
        assert "create" not in labels  # 未发单

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

    def test_price_of_both_fail_blacklists(self, monkeypatch):
        """gate+okx 均无价 → 进黑名单，后续调用不再查询（用户自行处理后重启循环恢复）。"""
        from nanobot_quant.gate_cex_data import blacklist_reason, clear_blacklist
        clear_blacklist()
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, None, calls))
        b = _broker()
        assert b._price_of("VSC") == 0.0
        assert calls == ["gate_cex", "okx_cex"]
        assert blacklist_reason("VSC") and "无行情" in blacklist_reason("VSC")
        # 黑名单内：静默短路，不再发任何查询
        assert b._price_of("VSC") == 0.0
        assert calls == ["gate_cex", "okx_cex"]  # 未新增调用
        clear_blacklist()

    def test_price_of_blacklisted_short_circuits(self, monkeypatch):
        """黑名单内的币直接 0.0，不调数据源（gate ticker 400 进黑名单场景）。"""
        from nanobot_quant.gate_cex_data import clear_blacklist, mark_blacklisted
        clear_blacklist()
        mark_blacklisted("VSC", "Gate 已下架/无行情 (delisted)")
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, 99.0, calls))
        b = _broker()
        assert b._price_of("VSC") == 0.0
        assert calls == []  # gate + okx 都不查
        clear_blacklist()

    def test_price_of_cache_second_call_no_network(self, monkeypatch):
        """短 TTL 缓存：同轮内对同一币重复取价只发一次网络请求。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, 67.2 if name == "gate_cex" else None, calls))
        b = _broker()
        assert b._price_of("CRCLX") == 67.2
        assert b._price_of("CRCLX") == 67.2   # 缓存命中
        assert b._price_of("CRCLX") == 67.2
        assert calls == ["gate_cex"]          # 只查了一次

    def test_price_of_cache_expires_after_ttl(self, monkeypatch):
        """TTL 过期后重新查询（覆盖过期路径，不 mock 全局 time）。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, 67.2 if name == "gate_cex" else None, calls))
        b = _broker()
        b._PRICE_CACHE_TTL = -1.0  # 强制立即过期
        assert b._price_of("CRCLX") == 67.2
        assert b._price_of("CRCLX") == 67.2
        assert calls == ["gate_cex", "gate_cex"]  # 每次重新查询

    def test_price_of_cache_per_symbol(self, monkeypatch):
        """缓存按 symbol 区分——不同币互不污染（子账号多持仓币各取各的价）。"""
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, 67.2 if name == "gate_cex" else None, calls))
        b = _broker()
        assert b._price_of("CRCLX") == 67.2
        assert b._price_of("RENDER") == 67.2
        assert b._price_of("CRCLX") == 67.2   # CRCLX 缓存命中
        assert calls == ["gate_cex", "gate_cex"]  # RENDER 是新币，重新查

    def test_price_of_zero_not_cached(self, monkeypatch):
        """0 价不写缓存——临时失败不会污染；黑名单仍由黑名单机制覆盖。"""
        from nanobot_quant.gate_cex_data import clear_blacklist, blacklist_reason
        clear_blacklist()
        calls = []
        monkeypatch.setattr(mod, "get_data_source", lambda name: _FakePriceSource(
            name, None, calls))
        b = _broker()
        assert b._price_of("VSC") == 0.0
        assert b._price_cache.get("VSC") is None  # 0 不入缓存
        assert blacklist_reason("VSC")            # 但进了黑名单（永久短路）
        clear_blacklist()

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

class TestBuyAmountPrecisionFloor:
    """2026-08-26：Gate 市价单 amount_precision 截断（实测 ETH 下单
    0.00126091 实际成交 0.0012、退回 $0.146）——买入前按精度向下取整：
    ① 预检用取整后金额（防「截断后 < min_quote 卖出必卡 slot」）
    ② 下单金额用取整后数量×现价（避免 Gate 内部二次截断浪费金额）。"""

    # ETH_USDT: amount_precision=4（2026-08-26 实盘日志 filled=0.0012 确认）
    _PAIR_META_ETH = {
        "id": "ETH_USDT", "base": "ETH", "quote": "USDT",
        "trade_status": "tradable", "amount_precision": 4,
        "precision": 2, "min_quote_amount": 3,
    }

    @pytest.fixture(autouse=True)
    def _clear_pair_meta_cache(self):
        CexBroker._pair_meta_cache.clear()
        yield
        CexBroker._pair_meta_cache.clear()

    def test_buy_floors_to_precision_before_min_quote(self, monkeypatch):
        """3.1U ETH（qty=0.00126091）→ 取整 0.0012 → $2.95 < $3 → fail-closed
        拒单——不产生「截断后卖出必卡」的仓位（2026-08-25 实盘卡 slot 案例）。"""
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META_ETH,  # get_currency_pair
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 2461.4)
        order = _mk_order(quantity=0.00126091, symbol="ETH")
        out = b._submit_order(order)
        assert out.filled is False
        assert "below min order amount 3 USDT" in out.error
        assert "after 4-dec precision floor" in out.error
        # 拒单只查了交易对规则，未发单
        assert len(state["calls"]) == 1
        assert state["calls"][0][0] == "pair_meta"

    def test_buy_quote_uses_floored_qty(self, monkeypatch):
        """3.5U ETH（qty=0.0014239）→ 取整 0.0014 → 下单金额 0.0014×2461.4=3.446
        （precision=2 → "3.45"）；filled 写入 custom_params.cex.filled。"""
        state = _fake_sdk(monkeypatch, [
            self._PAIR_META_ETH,  # get_currency_pair
            {"id": "123", "status": "closed", "left": "0",
             "filled_amount": "0.0014", "avg_deal_price": "2461.6"},  # create
            {"id": "123", "status": "closed", "left": "0",
             "filled_amount": "0.0014", "avg_deal_price": "2461.6"},  # query
        ])
        b = _broker()
        monkeypatch.setattr(b, "_price_of", lambda symbol: 2461.4)
        order = _mk_order(quantity=0.0014239, symbol="ETH")
        out = b._submit_order(order)
        assert out.filled is True
        label, args, kwargs = state["calls"][1]
        assert label == "create" and args[3] == "buy"
        assert args[4] == "3.45"
        # 实际成交数量回填（amount_precision 截断后）——台账据此建仓
        assert out.custom_params["cex"]["filled"] == 0.0014
    def test_min_quote_for_returns_pair_min_quote(self, monkeypatch):
        """min_quote_for：从交易对规则缓存返回 min_quote_amount（2026-08-26
        B 方案——策略卖出前预检用，价值 < min_quote 释放台账不卖）。"""
        state = _fake_sdk(monkeypatch, [self._PAIR_META_ETH])
        b = _broker()
        assert b.min_quote_for("ETH") == 3.0
        assert state["calls"][0][0] == "pair_meta"
