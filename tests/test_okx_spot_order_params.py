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


# ── 2. tdMode：现货一律 cash，撞错才自适应 ────────────────────────────

def test_default_td_mode_is_cash():
    """现货交易不需要保证金 ⇒ 默认现金模式（用户 2026-10-30 核实子账号模式）。

    Bot2–5 是「现货模式」子账号，传 cross 直接 54000；而 acctLv 是主账户级
    属性、对子账号不成立，所以不能靠它判断。
    """
    assert okx_spot.SPOT_TDMODE == "cash"


def test_td_mode_for_returns_cached_or_cash():
    okx_spot._TDMODE_CACHE.clear()
    assert okx_spot.td_mode_for({"api_key": "k1"}) == "cash"
    okx_spot._TDMODE_CACHE["k1"] = "cross"       # 撞错后记住的值
    assert okx_spot.td_mode_for({"api_key": "k1"}) == "cross"


def test_market_order_uses_cash_by_default(monkeypatch):
    import nanobot_quant.okx_sdk as sdk

    captured = {}
    monkeypatch.setattr(sdk, "trade_for",
                        lambda c: _FakeTrade(captured, _ok([{"sCode": "0", "ordId": "E1"}])))
    okx_spot._TDMODE_CACHE.clear()
    okx_spot.market_order({"api_key": "kc"}, inst_id="ETH-USDC", side="buy", sz=0.0017)
    assert captured["set_order"]["tdMode"] == "cash"


# ── 3. 计价币只认 WebUI 参数，不自动换对 ──────────────────────────────────

def _broker(quote, balances, px):
    from nanobot_quant.brokers.okx_cex_broker import OkxCexBroker

    b = OkxCexBroker.__new__(OkxCexBroker)
    b._quote = quote
    b._price_cache = {}
    b._price_of_inst = lambda inst: px          # type: ignore[method-assign]
    b.inst_id_of = lambda symbol: f"{symbol}-{quote}"   # type: ignore[method-assign]
    b.quote_of = lambda symbol: quote           # type: ignore[method-assign]
    b.balances = balances
    return b


def test_buy_pair_follows_configured_quote_only():
    """用户拍板（2026-10-30）：计价币是 WebUI 上**人**选的，系统不替用户猜。"""
    b = _broker("USDT", {"USDT": {"available": 0.0}, "USDC": {"available": 14.57}}, 96.78)
    inst, quote, px, cash = b._pair_for_buy("SOL", 0.041, b.balances)
    assert inst == "SOL-USDT" and quote == "USDT"   # 不因为 USDC 有钱就切过去
    assert cash == 0.0 and px == pytest.approx(96.78)


def test_buy_pair_uses_usdc_when_user_selected_usdc():
    b = _broker("USDC", {"USDT": {"available": 99.0}, "USDC": {"available": 14.57}}, 96.75)
    inst, quote, _px, cash = b._pair_for_buy("SOL", 0.041, b.balances)
    assert inst == "SOL-USDC" and quote == "USDC" and cash == pytest.approx(14.57)


def test_source_has_no_auto_pair_switching():
    """回归锁：不得再出现「按余额自动选对」的实现（用户明确否决）。"""
    import inspect

    from nanobot_quant.brokers.okx_cex_broker import OkxCexBroker

    src = inspect.getsource(OkxCexBroker._pair_for_buy)
    # 只看代码体（docstring 里会提到被否决的方案，不作为判据）
    parts = src.split('"""')
    body = parts[2] if len(parts) >= 3 else src
    assert "USDC" not in body and "insts" not in body


# ── 4. 54000 自动降级 cash 重试（acctLv 查不准时的兜底）─────────────────

def test_margin_not_supported_degrades_to_cash(monkeypatch):
    """acctLv 选了 cross 但账户实际不支保证金 → 54000 → 降级 cash 重试一次。

    54000 是明确拒单（订单未成交），重试不产生重复仓。
    """
    import nanobot_quant.okx_sdk as sdk
    from nanobot_quant.okx_sdk import OkxSdkError

    calls = []

    class _Trade:
        def set_order(self, **kw):
            calls.append(kw["tdMode"])
            if len(calls) == 1:
                return {"code": "1", "msg": "All operations failed",
                        "data": [{"sCode": "54000", "sMsg": "Margin trading is not supported."}]}
            return {"code": "0", "data": [{"sCode": "0", "ordId": "B2"}]}

    monkeypatch.setattr(sdk, "trade_for", lambda c: _Trade())
    monkeypatch.setattr(okx_spot, "td_mode_for", lambda creds: "cross")
    okx_spot._TDMODE_CACHE.clear()

    res = okx_spot.market_order({"api_key": "kb"}, inst_id="BTC-USDC", side="buy", sz=1e-05)
    assert calls == ["cross", "cash"]          # 先 cross，撞 54000 后降级
    assert res["ord_id"] == "B2"
    # 降级结果写回缓存，后续订单直接用 cash（不再撞一次 54000）
    assert okx_spot._TDMODE_CACHE["kb"] == "cash"


