"""OKX 期权执行层适配单测：Asset↔instId 映射 / Broker 方法转发 / 期权数据源。

全程离线（monkeypatch okx_options_trade / okx_options_data），验证适配层语义：
方向路由（put/call、开/平）、保护价 fail-closed、multiplier 贯通、持仓符号、
期权链结构。不触网、不下单。
"""

from __future__ import annotations

import datetime as dt

import pytest

from nanobot_quant import okx_options_assets as oa
from nanobot_quant import okx_options_trade as oot

EXP = dt.date(2026, 9, 18)


# ─────────────────────── Asset ↔ instId ───────────────────────
class TestAssetMapping:
    def test_inst_to_asset_carries_lot_multiplier(self):
        a = oa.inst_to_asset("SOL-USD_UM-260918-94-P")
        assert (a.symbol, a.strike, a.right) == ("SOL", 94.0, "PUT")
        assert a.expiration == EXP
        assert a.multiplier == 0.1  # 每张面值，而非美股默认 100

    def test_btc_lot_is_0_01(self):
        assert oa.inst_to_asset("BTC-USD_UM-260918-56000-C").multiplier == 0.01

    def test_roundtrip_integer_and_fractional_strike(self):
        for inst in ("SOL-USD_UM-260918-94-P", "BTC-USD_UM-260918-101.5-P",
                     "ETH-USD_UM-260930-4000-C"):
            assert oa.asset_to_inst(oa.inst_to_asset(inst)) == inst

    def test_unknown_family_fails_closed(self):
        assert oa.inst_to_asset("FOO-USD_UM-260918-94-P") is None
        assert oa.inst_to_asset("garbage") is None

    def test_family_helpers(self):
        assert oa.family_of("sol") == "SOL-USD_UM"
        assert oa.base_of_family("SOL-USD_UM") == "SOL"
        assert oa.family_of_inst("SOL-USD_UM-260918-94-P") == "SOL-USD_UM"

    def test_make_option_asset_guards_unknown_family(self):
        from nanobot_quant.brokers.okx_options_broker import make_option_asset
        assert make_option_asset("SOL", EXP, 94, "PUT").multiplier == 0.1
        with pytest.raises(ValueError):
            make_option_asset("FOO", EXP, 94, "PUT")


# ─────────────────────── Broker：下单路由 ───────────────────────
def _broker():
    from nanobot_quant.brokers.okx_options_broker import OkxOptionsBroker
    return OkxOptionsBroker(account="DreamShepherdbot1")


def _order(inst, side, qty=1, base=""):
    from lumibot.entities import Order
    return Order(strategy=None, asset=oa.inst_to_asset(inst),
                 quantity=qty, side=side)


