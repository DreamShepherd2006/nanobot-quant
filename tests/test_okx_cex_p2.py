"""OKX CEX P2（docs/quant-system.md §33.52.10）：现货原语 + broker + 零行为变化回归。

覆盖范围：
- ``okx_spot`` 原语：数值/取整、合约规格、市价单参数构造、``-USD`` 对 tradeQuoteCcy
  透传、拒单语义、查单状态映射、轮询
- ``OkxCexBroker``：instId 解析、余额/持仓、下单（取整 / minSz / 余额门槛 /
  filled / cancelled / pending）
- 期权 ``spot_cover`` / ``spot_exit`` 调用原语的参数（重构前后逐字一致）
"""

import pytest

from nanobot_quant import okx_sdk as _sdk
from nanobot_quant import okx_spot


# ═══════════════════════════════════════════════════════════
#  okx_spot 原语
# ═══════════════════════════════════════════════════════════

def test_num_handles_none_and_garbage():
    assert okx_spot.num(None) == 0.0
    assert okx_spot.num("") == 0.0
    assert okx_spot.num("1.5") == 1.5
    assert okx_spot.num("bad") == 0.0


def test_floor_step_tolerance_and_invalid_step():
    assert okx_spot.floor_step(0.3, 0.1) == pytest.approx(0.3)   # 浮点容差
    assert okx_spot.floor_step(0.37, 0.1) == pytest.approx(0.3)
    assert okx_spot.floor_step(1.0, 0) == 1.0                    # 步进非法 → 原值
    assert okx_spot.floor_step(1.0, None) == 1.0


def test_instrument_limits_reads_spot(monkeypatch):
    class _P:
        def get_instruments(self, **kw):
            assert kw == {"instType": "SPOT", "instId": "SOL-USDT"}
            return {"code": "0", "data": [
                {"lotSz": "0.001", "minSz": "0.01", "tickSz": "0.01"},
            ]}

    monkeypatch.setattr(_sdk, "public", lambda: _P())
    assert okx_spot.instrument_limits("SOL-USDT") == {
        "lot_sz": 0.001, "min_sz": 0.01, "tick_sz": 0.01,
    }


def test_instrument_limits_empty_raises(monkeypatch):
    class _P:
        def get_instruments(self, **kw):
            return {"code": "0", "data": []}

    monkeypatch.setattr(_sdk, "public", lambda: _P())
    with pytest.raises(okx_spot.OkxSdkError):
        okx_spot.instrument_limits("NOPE-USDT")


class _FakeTrade:
    def __init__(self, captured, response):
        self._captured = captured
        self._response = response

    def set_order(self, **kw):
        self._captured["set_order"] = kw
        return self._response

    def send_request(self, path, method, **kw):
        self._captured["send_request"] = {"path": path, "method": method, **kw}
        return self._response

    def get_order(self, **kw):
        return self._response


_OK = lambda rows: {"code": "0", "data": rows}  # noqa: E731


def test_market_order_params_cash_and_base_ccy(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        _sdk, "trade_for",
        lambda c: _FakeTrade(captured, _OK([{"sCode": "0", "ordId": "A1"}])),
    )
    req = okx_spot.market_order(
        {"api_key": "k"}, inst_id="SOL-USDT", side="buy", sz="0.5", tag="t1",
    )
    assert req["ord_id"] == "A1" and req["rejected"] is False
    p = captured["set_order"]
    # 现货交易不需要保证金 ⇒ 默认 cash（2026-10-30 定稿）；撞 54000/51000 时
    # market_order 内部会自适应切换（期权线 Bot1 显式传 td_mode="cross"）
    assert p["tdMode"] == "cash"
    assert p["ordType"] == "market"
    assert p["tgtCcy"] == "base_ccy"
    assert p["side"] == "buy"
    assert p["tag"] == "t1"


def test_market_order_usd_pair_uses_send_request_with_quote_ccy(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        _sdk, "trade_for",
        lambda c: _FakeTrade(captured, _OK([{"sCode": "0", "ordId": "A2"}])),
    )
    okx_spot.market_order({"api_key": "k"}, inst_id="SOL-USD", side="sell", sz="1")
    sr = captured["send_request"]
    assert sr["tradeQuoteCcy"] == "USDC"
    assert sr["path"] == "/api/v5/trade/order"
    assert sr["method"] == "POST"