def test_other_rejections_are_not_retried(monkeypatch):
    """只有 54000 才降级重试，别的拒单必须原样抛（不能吞错误）。"""
    import pytest as _pytest

    import nanobot_quant.okx_sdk as sdk

    calls = []

    class _Trade:
        def set_order(self, **kw):
            calls.append(kw["tdMode"])
            return {"code": "1", "msg": "All operations failed",
                    "data": [{"sCode": "51000", "sMsg": "Parameter sz error"}]}

    monkeypatch.setattr(sdk, "trade_for", lambda c: _Trade())
    monkeypatch.setattr(okx_spot, "td_mode_for", lambda creds: "cash")
    with _pytest.raises(sdk.OkxSdkError):
        okx_spot.market_order({"api_key": "kx"}, inst_id="BTC-USDC", side="buy", sz=1e-05)
    assert calls == ["cash"]                   # 只发一次


def test_td_mode_error_switches_to_cross(monkeypatch):
    """跨币种保证金子账号（Bot1）不接受 cash → 51000 Parameter tdMode error
    → 自动切 cross 重试一次并记忆（期权线补买走的就是这条）。"""
    import nanobot_quant.okx_sdk as sdk

    calls = []

    class _Trade:
        def set_order(self, **kw):          # -USDC 等普通对
            return self._go(kw)

        def send_request(self, path, method, **kw):   # -USD（统一 USD 订单簿）
            return self._go(kw)

        def _go(self, kw):
            calls.append(kw["tdMode"])
            if len(calls) == 1:
                return {"code": "1", "msg": "All operations failed",
                        "data": [{"sCode": "51000", "sMsg": "Parameter tdMode error"}]}
            return {"code": "0", "data": [{"sCode": "0", "ordId": "C1"}]}

    monkeypatch.setattr(sdk, "trade_for", lambda c: _Trade())
    okx_spot._TDMODE_CACHE.clear()
    res = okx_spot.market_order({"api_key": "kbot1"}, inst_id="SOL-USD", side="buy", sz="0.1")
    assert calls == ["cash", "cross"]
    assert res["ord_id"] == "C1"
    assert okx_spot._TDMODE_CACHE["kbot1"] == "cross"


def test_51000_other_params_not_treated_as_td_mode(monkeypatch):
    """51000 是通用参数错误码，只有消息里带 tdMode 才切换（否则会掩盖 sz 等问题）。"""
    import pytest as _pytest

    import nanobot_quant.okx_sdk as sdk

    calls = []

    class _Trade:
        def set_order(self, **kw):
            calls.append(kw["tdMode"])
            return {"code": "1", "msg": "All operations failed",
                    "data": [{"sCode": "51000", "sMsg": "Parameter sz error"}]}

    monkeypatch.setattr(sdk, "trade_for", lambda c: _Trade())
    okx_spot._TDMODE_CACHE.clear()
    with _pytest.raises(sdk.OkxSdkError):
        okx_spot.market_order({"api_key": "ksz"}, inst_id="BTC-USDC", side="buy", sz=1e-05)
    assert calls == ["cash"]        # 不切换、只发一次


# ── 5. fixed_amount 是最小金额 ⇒ 按官方精度向上取整 ────────────────────

def test_plan_buy_sz_ceils_not_floors():
    """4U 是**最小下单金额**：按 lotSz 向上取整，不能 floor 掉一档。

    BNB 实测场景（px≈716、lot 0.001）：4/716 = 0.00558 →
    floor 给 0.005（3.58U，比设定的 4U 少 10%）；ceil 给 0.006（4.30U，
    超支 0.30U，小于一个步进的价值 0.72U）。
    """
    sz = okx_spot.plan_buy_sz(4 / 716.58, lot=0.001, min_sz=0.001)
    assert sz == pytest.approx(0.006)
    assert sz * 716.58 >= 4.0           # 金额不低于设定值


def test_plan_buy_sz_keeps_position_sellable():
    """买入到账要扣手续费，floor 后仍需 ≥ minSz，否则是卖不掉的死仓。"""
    # 只有 1 个步进、minSz 就是 1 步进：到账 0.999 步 → floor 0 步 → 必须补到 2 步
    sz = okx_spot.plan_buy_sz(0.001, lot=0.001, min_sz=0.001)
    assert sz == pytest.approx(0.002)
    assert okx_spot.floor_step(sz * (1 - okx_spot.SPOT_FEE_TAKER), 0.001) >= 0.001


def test_plan_buy_sz_fails_closed_when_fixed_amount_too_small():
    """补步进超过 3 步 ⇒ 该标的做不了，显式拒绝（不无限超支）。"""
    import pytest as _pytest

    from nanobot_quant.okx_sdk import OkxSdkError

    with _pytest.raises(OkxSdkError):
        okx_spot.plan_buy_sz(0.001, lot=0.001, min_sz=0.05)


def test_plan_buy_sz_no_lot_returns_target():
    """规格缺失时原样返回（由调用方 fail-closed）。"""
    assert okx_spot.plan_buy_sz(0.05, lot=0, min_sz=0) == pytest.approx(0.05)
