"""OKX 现货下单参数三处实盘故障（2026-10-30 首个真实 BUY 日暴露）。

三笔全部失败，各自一个根因：

| 标的 | 报错 | 根因 |
|---|---|---|
| BTC | `51000 Parameter sz error` | `str(4/77971.7)` = `5.13e-05` 科学计数法 |
| ETH | `54000 Margin trading is not supported.` | acctLv<3 的账号传了 `tdMode=cross` |
| SOL | `USDT 不足：需 3.9999，可用 0.0000`（现金 USDC=14.57） | 硬按配置下 `SOL-USDT`，钱在 USDC |
"""
from __future__ import annotations

import pytest

from nanobot_quant import okx_spot


class _FakeTrade:
    def __init__(self, captured, resp):
        self._c, self._r = captured, resp

    def set_order(self, **kw):
        self._c["set_order"] = kw
        return self._r


def _ok(rows):
    return {"code": "0", "data": rows}


# ── 1. sz 不能用科学计数法 ────────────────────────────────────────────────

def test_fmt_decimal_never_uses_scientific_notation():
    assert okx_spot.fmt_decimal(5.13e-05) == "0.0000513"
    assert "e" not in okx_spot.fmt_decimal(5.13e-05).lower()
    assert okx_spot.fmt_decimal(1e-08) == "0.00000001"
    assert okx_spot.fmt_decimal(0.00534530682) == "0.00534530682"
    assert okx_spot.fmt_decimal(0) == "0"
    # 4U / 77971.7（BTC 实测）：修复前 str() 给 "5.130066421534993e-05" → 51000
    btc_sz = okx_spot.fmt_decimal(4 / 77971.7)
    assert "e" not in btc_sz.lower()
    assert btc_sz.startswith("0.0000513")
    assert abs(float(btc_sz) - 4 / 77971.7) < 1e-12


def test_market_order_sends_fixed_point_sz(monkeypatch):
    import nanobot_quant.okx_sdk as sdk

    captured = {}
    monkeypatch.setattr(sdk, "trade_for",
                        lambda c: _FakeTrade(captured, _ok([{"sCode": "0", "ordId": "B1"}])))
    monkeypatch.setattr(okx_spot, "td_mode_for", lambda creds: "cash")
    okx_spot.market_order({"api_key": "k"}, inst_id="BTC-USDC", side="buy", sz=5.13e-05)
    assert captured["set_order"]["sz"] == "0.0000513"


# ── 2. tdMode 按账号 acctLv 自适应 ──────────────────────────────────────

@pytest.mark.parametrize("lv,expected", [("3", "cross"), ("4", "cross"),
                                        ("1", "cash"), ("2", "cash")])
def test_td_mode_follows_acct_level(monkeypatch, lv, expected):
    import nanobot_quant.okx_sdk as sdk

    class _Acc:
        def get_account_config(self):
            return {"code": "0", "data": [{"acctLv": lv}]}

    monkeypatch.setattr(sdk, "account_for", lambda c: _Acc())
    okx_spot._TDMODE_CACHE.clear()
    assert okx_spot.td_mode_for({"api_key": f"k{lv}"}) == expected


def test_td_mode_query_failure_keeps_legacy_cross(monkeypatch):
    import nanobot_quant.okx_sdk as sdk

    def _boom(_c):
        raise RuntimeError("network down")

    monkeypatch.setattr(sdk, "account_for", _boom)
    okx_spot._TDMODE_CACHE.clear()
    assert okx_spot.td_mode_for({"api_key": "kfail"}) == "cross"


def test_market_order_uses_adaptive_td_mode(monkeypatch):
    import nanobot_quant.okx_sdk as sdk

    captured = {}
    monkeypatch.setattr(sdk, "trade_for",
                        lambda c: _FakeTrade(captured, _ok([{"sCode": "0", "ordId": "E1"}])))
    monkeypatch.setattr(okx_spot, "td_mode_for", lambda creds: "cash")
    okx_spot.market_order({"api_key": "k"}, inst_id="ETH-USDC", side="buy", sz=0.0017)
    assert captured["set_order"]["tdMode"] == "cash"


# ── 3. BUY 按余额选对（USDT / USDC 双对）─────────────────────────────────

def _broker_with(balances, prices, pairs=None):
    from nanobot_quant.brokers.okx_cex_broker import OkxCexBroker

    b = OkxCexBroker.__new__(OkxCexBroker)
    b._quote = "USDT"
    b._price_cache = {}
    b._price_of_inst = lambda inst: prices.get(inst, 0.0)   # type: ignore[method-assign]
    b._base_ticker = lambda symbol: symbol.split("/")[0]     # type: ignore[method-assign]
    b._pairs = pairs or {"SOL": ["SOL-USDT", "SOL-USDC"]}
    import nanobot_quant.brokers.okx_cex_broker as m
    m.inst_id_for = lambda base, q, strict=False: (   # type: ignore[assignment]
        f"{base}-{q}" if f"{base}-{q}" in b._pairs.get(base, []) else "")
    b.balances = balances
    return b


def test_buy_picks_quote_with_balance():
    """钱在 USDC 时不能硬下 SOL-USDT（2026-10-30 SOL 三 slot 全失败）。"""
    b = _broker_with({"USDT": {"available": 0.0}, "USDC": {"available": 14.57}},
                     {"SOL-USDT": 96.78, "SOL-USDC": 96.75})
    inst, quote, px, cash = b._pair_for_buy("SOL", 0.041, b.balances)
    assert inst == "SOL-USDC" and quote == "USDC" and cash == pytest.approx(14.57)
    assert px == pytest.approx(96.75)


def test_buy_prefers_configured_pair_when_both_funded():
    b = _broker_with({"USDT": {"available": 50.0}, "USDC": {"available": 50.0}},
                     {"SOL-USDT": 96.78, "SOL-USDC": 96.75})
    inst, quote, _px, _cash = b._pair_for_buy("SOL", 0.041, b.balances)
    assert inst == "SOL-USDT"


def test_buy_returns_richest_pair_when_underfunded():
    """两边都不够时给现金最多的那个，让报错信息指向真正有钱的币。"""
    b = _broker_with({"USDT": {"available": 0.0}, "USDC": {"available": 1.0}},
                     {"SOL-USDT": 96.78, "SOL-USDC": 96.75})
    inst, quote, _px, cash = b._pair_for_buy("SOL", 1.0, b.balances)
    assert inst == "SOL-USDC" and cash == pytest.approx(1.0)