def test_market_order_reject_returns_flag_not_raises(monkeypatch):
    """顶层 code=0 但订单级 sCode 非 0（少见路径）→ 返回 rejected 标志。"""
    monkeypatch.setattr(
        _sdk, "trade_for",
        lambda c: _FakeTrade({}, _OK([{"sCode": "51008", "sMsg": "insufficient"}])),
    )
    req = okx_spot.market_order(
        {"api_key": "k"}, inst_id="SOL-USDT", side="buy", sz="1",
    )
    assert req["rejected"] is True
    assert req["ord_id"] == ""
    assert req["s_code"] == "51008"
    assert "insufficient" in req["s_msg"]


def test_market_order_top_level_error_raises(monkeypatch):
    """顶层 code=1（All operations failed）→ check 抛异常。

    这是真实拒单的主路径，与原实现逐字一致（``spot_cover`` 落到台账 failed）。
    """
    monkeypatch.setattr(
        _sdk, "trade_for",
        lambda c: _FakeTrade({}, {
            "code": "1", "msg": "All operations failed",
            "data": [{"sCode": "51008", "sMsg": "insufficient"}],
        }),
    )
    with pytest.raises(okx_spot.OkxSdkError) as exc:
        okx_spot.market_order({"api_key": "k"}, inst_id="SOL-USDT",
                              side="buy", sz="1")
    assert "51008" in str(exc.value)


def test_strict_order_raises_on_reject(monkeypatch):
    monkeypatch.setattr(
        okx_spot, "market_order",
        lambda c, **kw: {"rejected": True, "s_code": "51008",
                         "s_msg": "insufficient", "ord_id": ""},
    )
    with pytest.raises(okx_spot.OkxSdkError):
        okx_spot.strict_order({"api_key": "k"}, inst_id="SOL-USDT",
                              side="buy", sz="1")


def test_strict_order_raises_without_ord_id(monkeypatch):
    monkeypatch.setattr(
        okx_spot, "market_order",
        lambda c, **kw: {"rejected": False, "s_code": "", "s_msg": "", "ord_id": ""},
    )
    with pytest.raises(okx_spot.OkxSdkError):
        okx_spot.strict_order({"api_key": "k"}, inst_id="SOL-USDT",
                              side="buy", sz="1")


@pytest.mark.parametrize("state,expect", [
    ("live", "pending"),
    ("partially_filled", "pending"),
    ("filled", "filled"),
    ("canceled", "cancelled"),
    ("mmp_canceled", "cancelled"),
    ("unknown_state", "unknown_state"),
])
def test_query_order_status_mapping(monkeypatch, state, expect):
    class _T:
        def get_order(self, **kw):
            return _OK([{"state": state, "avgPx": "1.5", "accFillSz": "0.2",
                         "fee": "-0.01", "feeCcy": "USDT"}])

    monkeypatch.setattr(_sdk, "trade_for", lambda c: _T())
    r = okx_spot.query_order({"api_key": "k"}, "SOL-USDT", "O1")
    assert r["status"] == expect
    assert r["avg_px"] == 1.5
    assert r["acc_fill_sz"] == 0.2
    assert r["fee_ccy"] == "USDT"


def test_poll_fill_returns_first_terminal(monkeypatch):
    calls = []

    def fake(creds, inst, oid):
        calls.append(oid)
        return {"status": "filled", "acc_fill_sz": 0.1}

    monkeypatch.setattr(okx_spot, "query_order", fake)
    r = okx_spot.poll_fill({}, "SOL-USDT", "O1", tries=3, interval=0)
    assert r["timed_out"] is False and r["tries_used"] == 1
    assert calls == ["O1"]


def test_poll_fill_timeout_is_not_failure(monkeypatch):
    monkeypatch.setattr(okx_spot, "query_order", lambda c, i, o: {"status": "pending"})
    r = okx_spot.poll_fill({}, "SOL-USDT", "O1", tries=3, interval=0)
    assert r["timed_out"] is True
    assert r["tries_used"] == 3
    assert r["status"] == "pending"   # 调用方按 pending 处理，不判成交也不判失败


# ═══════════════════════════════════════════════════════════
#  OkxCexBroker
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def broker(monkeypatch):
    import nanobot_quant.brokers.okx_cex_broker as bm

    monkeypatch.setattr(
        bm, "account_creds",
        lambda acct="": {"api_key": "k", "secret_key": "s", "passphrase": "p"},
    )
    return bm.OkxCexBroker(
        tokens_json=[{"symbol": "CRCLX", "okx_symbol": "XCRCL"}],
        quote="USDT",
    )


def test_broker_requires_credentials(monkeypatch):
    import nanobot_quant.brokers.okx_cex_broker as bm

    monkeypatch.setattr(bm, "account_creds", lambda acct="": {})
    with pytest.raises(ValueError):
        bm.OkxCexBroker()