class TestBrokerSubmitOrder:
    def _patch_px(self, monkeypatch, px=0.27):
        monkeypatch.setattr(oot, "suggest_px_for_order",
                            lambda inst, side, sz=None: {"px": px, "side": side})

    def test_sell_put_routes_to_open_put_with_ioc(self, monkeypatch):
        self._patch_px(monkeypatch)
        seen = {}

        def _op(acc, **kw):
            seen.update({"acc": acc, **kw})
            return {"ord_id": "111", "status": "pending"}

        monkeypatch.setattr(oot, "open_put", _op)
        order = _broker()._submit_order(_order("SOL-USD_UM-260918-94-P", "sell"))
        assert seen["inst_id"] == "SOL-USD_UM-260918-94-P"
        assert seen["sz"] == 1 and seen["ord_type"] == "ioc" and seen["px"] == 0.27
        assert seen["acc"] == "DreamShepherdbot1"
        assert order.identifier == "111" and order.error is None

    def test_sell_call_routes_to_open_call_with_cost_basis(self, monkeypatch):
        self._patch_px(monkeypatch, px=0.5)
        seen = {}
        monkeypatch.setattr(oot, "open_call",
                            lambda acc, **kw: (seen.update(kw) or {"ord_id": "222"}))
        b = _broker()
        b._cost_basis_map = {"SOL": 101.0}
        b._submit_order(_order("SOL-USD_UM-260918-110-C", "sell"))
        assert seen["inst_id"].endswith("-110-C")
        assert seen["cost_basis"] == 101.0  # covered call 保本门参数透传

    def test_buy_routes_to_close_put(self, monkeypatch):
        self._patch_px(monkeypatch)
        seen = {}
        monkeypatch.setattr(oot, "close_put",
                            lambda acc, **kw: (seen.update(kw) or {"ord_id": "333"}))
        _broker()._submit_order(_order("SOL-USD_UM-260918-94-P", "buy"))
        assert seen["inst_id"] == "SOL-USD_UM-260918-94-P"
        assert seen["sz"] == 1

    def test_no_price_is_fail_closed(self, monkeypatch):
        monkeypatch.setattr(oot, "suggest_px_for_order",
                            lambda inst, side, sz=None: {"px": None})
        called = []
        monkeypatch.setattr(oot, "open_put", lambda acc, **kw: called.append(kw))
        order = _broker()._submit_order(_order("SOL-USD_UM-260918-94-P", "sell"))
        assert called == []
        assert "fail-closed" in (order.error or "")

    def test_order_error_is_visible(self, monkeypatch):
        self._patch_px(monkeypatch)

        def _boom(acc, **kw):
            raise RuntimeError("[51000] Parameter tdMode error")

        monkeypatch.setattr(oot, "open_put", _boom)
        order = _broker()._submit_order(_order("SOL-USD_UM-260918-94-P", "sell"))
        assert "51000" in (order.error or "")

    def test_zero_qty_rejected(self, monkeypatch):
        self._patch_px(monkeypatch)
        order = _broker()._submit_order(
            _order("SOL-USD_UM-260918-94-P", "sell", qty=0))
        assert "正整数" in (order.error or "")


# ─────────────────────── Broker：持仓 / 余额 / 查单 ───────────────────────
class TestBrokerState:
    def test_pull_positions_maps_short_to_negative_with_lot(self, monkeypatch):
        monkeypatch.setattr(oot, "open_puts", lambda account="": [
            {"inst_id": "SOL-USD_UM-260918-94-P", "side": "short", "pos": 1.0,
             "avg_px": 0.28, "mark_px": 0.26, "strike": 94.0},
            {"inst_id": "BTC-USD_UM-260918-56000-C", "side": "short", "pos": 2.0,
             "avg_px": 40.0, "mark_px": 44.0, "strike": 56000.0},
        ])
        positions = _broker()._pull_positions(strategy=None)
        assert [p.quantity for p in positions] == [-1.0, -2.0]  # 卖开 = 空头
        assert [p.asset.multiplier for p in positions] == [0.1, 0.01]
        assert positions[0].current_price == 0.26

    def test_pull_position_filters_by_asset(self, monkeypatch):
        monkeypatch.setattr(oot, "open_puts", lambda account="": [
            {"inst_id": "SOL-USD_UM-260918-94-P", "side": "short", "pos": 1.0,
             "avg_px": 0.28, "mark_px": 0.26},
        ])
        b = _broker()
        hit = b._pull_position(None, oa.inst_to_asset("SOL-USD_UM-260918-94-P"))
        miss = b._pull_position(None, oa.inst_to_asset("SOL-USD_UM-260918-90-P"))
        assert hit is not None and miss is None

    def test_balance_splits_cash_and_option_value(self, monkeypatch):
        monkeypatch.setattr(oot, "account_balance", lambda account="": {
            "total_eq_usd": 1200.0,
            "details": [
                {"ccy": "USDC", "avail_bal": 900.0, "cash_bal": 950.0},
                {"ccy": "BTC", "avail_bal": 0.01, "cash_bal": 0.01},
            ],
        })
        monkeypatch.setattr(oot, "open_puts", lambda account="": [
            {"inst_id": "SOL-USD_UM-260918-94-P", "side": "short", "pos": 2.0,
             "avg_px": 0.28, "mark_px": 0.5},
        ])
        cash, positions_value, _ = _broker()._get_balances_at_broker(None, None)
        assert cash == 900.0                      # 只汇总现金币种
        assert positions_value == pytest.approx(2 * 0.1 * 0.5)

    def test_pull_broker_order_uses_creds(self, monkeypatch):
        monkeypatch.setattr(oot, "account_creds", lambda account="": {"main": {"api_key": "x"}})
        monkeypatch.setattr(oot, "poll_order",
                            lambda creds, inst_id, ord_id: {"status": "filled",
                                                            "ord_id": ord_id})
        b = _broker()
        b._tracked["777"] = {"inst_id": "SOL-USD_UM-260918-94-P"}
        assert b._pull_broker_order("777")["status"] == "filled"
        assert b._pull_broker_order("unknown") is None  # 未跟踪 → 不猜

    def test_parse_broker_order_roundtrip(self):
        b = _broker()
        order = b._parse_broker_order(
            {"inst_id": "SOL-USD_UM-260918-94-P", "sz": "3", "side": "sell",
             "ord_id": "888", "px": "0.3"}, "okx_options")
        assert order.quantity == 3 and order.side == "sell"
        assert order.asset.multiplier == 0.1
        assert b._parse_broker_order({"inst_id": "junk"}, "s") is None