def test_broker_inst_id_uses_okx_symbol(broker):
    assert broker.inst_id_of("SOL") == "SOL-USDT"
    assert broker.inst_id_of("CRCLX") == "XCRCL-USDT"   # tokens.json okx_symbol
    assert broker.inst_id_of("AAPL") == "XAAPL-USDT"    # 股票加 X 前缀


def test_broker_inst_id_respects_quote_override(monkeypatch):
    import nanobot_quant.brokers.okx_cex_broker as bm

    monkeypatch.setattr(bm, "account_creds",
                        lambda acct="": {"api_key": "k", "secret_key": "s"})
    b = bm.OkxCexBroker(quote="USDC")
    assert b.inst_id_of("SOL") == "SOL-USDC"


def test_balances_split_cash_and_positions(broker, monkeypatch):
    monkeypatch.setattr(
        broker.__class__, "SOURCE", broker.__class__.SOURCE, raising=False,
    )
    import nanobot_quant.brokers.okx_cex_broker as bm

    monkeypatch.setattr(
        bm, "account_balance",
        lambda acct="": {"details": [
            {"ccy": "USDT", "avail_bal": "100", "frozen_bal": "5", "eq_usd": "105"},
            {"ccy": "SOL", "avail_bal": "2", "frozen_bal": "0", "eq_usd": "300"},
        ]},
    )
    class _Q:
        symbol = "USDT"

    cash, pos, total = broker._get_balances_at_broker(_Q(), None)
    assert cash == 100.0
    assert pos == 300.0
    assert total == 400.0


def test_balances_failure_is_fail_closed(broker, monkeypatch):
    import nanobot_quant.brokers.okx_cex_broker as bm

    def boom(acct=""):
        raise RuntimeError("network down")

    monkeypatch.setattr(bm, "account_balance", boom)
    assert broker._balances() == {}
    assert broker._get_balances_at_broker(None, None) == (0.0, 0.0, 0.0)


def _stub_limits(monkeypatch, lot=0.001, min_sz=0.01):
    from nanobot_quant.brokers.okx_cex_broker import OkxCexBroker

    OkxCexBroker._limits_cache.clear()   # 类级缓存跨用例共享，先清避免串扰
    monkeypatch.setattr(okx_spot, "instrument_limits",
                        lambda inst: {"lot_sz": lot, "min_sz": min_sz, "tick_sz": 0.01})


class _Order:
    """最小 lumibot Order 替身（策略/测试双用）。"""

    def __init__(self, side, symbol, quantity):
        self.side = side
        self.quantity = quantity
        self.custom_params = {}
        from lumibot.entities import Asset

        self.asset = Asset(symbol=symbol, asset_type="crypto")
        self.error = None
        self.status = "unprocessed"

    def set_error(self, msg):
        self.error = msg
        self.status = "error"

    def set_filled(self):
        self.status = "fill"


def test_submit_order_rounds_to_lot_and_fills(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_price_of", lambda s: 100.0)
    monkeypatch.setattr(broker, "_balances", lambda: {
        "USDT": {"available": 1000.0, "locked": 0.0, "eq_usd": 1000.0},
    })
    captured = {}

    def fake_strict(creds, **kw):
        captured.update(kw)
        return {"ord_id": "OID1"}

    monkeypatch.setattr(okx_spot, "strict_order", fake_strict)
    monkeypatch.setattr(okx_spot, "poll_fill",
                        lambda c, i, o, **kw: {"status": "filled", "avg_px": 99.5,
                                               "acc_fill_sz": 0.123, "timed_out": False})

    o = _Order("buy", "SOL", 0.1234)
    broker._submit_order(o)
    assert captured["inst_id"] == "SOL-USDT"
    assert captured["side"] == "buy"
    assert captured["tgt_ccy"] == "base_ccy"
    assert float(captured["sz"]) == pytest.approx(0.123)   # floor(0.1234, 0.001)
    assert o.status == "fill"
    assert o.custom_params["cex"]["avg_price"] == 99.5
    assert o.custom_params["cex"]["ord_id"] == "OID1"


def test_submit_order_rejects_below_min_sz(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=1.0)
    o = _Order("buy", "SOL", 0.5)
    broker._submit_order(o)
    assert o.status == "error"
    assert "minSz" in o.error


def test_submit_order_rejects_buy_when_quote_short(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_price_of", lambda s: 100.0)
    monkeypatch.setattr(broker, "_balances", lambda: {
        "USDT": {"available": 5.0, "locked": 0.0, "eq_usd": 5.0},
    })
    monkeypatch.setattr(okx_spot, "strict_order",
                        lambda c, **kw: pytest.fail("不应下单"))
    o = _Order("buy", "SOL", 1.0)
    broker._submit_order(o)
    assert o.status == "error"
    assert "不足" in o.error


def test_submit_order_sell_shrinks_to_available(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_balances", lambda: {
        "SOL": {"available": 0.2, "locked": 0.0, "eq_usd": 20.0},
        "USDT": {"available": 0.0, "locked": 0.0, "eq_usd": 0.0},
    })
    captured = {}

    def fake_strict(creds, **kw):
        captured.update(kw)
        return {"ord_id": "OID2"}

    monkeypatch.setattr(okx_spot, "strict_order", fake_strict)
    monkeypatch.setattr(okx_spot, "poll_fill",
                        lambda c, i, o, **kw: {"status": "filled", "avg_px": 100.0,
                                               "acc_fill_sz": 0.2, "timed_out": False})
    o = _Order("sell", "SOL", 0.5)
    broker._submit_order(o)
    assert float(captured["sz"]) == pytest.approx(0.2)   # 缩量到可用
    assert o.status == "fill"


def test_submit_order_sell_without_balance_errors(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_balances", lambda: {})
    o = _Order("sell", "SOL", 0.5)
    broker._submit_order(o)
    assert o.status == "error"


def test_submit_order_cancelled_sets_error(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_price_of", lambda s: 100.0)
    monkeypatch.setattr(broker, "_balances", lambda: {
        "USDT": {"available": 1000.0, "locked": 0.0, "eq_usd": 1000.0},
    })
    monkeypatch.setattr(okx_spot, "strict_order", lambda c, **kw: {"ord_id": "OID3"})
    monkeypatch.setattr(okx_spot, "poll_fill",
                        lambda c, i, o, **kw: {"status": "cancelled",
                                               "acc_fill_sz": 0.0, "timed_out": False})
    o = _Order("buy", "SOL", 0.1)
    broker._submit_order(o)
    assert o.status == "error"
    assert "cancelled" in o.error


def test_submit_order_pending_keeps_submitted(broker, monkeypatch):
    _stub_limits(monkeypatch, lot=0.001, min_sz=0.01)
    monkeypatch.setattr(broker, "_price_of", lambda s: 100.0)
    monkeypatch.setattr(broker, "_balances", lambda: {
        "USDT": {"available": 1000.0, "locked": 0.0, "eq_usd": 1000.0},
    })
    monkeypatch.setattr(okx_spot, "strict_order", lambda c, **kw: {"ord_id": "OID4"})
    monkeypatch.setattr(okx_spot, "poll_fill",
                        lambda c, i, o, **kw: {"status": "pending",
                                               "timed_out": True})
    o = _Order("buy", "SOL", 0.1)
    broker._submit_order(o)
    assert o.status == "unprocessed"      # 不判成交、不判失败
    assert o.error is None
    assert "OID4" in broker._tracked


def test_pull_broker_order_reconciles(monkeypatch):
    import nanobot_quant.brokers.okx_cex_broker as bm

    monkeypatch.setattr(bm, "account_creds",
                        lambda acct="": {"api_key": "k", "secret_key": "s"})
    b = bm.OkxCexBroker()
    b._tracked["OID5"] = {"symbol": "SOL", "inst": "SOL-USDT", "side": "buy",
                          "quantity": 0.5}
    monkeypatch.setattr(okx_spot, "query_order",
                        lambda c, i, o: {"status": "filled", "acc_fill_sz": 0.5,
                                         "avg_px": 100.2})
    order = b._pull_broker_order("OID5")
    assert order is not None
    assert order.status == "fill"


def test_pull_broker_order_unknown_returns_none(broker):
    assert broker._pull_broker_order("NOPE") is None


def test_registry_has_okx_cex_shared_pool():
    from nanobot_quant.brokers import registry

    spec = registry.get_broker("okx_cex")
    assert spec.slot_model == "shared_pool"
    assert spec.family == "cex"
    assert spec.data_source == "okx_cex"
    assert spec.quote == "USDT"

# ═══════════════════════════════════════════════════════════
#  期权 spot_cover / spot_exit 委托原语（零行为变化回归）
# ═══════════════════════════════════════════════════════════

def _patch_cover_env(monkeypatch):
    import nanobot_quant.okx_options_trade as ot

    monkeypatch.setattr(
        ot, "_entry_account",
        lambda acct="": {"creds": {"api_key": "k"}, "label": "bot1"},
    )
    monkeypatch.setattr(ot, "add_ledger", lambda **kw: {"id": "e1", **kw})
    monkeypatch.setattr(ot, "update_ledger", lambda *a, **k: None)
    return ot


def test_spot_cover_delegates_with_identical_params(monkeypatch):
    """重构前逐字参数：instId / side=buy / sz / tgtCcy / tag=TAG_COVER。"""
    ot = _patch_cover_env(monkeypatch)
    monkeypatch.setattr(ot, "_settle_cover_entry", lambda creds, entry: entry)
    captured = {}

    def fake(creds, **kw):
        captured.update(kw)
        return {"ord_id": "OID", "rejected": False, "s_code": "", "s_msg": ""}

    monkeypatch.setattr(ot.okx_spot, "market_order", fake)
    ot.spot_cover("", spot_inst="SOL-USDC", base_qty=0.1)
    assert captured == {
        "inst_id": "SOL-USDC",
        "side": "buy",
        "sz": "0.1",
        "tgt_ccy": "base_ccy",
        "tag": ot.TAG_COVER,
        # 期权线子账号是跨币种保证金模式（cash → 51000）⇒ 显式指定
        "td_mode": "cross",
    }


def test_spot_cover_quote_amt_uses_quote_ccy(monkeypatch):
    ot = _patch_cover_env(monkeypatch)
    monkeypatch.setattr(ot, "_settle_cover_entry", lambda creds, entry: entry)
    captured = {}

    def fake(creds, **kw):
        captured.update(kw)
        return {"ord_id": "OID", "rejected": False, "s_code": "", "s_msg": ""}

    monkeypatch.setattr(ot.okx_spot, "market_order", fake)
    ot.spot_cover("", spot_inst="SOL-USDC", quote_amt=12.5)
    assert captured["sz"] == "12.50"
    assert captured["tgt_ccy"] == "quote_ccy"


def test_spot_cover_reject_keeps_ledger_pending(monkeypatch):
    """拒单沿用原语义：不落台账 failed、异常直接抛。"""
    ot = _patch_cover_env(monkeypatch)
    updates = []
    monkeypatch.setattr(ot, "update_ledger",
                        lambda *a, **k: updates.append(k) or None)
    monkeypatch.setattr(
        ot.okx_spot, "market_order",
        lambda c, **kw: {"ord_id": "", "rejected": True,
                         "s_code": "51008", "s_msg": "insufficient"},
    )
    with pytest.raises(ot.OkxSdkError):
        ot.spot_cover("", spot_inst="SOL-USDC", base_qty=0.1)
    assert not any(u.get("status") == "failed" for u in updates)


def test_spot_cover_transport_error_marks_ledger_failed(monkeypatch):
    """传输层异常→台账 failed（与原实现一致）。"""
    ot = _patch_cover_env(monkeypatch)
    updates = []
    monkeypatch.setattr(ot, "update_ledger",
                        lambda *a, **k: updates.append(k) or None)

    def boom(c, **kw):
        raise RuntimeError("network")

    monkeypatch.setattr(ot.okx_spot, "market_order", boom)
    with pytest.raises(RuntimeError):
        ot.spot_cover("", spot_inst="SOL-USDC", base_qty=0.1)
    assert any(u.get("status") == "failed" for u in updates)


def test_spot_exit_delegates_with_identical_params(monkeypatch):
    ot = _patch_cover_env(monkeypatch)
    monkeypatch.setattr(ot, "_settle_exit_entry", lambda creds, entry: entry)
    captured = {}

    def fake(creds, **kw):
        captured.update(kw)
        return {"ord_id": "OID", "rejected": False, "s_code": "", "s_msg": ""}

    monkeypatch.setattr(ot.okx_spot, "market_order", fake)
    ot.spot_exit("", spot_inst="SOL-USDC", base_qty=0.1)
    assert captured == {
        "inst_id": "SOL-USDC",
        "side": "sell",
        "sz": "0.1",
        "tgt_ccy": "base_ccy",
        "tag": ot.TAG_EXIT,
        # 期权线子账号跨币种保证金 ⇒ 显式 cross（现货线走默认 cash）
        "td_mode": "cross",
    }


def test_poll_order_is_thin_wrapper(monkeypatch):
    """`poll_order` 保留旧名、行为转发 `okx_spot.query_order`（调用方零改动）。"""
    import nanobot_quant.okx_options_trade as ot

    monkeypatch.setattr(
        ot.okx_spot, "query_order",
        lambda c, i, o: {"status": "filled", "avg_px": 1.0,
                         "acc_fill_sz": 2.0, "fee": 0.0, "fee_ccy": ""},
    )
    r = ot.poll_order({}, "SOL-USDC", "OID")
    assert r["status"] == "filled"
    assert r["avg_px"] == 1.0