# ─────────────────────── 期权数据源 ───────────────────────
class TestOptionsDataSource:
    def _ds(self):
        from nanobot_quant.data.okx_options_data_source import OkxOptionsDataSource
        return OkxOptionsDataSource()

    def test_chains_structure_and_multiplier(self, monkeypatch):
        from nanobot_quant import okx_options_data as ood
        monkeypatch.setattr(ood, "fetch_chain", lambda family, **kw: {
            "lot_coin": 0.1,
            "groups": [{"date": "2026-09-18", "rows": [
                {"strike": 90.0, "C": {"inst_id": "SOL-USD_UM-260918-90-C"},
                 "P": {"inst_id": "SOL-USD_UM-260918-90-P"}},
                {"strike": 94.0, "C": {"inst_id": ""},
                 "P": {"inst_id": "SOL-USD_UM-260918-94-P"}},
            ]}],
        })
        from lumibot.entities import Asset
        chains = self._ds().get_chains(Asset(symbol="SOL", asset_type="stock"))
        assert chains["Multiplier"] == "0.1"
        assert chains["Chains"]["PUT"]["2026-09-18"] == [90.0, 94.0]
        assert chains["Chains"]["CALL"]["2026-09-18"] == [90.0]  # 空 instId 不计

    def test_last_price_uses_bid_ask_mid(self, monkeypatch):
        from nanobot_quant import okx_options_data as ood
        monkeypatch.setattr(ood, "get_ticker_bid_ask",
                            lambda inst: {"bid": 0.20, "ask": 0.30})
        px = self._ds().get_last_price(oa.inst_to_asset("SOL-USD_UM-260918-94-P"))
        assert px == pytest.approx(0.25)

    def test_historical_prices_returns_mark_bars(self, monkeypatch):
        from nanobot_quant import okx_options_data as ood
        monkeypatch.setattr(ood, "fetch_lifecycle", lambda inst, bar="15m": {
            "rows": [{"ts": 1758000000000 + i * 60000, "mark_px": 0.2 + i * 0.01,
                      "ref_px": 100.0} for i in range(5)],
        })
        bars = self._ds().get_historical_prices(
            oa.inst_to_asset("SOL-USD_UM-260918-94-P"), length=3, timestep="minute")
        assert bars is not None
        df = bars.df
        assert len(df) == 3
        assert list(df["close"]) == pytest.approx([0.22, 0.23, 0.24])

    def test_empty_lifecycle_returns_none(self, monkeypatch):
        from nanobot_quant import okx_options_data as ood
        monkeypatch.setattr(ood, "fetch_lifecycle", lambda inst, bar="15m": {"rows": []})
        out = self._ds().get_historical_prices(
            oa.inst_to_asset("SOL-USD_UM-260918-94-P"), length=3, timestep="15min")
        assert out is None
