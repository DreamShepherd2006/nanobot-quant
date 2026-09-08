"""okx_options_trade 单元测试（mock okx_sdk 层，不触网）。

批次 C：卖 put 执行层 — 预览/下单参数/台账/两步确认/平仓/补买。
mock 对象替代 okx_sdk.public/market/trade_for/account_for 与凭证存储，
验证逻辑与参数构造；HTTP 与 SDK 请求不发出。
"""

import time
from datetime import datetime, timezone

import pytest

from nanobot_quant import okx_options_trade as ot
from nanobot_quant.okx_options_trade import (
    suggest_close_px,
    suggest_sell_px,
)
from nanobot_quant.okx_sdk import OkxSdkError


# ── fixtures ──────────────────────────────────────────────

class _FakeInst:
    """mock public().get_instruments(instType=OPTION, instId=...)"""

    def get_instruments(self, instType=None, instId=None, **kw):
        parts = instId.split("-")
        stk, typ = parts[-2], parts[-1]
        return {"code": "0", "data": [{
            "instId": instId, "instFamily": "-".join(parts[:2]),
            "optType": typ, "stk": stk, "expTime": "1777900000000",
            "ctVal": "1", "ctMult": "0.01", "uly": "BTC-USD", "state": "live",
        }]}


class _FakeMarket:
    def get_ticker(self, instId=None, **kw):
        return {"code": "0", "data": [{
            "instId": instId, "bidPx": "100.0", "askPx": "110.0", "last": "105.0",
        }]}

    def get_books(self, instId=None, sz=None, **kw):
        # 无盘口（空档）——preview 的 simulate_fill 走 None 容错路径
        return {"code": "0", "data": [{"bids": [], "asks": [], "ts": "0"}]}


class _FakeTrade:
    def __init__(self):
        self.calls = []
        self.last_px = None
        # ord_id → state；get_order 缺省 "filled"（兼容既有 settle 测试）
        self.order_states = {}
        self.cancel_calls = []

    def set_order(self, **params):
        self.calls.append(params)
        self.last_px = params.get("px", params.get("sz", "1"))
        return {"code": "0", "data": [{"ordId": f"ord-{len(self.calls)}", "clOrdId": ""}]}

    def send_request(self, path, method, **params):
        # python-okx 未封装参数的透传通道（如现货 USD 对下单 tradeQuoteCcy）
        self.calls.append({"path": path, "method": method, **params})
        self.last_px = params.get("px", params.get("sz", "1"))
        return {"code": "0", "data": [{"ordId": f"ord-{len(self.calls)}", "clOrdId": ""}]}

    def get_order(self, instId=None, ordId=None, **kw):
        # 缺省 filled（既有 settle 测试依赖），撤单测试经 order_states 设 live/canceled
        state = self.order_states.get(ordId, "filled")
        return {"code": "0", "data": [{"instId": instId, "ordId": ordId,
                                       "state": state, "avgPx": str(self.last_px),
                                       "accFillSz": "1", "fee": "-0.02"}]}

    def set_cancel_order(self, instId=None, ordId=None, **kw):
        self.cancel_calls.append({"instId": instId, "ordId": ordId})
        self.order_states[ordId] = "canceled"
        return {"code": "0", "data": [{"sCode": "0", "sMsg": "", "ordId": ordId}]}

    def get_orders_pending(self, instType="", instFamily="", **kw):
        rows = []
        for ord_id, st in self.order_states.items():
            if st in ("live", "partially_filled"):
                rows.append({"instId": f"{instFamily}-260905-100-P", "ordId": ord_id,
                             "px": "0.15", "sz": "1", "side": "buy",
                             "ordType": "limit", "tdMode": "isolated",
                             "cTime": "1757000000000"})
        return {"code": "0", "data": rows}


class _FakeAccount:
    def __init__(self):
        self.margin = 0.0          # 该仓当前保证金（get_positions 返回，0 → imr fallback）
        self.margin_calls = []     # set_margin_balance 调用记录
        self.margin_fail = None    # 非空时 set_margin_balance 抛 RuntimeError
        self.bills = []            # 预置账单行（OKX 字段：instId/type/subType/px/pnl/ts）
        self.bills_fail = None     # 非空时 get_bills 抛 RuntimeError

    def get_bills(self, instType=None, type=None, begin=None, end=None,
                  limit=None, **kw):
        if self.bills_fail:
            raise RuntimeError(self.bills_fail)
        rows = []
        for b in self.bills:
            if type and b.get("type") != type:
                continue
            if instType and b.get("instType", "OPTION") != instType:
                continue
            ts = int(b.get("ts") or 0)
            if begin and ts < int(begin):
                continue
            if end and ts > int(end):
                continue
            rows.append(b)
        return {"code": "0", "data": rows}

    def get_positions(self, instType=None, instId=None, **kw):
        if instId:
            if self.margin > 0:
                return {"code": "0", "data": [{"instId": instId,
                        "mgnMode": "isolated", "margin": str(self.margin)}]}
            return {"code": "0", "data": [{"instId": instId,
                    "mgnMode": "isolated", "margin": "", "imr": "0.05"}]}
        return {"code": "0", "data": []}

    def set_margin_balance(self, instId=None, posSide=None, type=None,
                           amt=None, **kw):
        if self.margin_fail:
            raise RuntimeError(self.margin_fail)
        self.margin_calls.append({"instId": instId, "posSide": posSide,
                                  "type": type, "amt": amt})
        self.margin += float(amt)
        return {"code": "0", "data": []}

    def get_balance(self, ccy=""):
        return {"code": "0", "data": [{"totalEq": "67.44", "details": [
            {"ccy": "XCRCL", "cashBal": "0.66", "availBal": "0.66",
             "frozenBal": "0", "eq": "0.6603", "eqUsd": "67.44", "uTime": "0"}]}]}

    def get_config(self):
        return {"code": "0", "data": [{"uid": "881574754615066858", "acctLv": "3",
                                       "posMode": "net_mode", "opAuth": "1",
                                       "settleCcy": "USDC", "perm": "read_only,trade"}]}


@pytest.fixture(autouse=True)
def _mock_sdk(monkeypatch, tmp_path):
    fake_trade = _FakeTrade()
    fake_account = _FakeAccount()
    monkeypatch.setattr(ot.okx_sdk, "public", lambda: _FakeInst())
    monkeypatch.setattr(ot.okx_sdk, "market", lambda: _FakeMarket())
    monkeypatch.setattr(ot.okx_sdk, "trade_for", lambda creds: fake_trade)
    monkeypatch.setattr(ot.okx_sdk, "account_for", lambda creds: fake_account)
    monkeypatch.setattr(ot, "ledger_path", lambda: tmp_path / "ledger.json")
    monkeypatch.setattr(ot, "params_path", lambda: tmp_path / "okx_options_params.json")
    fake_trade.account = fake_account
    return fake_trade


@pytest.fixture
def _patch_entry(monkeypatch):
    monkeypatch.setattr(ot, "_entry_account", lambda account: {
        "creds": {"api_key": "k", "secret_key": "s", "passphrase": "p"},
        "label": account or "bot1", "name": "DreamShepherdbot1",
        "uid": account or "881574754615066858"})


# ── preview ───────────────────────────────────────────────

def test_preview_open_put_limit():
    p = ot.preview_open_put("BTC-USD_UM-260904-80000-P", 1, "limit", px=110.0)
    assert p["ok"] is True
    assert p["opt_type"] == "P"
    assert p["strike"] == 80000
    assert p["lot"] == pytest.approx(0.01)          # ctVal 1 × ctMult 0.01
    assert p["est_premium_usd"] == pytest.approx(110.0 * 0.01)   # px × lot
    assert p["collateral_est_usd"] == pytest.approx(80000 * 0.01)  # strike × lot
    assert p["td_mode"] == "isolated"
    assert p["ref"]["ask"] == 110.0


def test_preview_open_put_rejects_market():
    # OKX 期权无纯市价单（实测 50016 instId and ordType don't match）
    with pytest.raises(OkxSdkError, match="市价"):
        ot.preview_open_put("BTC-USD_UM-260904-80000-P", 2, "market")


def test_preview_rejects_call_and_bad_type():
    with pytest.raises(OkxSdkError):
        ot.preview_open_put("BTC-USD_UM-260904-80000-C", 1, "limit", px=10)  # 非 P
    with pytest.raises(OkxSdkError):
        ot.preview_open_put("BTC-USD_UM-260904-80000-P", 1, "weird", px=10)  # 非法类型
    with pytest.raises(OkxSdkError):
        ot.preview_open_put("BTC-USD_UM-260904-80000-P", 1, "limit", px=None)  # 缺 px


# ── 下单参数构造 ─────────────────────────────────────────

def test_place_sell_put_params(_mock_sdk, _patch_entry):
    entry = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                        ord_type="limit", px=110.0)
    call = _mock_sdk.calls[-1]
    assert call["instId"] == "BTC-USD_UM-260904-80000-P"
    assert call["side"] == "sell"
    assert call["tdMode"] == "isolated"
    assert call["ordType"] == "limit"
    assert call["sz"] == "1"
    assert call["px"] == "110.0"
    assert call["tag"] == ot.TAG_OPEN
    # 轮询成交 → open，回填（avg = 限价 px）
    assert entry["status"] == "open"
    assert entry["filled_px"] == 110.0
    assert entry["premium_usd"] == pytest.approx(110.0 * 0.01)


def test_place_close_put_buy(_mock_sdk, _patch_entry):
    ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1, ord_type="limit", px=110.0)
    ot.open_put("bot1", inst_id="BTC-USD_UM-260904-82000-P", sz=2, ord_type="limit", px=50.0)
    e = ot.close_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                     ord_type="limit", px=108.0)
    call = _mock_sdk.calls[-1]
    assert call["side"] == "buy"
    assert call["instId"] == "BTC-USD_UM-260904-80000-P"
    assert call["tdMode"] == "isolated"  # 平仓须匹配持仓 mgnMode（cash → 51000）
    assert call["tag"] == ot.TAG_CLOSE
    assert e["status"] == "closed"
    # 盈亏 = (开 110 − 平 108) × 0.01 × 1 = 0.02
    assert e["pnl_usd"] == pytest.approx(0.02)
    # open 行也被标 closed
    entries = ot.load_ledger()
    open_rows = [x for x in entries if x["kind"] == "open_put"
                 and x["inst_id"] == "BTC-USD_UM-260904-80000-P"]
    assert open_rows[0]["status"] == "closed"


def test_close_put_without_open_rejected(_patch_entry):
    with pytest.raises(OkxSdkError):
        ot.close_put("bot1", inst_id="BTC-USD_UM-260904-90000-P", sz=1,
                     ord_type="limit", px=5)


def test_spot_cover_quote_amt(_mock_sdk, _patch_entry):
    e = ot.spot_cover("bot1", spot_inst="BTC-USD", quote_amt=50.0)
    call = _mock_sdk.calls[-1]
    assert call["instId"] == "BTC-USD"
    assert call["side"] == "buy"
    assert call["ordType"] == "market"
    assert call["sz"] == "50.00"
    assert call["tgtCcy"] == "quote_ccy"
    assert call["tag"] == ot.TAG_COVER
    # Crypto-USD（统一 USD 订单簿）：tdMode=cross（acctLv=3 子账号）+ tradeQuoteCcy=USDC
    # 经 send_request 透传（set_order 未封装 tradeQuoteCcy）
    assert call["path"] == "/api/v5/trade/order"
    assert call["method"] == "POST"
    assert call["tradeQuoteCcy"] == "USDC"
    assert call["tdMode"] == "cross"
    assert e["status"] == "filled"


def test_spot_cover_base_qty(_mock_sdk, _patch_entry):
    ot.spot_cover("bot1", spot_inst="SOL-USD", base_qty=0.01)
    call = _mock_sdk.calls[-1]
    assert call["tgtCcy"] == "base_ccy"
    assert call["tradeQuoteCcy"] == "USDC"
    assert call["tdMode"] == "cross"


def test_spot_cover_usdt_pair_uses_set_order_with_td_mode(_mock_sdk, _patch_entry):
    # 普通现货对（USDT 等）走 set_order；acctLv=3 子账号同样 tdMode=cross
    ot.spot_cover("bot1", spot_inst="SOL-USDT", quote_amt=10.0)
    call = _mock_sdk.calls[-1]
    assert call["instId"] == "SOL-USDT"
    assert call["tdMode"] == "cross"
    assert "tradeQuoteCcy" not in call
    assert "path" not in call
    assert call["sz"] == "10.00"


def test_spot_cover_requires_amount(_patch_entry):
    with pytest.raises(OkxSdkError):
        ot.spot_cover("bot1", spot_inst="BTC-USDC")
    with pytest.raises(OkxSdkError):
        ot.spot_cover("bot1", spot_inst="BTC-USDC", quote_amt=0)


# ── 两步确认 ─────────────────────────────────────────────

def test_stage_take_action_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "_TTL", 30)
    tx, info = ot.stage_action("sell", {"inst_id": "X", "sz": 1})
    assert tx and info["expires_in"] == 30
    act = ot.take_action(tx)
    assert act["action"] == "sell"
    # 二次消费失败（一次性）
    with pytest.raises(OkxSdkError):
        ot.take_action(tx)


def test_take_action_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "_TTL", 0.01)
    tx, _ = ot.stage_action("sell", {})
    time.sleep(0.03)
    with pytest.raises(OkxSdkError):
        ot.take_action(tx)


# ── ledger 持久化 ────────────────────────────────────────

def test_ledger_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "ledger_path", lambda: tmp_path / "ledger.json")
    e = ot.add_ledger(kind="open_put", inst_id="BTC-USD_UM-260904-80000-P", sz=1)
    rows = ot.load_ledger()
    assert len(rows) == 1 and rows[0]["id"] == e["id"] and rows[0]["status"] == "pending"
    ot.update_ledger(lambda x: x["id"] == e["id"], status="open", filled_px=108.0)
    assert ot.load_ledger()[0]["status"] == "open"
    assert ot.find_entry(lambda x: x["id"] == e["id"])["filled_px"] == 108.0


# ── expiry reminder ──────────────────────────────────────

def test_expiry_reminder(tmp_path, monkeypatch):
    monkeypatch.setattr(ot, "ledger_path", lambda: tmp_path / "ledger.json")
    soon = int(time.time() * 1000) + 3600_000        # 1h 后
    far = int(time.time() * 1000) + 30 * 86400_000    # 30 天后
    ot.add_ledger(kind="open_put", inst_id="A", status="open", exp_ms=soon, sz=1)
    ot.add_ledger(kind="open_put", inst_id="B", status="open", exp_ms=far, sz=1)
    ot.add_ledger(kind="open_put", inst_id="C", status="closed", exp_ms=soon, sz=1)
    r = ot.expiry_reminder()
    assert len(r) == 1 and r[0]["inst_id"] == "A"


# ── balance / config（资产卡 + 账户配置条）──────────────

def test_account_balance_normalized(_patch_entry):
    b = ot.account_balance("881574754615066858")
    assert b["total_eq_usd"] == pytest.approx(67.44)
    assert b["details"][0]["ccy"] == "XCRCL"
    assert b["details"][0]["avail_bal"] == pytest.approx(0.66)
    assert b["account"] == "DreamShepherdbot1"
    assert b["account_uid"] == "881574754615066858"


def test_account_config_option_authorized(_patch_entry):
    c = ot.account_config("881574754615066858")
    assert c["op_auth"] == 1
    assert c["acct_lv"] == "3"
    assert c["settle_ccy"] == "USDC"


# ── 持仓方向归一化（平仓按钮依赖 side）──────────────

def test_normalize_position_net_mode_sign():
    # net_mode（跨币种保证金）：posSide=net，空头由 pos 负号表达 → side=short（前端显示平仓按钮）
    p = ot._normalize_position({"instId": "SOL-USD_UM-260905-99-P", "posSide": "net",
                                "pos": "-1", "avgPx": "0.11", "markPx": "0.18"})
    assert p["side"] == "short" and p["pos"] == 1.0
    p2 = ot._normalize_position({"instId": "SOL-USD_UM-260905-99-C", "posSide": "net",
                                 "pos": "1", "avgPx": "0.2"})
    assert p2["side"] == "long"
    # 明确 long/short posSide 直通（简单/单币种模式）
    p3 = ot._normalize_position({"instId": "X", "posSide": "short", "pos": "-2"})
    assert p3["side"] == "short" and p3["pos"] == 2.0
    # 保证金：margin 0/空回退 imr；强平价空/-- → None（足额担保时 OKX 无强平价）
    p4 = ot._normalize_position({"instId": "X", "posSide": "short", "pos": "-2",
                                 "margin": "", "imr": "1.5", "liqPx": "--"})
    assert p4["margin_usd"] == pytest.approx(1.5) and p4["liq_px"] is None
    p5 = ot._normalize_position({"instId": "X", "posSide": "long", "pos": "1",
                                 "margin": "3.2", "liqPx": "88.5"})
    assert p5["margin_usd"] == pytest.approx(3.2) and p5["liq_px"] == pytest.approx(88.5)
    p6 = ot._normalize_position({"instId": "X", "posSide": "long", "pos": "1",
                                 "margin": "0", "imr": "0", "liqPx": "abc"})
    assert p6["margin_usd"] == 0.0 and p6["liq_px"] is None


# ── instId 到期/行权解析（持仓卡片展示）────────────

def test_parse_exp_um_suffix():
    # U 本位 _UM 后缀：SOL-USD_UM-260905-99-P → 2026-09-05（曾取 parts[1]=USD_UM 失败 → 1970-01-01）
    e = ot._parse_exp("SOL-USD_UM-260905-99-P")
    assert e is not None and e == int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp() * 1000)
    # 无 _UM 后缀（币本位参考格式）同样按倒数第 3 段
    e2 = ot._parse_exp("BTC-USD-260904-80000-C")
    assert e2 == int(datetime(2026, 9, 4, tzinfo=timezone.utc).timestamp() * 1000)
    # 畸形段 → None（不落 1970）
    assert ot._parse_exp("BAD") is None
    assert ot._parse_exp("SOL-USD_UM-ABCDE-99-P") is None
    assert ot._parse_strike("SOL-USD_UM-260905-99-P") == 99.0


# ── 逐仓现金担保（走 A：成交后自动追加保证金至全损上限）────────

def _open_put(_mock_sdk, inst_id="SOL-USD_UM-260905-99-P", px=0.11):
    # _FakeInst 里 ctVal=1 ctMult=0.01 → lot=0.01；用 BTC-USD_UM 系列保持与既有测试一致
    inst = "BTC-USD_UM-260904-80000-P" if inst_id.startswith("BTC") else inst_id
    return ot.open_put("bot1", inst_id=inst, sz=1, ord_type="limit", px=px)


def test_open_put_auto_adds_collateral(_mock_sdk, _patch_entry):
    # 成交后自动把保证金追加至全损上限 strike×lot×sz（0 保证金时按 imr fallback）
    e = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                    ord_type="limit", px=110.0)
    acc = _mock_sdk.account
    assert e["status"] == "open"
    assert e["collateral_usd"] == pytest.approx(80000.0 * 0.01 * 1)
    # 追加调用：type=add、posSide=net（net_mode 逐仓）、amt = target − imr(0.05)
    call = acc.margin_calls[-1]
    assert call["type"] == "add"
    assert call["posSide"] == ot.POS_SIDE
    assert call["amt"] == pytest.approx(str(80000.0 * 0.01 - 0.05), abs=0.011)
    assert acc.margin_calls[0]["instId"] == "BTC-USD_UM-260904-80000-P"
    assert e["margin_added"] == pytest.approx(800.0 - 0.05, abs=0.011)
    assert "追加" in e["margin_note"] or e["margin_note"] == "ok（现金担保已追加）"


def test_collateral_skip_when_sufficient(_mock_sdk, _patch_entry):
    # 仓位已有保证金 ≥ 全损上限 → 不追加
    _mock_sdk.account.margin = 900.0  # > 800 目标
    e = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                    ord_type="limit", px=110.0)
    assert _mock_sdk.account.margin_calls == []
    assert e["margin_added"] == 0.0
    assert e["margin_note"] == "ok（已达标）"


def test_collateral_fail_keeps_open(_mock_sdk, _patch_entry):
    # 追加失败不阻断——仓位已成交保持 open，note 记录提醒
    _mock_sdk.account.margin_fail = "资金不足"
    e = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                    ord_type="limit", px=110.0)
    assert e["status"] == "open"
    assert e["margin_added"] is None
    assert "追加失败" in e["margin_note"]


def test_position_margin_imr_fallback(_mock_sdk, _patch_entry):
    # positions.margin 为空 → 回退 imr；有 margin 直接用
    mgn, mode = ot._position_margin(
        {"api_key": "k"}, "BTC-USD_UM-260904-80000-P")
    assert mgn == pytest.approx(0.05)  # imr fallback
    assert mode == "isolated"
    _mock_sdk.account.margin = 1.26
    mgn2, _ = ot._position_margin(
        {"api_key": "k"}, "BTC-USD_UM-260904-80000-P")
    assert mgn2 == pytest.approx(1.26)


# ── 担保比例（WebUI 参数：全损×ratio%，0=关闭）──────────────

def test_collateral_ratio_applied(_mock_sdk, _patch_entry):
    # ratio=50 → 自动追加目标 = 全损 × 0.5（800×0.5 − imr 0.05）
    ot.save_option_params(collateral_ratio_pct=50)
    e = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                    ord_type="limit", px=110.0)
    call = _mock_sdk.account.margin_calls[-1]
    assert e["collateral_usd"] == pytest.approx(400.0)
    assert call["amt"] == pytest.approx(str(400.0 - 0.05), abs=0.011)


def test_collateral_ratio_zero_disabled(_mock_sdk, _patch_entry):
    # ratio=0 → 关闭自动追加：不调 margin-balance，note 提示
    ot.save_option_params(collateral_ratio_pct=0)
    e = ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                    ord_type="limit", px=110.0)
    assert _mock_sdk.account.margin_calls == []
    assert e["status"] == "open"
    assert e["margin_added"] is None
    assert "关闭" in e["margin_note"]


def test_preview_reports_ratio_target(_mock_sdk, _patch_entry):
    ot.save_option_params(collateral_ratio_pct=120)
    p = ot.preview_open_put("BTC-USD_UM-260904-80000-P", 1, "limit", 110.0)
    assert p["collateral_ratio_pct"] == 120
    assert p["collateral_est_usd"] == pytest.approx(800.0)
    assert p["collateral_target_usd"] == pytest.approx(960.0)


def test_save_params_clamp_and_default(_mock_sdk, _patch_entry):
    d = ot.save_option_params(collateral_ratio_pct=999)
    assert d["collateral_ratio_pct"] == 200
    d2 = ot.save_option_params(collateral_ratio_pct=-3)
    assert d2["collateral_ratio_pct"] == 0
    # 损坏文件 → 默认 100
    ot.params_path().write_text("{bad json", "utf-8")
    assert ot.collateral_ratio_pct() == ot.DEFAULT_COLLATERAL_RATIO_PCT
    # 默认（无文件）
    ot.params_path().unlink(missing_ok=True)
    assert ot.collateral_ratio_pct() == ot.DEFAULT_COLLATERAL_RATIO_PCT


# ── 撤单 / 当前委托（批次：feat/options-cancel）────────────────

def test_cancel_live_order_updates_ledger(_mock_sdk, _patch_entry):
    """live 挂单撤销成功 → 台账对应条目置 cancelled + cancel_ts。"""
    fake = _mock_sdk
    fake.order_states["ord-100"] = "live"
    ot.add_ledger(kind="close_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-100", status="pending", side="buy", ord_type="limit",
                  px=0.3, sz=1)
    res = ot.cancel_order("bot1", inst_id="SOL-USD_UM-260905-100-P", ord_id="ord-100")
    assert res["status"] == "cancelled"
    assert fake.cancel_calls and fake.cancel_calls[0]["ordId"] == "ord-100"
    ent = ot.find_entry(lambda x: x.get("ord_id") == "ord-100")
    assert ent["status"] == "cancelled" and ent.get("cancel_ts")


def test_cancel_already_filled_leaves_ledger(_mock_sdk, _patch_entry):
    """订单已成交时拒绝撤销（提示刷新），台账 pending 条目不动。"""
    fake = _mock_sdk
    fake.order_states["ord-101"] = "filled"
    ot.add_ledger(kind="close_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-101", status="pending", side="buy", ord_type="limit",
                  px=0.3, sz=1)
    res = ot.cancel_order("bot1", inst_id="SOL-USD_UM-260905-100-P", ord_id="ord-101")
    assert res["status"] == "filled"
    assert not fake.cancel_calls
    assert ot.find_entry(lambda x: x.get("ord_id") == "ord-101")["status"] == "pending"


def test_cancel_idempotent_when_already_cancelled(_mock_sdk, _patch_entry):
    """OKX 侧已 cancelled → 幂等返回 cancelled 并对齐台账。"""
    fake = _mock_sdk
    fake.order_states["ord-102"] = "canceled"
    ot.add_ledger(kind="open_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-102", status="pending", side="sell", ord_type="limit",
                  px=0.15, sz=1)
    res = ot.cancel_order("bot1", inst_id="SOL-USD_UM-260905-100-P", ord_id="ord-102")
    assert res["status"] == "cancelled"
    assert ot.find_entry(lambda x: x.get("ord_id") == "ord-102")["status"] == "cancelled"


def test_cancel_close_put_keeps_open_row(_mock_sdk, _patch_entry):
    """撤销平仓挂单 → 平仓条目 cancelled，但 open_put 持仓行保持 open。"""
    fake = _mock_sdk
    fake.order_states["ord-103"] = "live"
    ot.add_ledger(kind="open_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-200", status="open", side="sell", ord_type="limit",
                  px=0.15, sz=1, strike=100, exp_ms=1760000000000, premium_usd=0.015)
    ot.add_ledger(kind="close_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-103", status="pending", side="buy", ord_type="limit",
                  px=0.3, sz=1)
    ot.cancel_order("bot1", inst_id="SOL-USD_UM-260905-100-P", ord_id="ord-103")
    assert ot.find_entry(lambda x: x.get("ord_id") == "ord-103")["status"] == "cancelled"
    assert ot.find_entry(lambda x: x.get("ord_id") == "ord-200")["status"] == "open"


def test_cancel_okx_error_keeps_ledger(_mock_sdk, _patch_entry, monkeypatch):
    """OKX 撤单返回业务错误（如 51400）→ 抛 OkxSdkError、台账不动。"""
    fake = _mock_sdk
    fake.order_states["ord-104"] = "live"

    def _boom(instId=None, ordId=None, **kw):
        return {"code": "0", "data": [{"sCode": "51400", "sMsg": "Order does not exist"}]}
    monkeypatch.setattr(fake, "set_cancel_order", _boom)
    ot.add_ledger(kind="open_put", account="bot1", inst_id="SOL-USD_UM-260905-100-P",
                  ord_id="ord-104", status="pending", side="sell", ord_type="limit",
                  px=0.15, sz=1)
    try:
        ot.cancel_order("bot1", inst_id="SOL-USD_UM-260905-100-P", ord_id="ord-104")
        assert False, "应抛 OkxSdkError"
    except OkxSdkError as e:
        assert "51400" in str(e)
    assert ot.find_entry(lambda x: x.get("ord_id") == "ord-104")["status"] == "pending"


def test_pending_orders_normalized_sorted(_mock_sdk, _patch_entry):
    """pending_orders 归一化并按时间排序（含官方手动挂单）。"""
    fake = _mock_sdk
    fake.order_states["ord-105"] = "live"
    fake.order_states["ord-106"] = "live"
    rows = ot.pending_orders("bot1", inst_family="SOL-USD_UM")
    assert len(rows) == 2
    r = rows[0]
    assert r["inst_id"].endswith("-100-P") and r["ord_id"] in ("ord-105", "ord-106")
    assert r["px"] == 0.15 and r["sz"] == 1 and r["side"] == "buy"
    assert r["ts_ms"] == 1757000000000


def test_pending_orders_requires_family(_mock_sdk, _patch_entry):
    try:
        ot.pending_orders("bot1", inst_family="")
        assert False, "应抛 OkxSdkError"
    except OkxSdkError as e:
        assert "inst_family" in str(e)


def test_suggest_sell_px_bid_half_protection():
    # 卖 put IOC 保底 = bid×0.5（保护线：正常盘口吃 bid 成交、闪崩半价下拒单）
    assert suggest_sell_px(0.30) == 0.15
    assert suggest_sell_px(0.02) == 0.01
    assert suggest_sell_px(1.04) == 0.52
    assert suggest_sell_px(None) is None
    assert suggest_sell_px(0) is None
    assert suggest_sell_px(-0.1) is None


def test_suggest_close_px_ask_no_buffer():
    # 买回 px = ask（吃买一不留缓冲；ask 升高自动撤不追价）
    assert suggest_close_px(0.33) == 0.33
    assert suggest_close_px(0.41) == 0.41
    assert suggest_close_px(None) is None
    assert suggest_close_px(0) is None
    assert suggest_close_px(-0.1) is None


# ── 到期结算判定（C23）──────────────────────────────────

def _seed_expired_put(inst="SOL-USD_UM-260906-101-P", strike=101.0,
                      exp_ms=1757145600000, account="bot1", sz=1, lot=0.1):
    return ot.add_ledger(kind="open_put", status="open", inst_id=inst,
                         account=account, strike=strike, exp_ms=exp_ms,
                         sz=sz, lot=lot, px=0.11, premium_usd=0.011,
                         family="SOL-USD_UM", collateral_usd=10.0977)


def _settle_bill(inst, sub, px, pnl="0.0", ts=1757145601000):
    return {"instId": inst, "type": "3", "subType": sub,
            "px": px, "pnl": pnl, "ts": ts}


def test_settle_otm_worthless_autoclose(_mock_sdk, _patch_entry):
    # 到期作废（172）且结算价 ≥ strike → settled_otm 自动关账（101-P 场景）
    _mock_sdk.account.bills = [_settle_bill("SOL-USD_UM-260906-101-P", "172",
                                            "105.0", "0.0070")]
    e = _seed_expired_put()
    out = ot.settle_expired_puts(now_ms=1757145700000)
    assert len(out) == 1 and out[0]["status"] == ot.STATUS_SETTLED_OTM
    row = next(x for x in ot.load_ledger() if x["id"] == e["id"])
    assert row["status"] == ot.STATUS_SETTLED_OTM
    assert row["settle_px"] == pytest.approx(105.0)
    assert row["settle_subtype"] == "172"
    assert row["settle_pnl"] == pytest.approx(0.007, abs=1e-6)


def test_settle_itm_exercised(_mock_sdk, _patch_entry):
    # 到期被行权（171）且结算价 < strike → settled_itm（后续回补流程）
    _mock_sdk.account.bills = [_settle_bill("SOL-USD_UM-260906-101-P", "171",
                                            "99.0", "-0.20")]
    e = _seed_expired_put()
    out = ot.settle_expired_puts(now_ms=1757145700000)
    assert len(out) == 1 and out[0]["status"] == ot.STATUS_SETTLED_ITM
    row = next(x for x in ot.load_ledger() if x["id"] == e["id"])
    assert row["status"] == ot.STATUS_SETTLED_ITM
    assert row["settle_px"] == pytest.approx(99.0)
    # ITM 毛赔付 = (行权价−结算价)×面值×张数（面值走家族常量；101-P → SOL 0.1）
    # 净盈亏 settle_pnl 已含权利金收入，与毛赔付分开存
    assert row["settle_payout"] == pytest.approx((101 - 99.0) * 0.1 * 1, abs=1e-6)


def test_settle_keeps_open_when_bill_missing(_mock_sdk, _patch_entry):
    # 账单未出（OKX 结算后 ~27s 才出现）→ 保持 open，下轮重试
    _mock_sdk.account.bills = []
    e = _seed_expired_put()
    assert ot.settle_expired_puts(now_ms=1757145700000) == []
    row = next(x for x in ot.load_ledger() if x["id"] == e["id"])
    assert row["status"] == "open"


def test_settle_review_on_subtype_px_contradiction(_mock_sdk, _patch_entry):
    # 作废行（172）但结算价 < strike → 矛盾 → settled_review（fail-closed 不猜）
    _mock_sdk.account.bills = [_settle_bill("SOL-USD_UM-260906-101-P", "172",
                                            "99.0", "0.0")]
    e = _seed_expired_put()
    out = ot.settle_expired_puts(now_ms=1757145700000)
    assert len(out) == 1 and out[0]["status"] == ot.STATUS_SETTLED_REVIEW
    assert "存疑" in out[0]["note"]
    row = next(x for x in ot.load_ledger() if x["id"] == e["id"])
    assert row["status"] == ot.STATUS_SETTLED_REVIEW


def test_settle_review_on_unknown_subtype(_mock_sdk, _patch_entry):
    # 交割行出现未知 subType（如 170 买方行权）→ review 不猜
    _mock_sdk.account.bills = [_settle_bill("SOL-USD_UM-260906-101-P", "170",
                                            "99.0", "0.0")]
    e = _seed_expired_put()
    out = ot.settle_expired_puts(now_ms=1757145700000)
    assert out[0]["status"] == ot.STATUS_SETTLED_REVIEW
    assert "170" in out[0]["note"]


def test_settle_skips_unexpired_and_non_open(_mock_sdk, _patch_entry):
    # 未到期 / 非 open 行不查询不处理
    _mock_sdk.account.bills = []
    ot.add_ledger(kind="open_put", status="open", inst_id="X-260999-101-P",
                  account="bot1", strike=101, exp_ms=1757145600000 + 999_000_000_000,
                  sz=1, lot=0.1, px=0.11)   # 未来到期
    ot.add_ledger(kind="open_put", status="closed", inst_id="Y-260906-101-P",
                  account="bot1", strike=101, exp_ms=1757145600000,
                  sz=1, lot=0.1, px=0.11)   # 已平仓
    assert ot.settle_expired_puts(now_ms=1757145700000) == []


def test_settle_uses_row_account_creds(_mock_sdk, _patch_entry):
    # 每行按自身 account 查账单（跨多子账号正确），_entry_account 收到 account 名
    seen = {}
    def spy(account):
        seen["acct"] = account
        return {"creds": {"api_key": "k", "secret_key": "s", "passphrase": "p"},
                "label": account, "name": account, "uid": account}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ot, "_entry_account", spy)
    try:
        _mock_sdk.account.bills = [_settle_bill("SOL-USD_UM-260906-101-P",
                                                "172", "105.0", "0.0")]
        _seed_expired_put(account="DreamShepherdbot1")
        ot.settle_expired_puts(now_ms=1757145700000)
        assert seen.get("acct") == "DreamShepherdbot1"
    finally:
        monkeypatch.undo()


# ── 台账手动关账（closed_manual，系统外平仓收尾）────────────

def test_manual_close_open_put(_mock_sdk):
    e = ot.add_ledger(kind="open_put", status="open", inst_id="SOL-USD_UM-260908-104-P",
                      side="sell", sz=1, px=0.27, collateral_usd=10.4, account="A")
    got = ot.manual_close_entry(e["id"])
    assert got is not None
    assert got["status"] == "closed_manual"
    assert got["close_ts"]
    assert "手动关账" in got.get("note", "")
    # 已关账行不可再次关账（幂等）
    assert ot.manual_close_entry(e["id"]) is None


def test_manual_close_custom_note(_mock_sdk):
    e = ot.add_ledger(kind="open_put", status="open", inst_id="SOL-USD_UM-260909-100-P",
                      side="sell", sz=1, account="A")
    got = ot.manual_close_entry(e["id"], note="OKX 后台手动平仓测试")
    assert got["status"] == "closed_manual"
    assert got["note"] == "OKX 后台手动平仓测试"


def test_manual_close_rejects_non_open(_mock_sdk):
    # settled / closed 行不可手动关账
    s = ot.add_ledger(kind="open_put", status="settled_otm", inst_id="SOL-USD_UM-260910-90-P",
                      side="sell", sz=1, account="A")
    assert ot.manual_close_entry(s["id"]) is None
    c = ot.add_ledger(kind="close_put", status="closed", inst_id="SOL-USD_UM-260910-90-P",
                      side="buy", sz=1, account="A")
    assert ot.manual_close_entry(c["id"]) is None
    # 未知 id
    assert ot.manual_close_entry("deadbeef") is None


def test_reopen_closed_manual(_mock_sdk):
    # 撤销手动关账：closed_manual → open（交回 settle 管辖）
    e = ot.add_ledger(kind="open_put", status="open", inst_id="SOL-USD_UM-260907-106-P",
                      side="sell", sz=1, px=0.46, account="A")
    ot.manual_close_entry(e["id"])
    got = ot.reopen_entry(e["id"])
    assert got is not None
    assert got["status"] == "open"
    assert "已撤销手动关账" in got.get("note", "")
    # 再关账后可再撤销（循环可用）
    ot.manual_close_entry(e["id"])
    assert ot.reopen_entry(e["id"])["status"] == "open"


def test_reopen_rejects_non_closed_manual(_mock_sdk):
    # 仅 closed_manual 可撤销；open / settled 不可
    o = ot.add_ledger(kind="open_put", status="open", inst_id="SOL-USD_UM-260911-95-P",
                      side="sell", sz=1, account="A")
    assert ot.reopen_entry(o["id"]) is None
    s = ot.add_ledger(kind="open_put", status="settled_otm", inst_id="SOL-USD_UM-260911-95-P",
                      side="sell", sz=1, account="A")
    assert ot.reopen_entry(s["id"]) is None
    # 未知 id
    assert ot.reopen_entry("deadbeef") is None


# ── 到期 ITM 补买预填（cover_prefill_defaults）────────────

def test_cover_prefill_defaults_qty_and_px(monkeypatch):
    # SOL 面值走家族常量（已到期合约 OKX 不再返回规格 → 不依赖 resolve_instrument）
    monkeypatch.setattr(ot, "resolve_instrument",
                        lambda iid: (_ for _ in ()).throw(RuntimeError("expired")))
    # 数量 = 面值×张数；价格默认现货现价
    d = ot.cover_prefill_defaults("SOL-USD_UM-260907-106-P", 1,
                                  spot_px=104.5, entry={"settle_px": 104.44})
    assert d["qty"] == 0.1
    assert d["px"] == 104.5
    assert d["px_src"] == "spot"
    # 2 张
    d2 = ot.cover_prefill_defaults("SOL-USD_UM-260907-106-P", 2,
                                   spot_px=104.5, entry={})
    assert d2["qty"] == 0.2


def test_cover_prefill_fallback_settle_then_strike(monkeypatch):
    monkeypatch.setattr(ot, "resolve_instrument",
                        lambda iid: {"lot": 0.01, "inst_id": iid})
    # 现货取价失败 → 结算价 → 行权价
    d = ot.cover_prefill_defaults("BTC-USD_UM-260912-60000-P", 1,
                                  spot_px=None,
                                  entry={"settle_px": 59200.0, "strike": 60000})
    assert d["px"] == 59200.0
    assert d["px_src"] == "settle"
    d2 = ot.cover_prefill_defaults("BTC-USD_UM-260912-60000-P", 1,
                                   spot_px=None, entry={"strike": 60000})
    assert d2["px"] == 60000.0
    assert d2["px_src"] == "strike"
    # 无任何价格 → px None（页面提示手填）
    d3 = ot.cover_prefill_defaults("BTC-USD_UM-260912-60000-P", 1,
                                   spot_px=None, entry=None)
    assert d3["px"] is None
    assert d3["px_src"] is None



# ── 批 1 Step 1：卖 call（covered call）执行层（2026-09-08）──────


def test_place_sell_call_params(_mock_sdk, _patch_entry):
    """卖 call 下单镜像 put：side=sell isolated；成交回填，kind=open_call。"""
    entry = ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                         ord_type="limit", px=110.0)
    call = _mock_sdk.calls[-1]
    assert call["instId"] == "BTC-USD_UM-260904-80000-C"
    assert call["side"] == "sell"
    assert call["tdMode"] == "isolated"
    assert call["sz"] == "1"
    assert call["tag"] == ot.TAG_OPEN
    assert entry["status"] == "open"
    assert entry["kind"] == "open_call"
    assert entry["filled_px"] == 110.0
    assert entry["premium_usd"] == pytest.approx(110.0 * 0.01)
    # call 不做全损现金担保：不触发 set_margin_balance，记 covered 语义
    assert _mock_sdk.account.margin_calls == []
    assert "covered" in (entry.get("margin_note") or "")


def test_open_call_rejects_put_inst(_patch_entry):
    """open_call 传 put 合约 → 拒绝。"""
    with pytest.raises(OkxSdkError):
        ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                     ord_type="limit", px=110.0)


def test_open_call_guard_fail_closed(_patch_entry):
    """保本门：cost_basis > K+px → fail-closed 拒绝，该轮跳过不硬卖。"""
    with pytest.raises(OkxSdkError) as ei:
        ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                     ord_type="limit", px=110.0, cost_basis=80200.0)  # K+px=80110 < C
    assert "保本门不通过" in str(ei.value)


def test_open_call_guard_ok(_mock_sdk, _patch_entry):
    """保本门通过：K+px ≥ C → 正常开仓，台账记录 cost_basis。"""
    entry = ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                         ord_type="limit", px=110.0, cost_basis=80000.0)
    assert entry["status"] == "open"
    assert entry["cost_basis"] == pytest.approx(80000.0)
    assert entry["strike"] == 80000.0


def test_place_close_call_buy(_mock_sdk, _patch_entry):
    """买回平仓卖 call（止盈/主动落袋）：kind=close_call，pnl=(开−平)×lot×sz。"""
    ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                 ord_type="limit", px=110.0)
    e = ot.close_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                      ord_type="limit", px=108.0)
    call = _mock_sdk.calls[-1]
    assert call["side"] == "buy"
    assert call["tdMode"] == "isolated"
    assert call["tag"] == ot.TAG_CLOSE
    assert e["status"] == "closed"
    assert e["pnl_usd"] == pytest.approx(0.02)  # (110−108)×0.01×1
    entries = ot.load_ledger()
    open_rows = [x for x in entries if x["kind"] == "open_call"
                 and x["inst_id"] == "BTC-USD_UM-260904-80000-C"]
    assert open_rows[0]["status"] == "closed"


def test_close_call_without_open_rejected(_patch_entry):
    """无 open 卖 call 记录 → 无法平仓。"""
    with pytest.raises(OkxSdkError):
        ot.close_call("bot1", inst_id="BTC-USD_UM-260904-88000-C", sz=1,
                      ord_type="limit", px=5)


def test_close_call_does_not_touch_put_rows(_mock_sdk, _patch_entry):
    """call 平仓只匹配 open_call——同名 strike 的 put 行不受影响。"""
    ot.open_put("bot1", inst_id="BTC-USD_UM-260904-80000-P", sz=1,
                ord_type="limit", px=110.0)
    ot.open_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                 ord_type="limit", px=110.0)
    e = ot.close_call("bot1", inst_id="BTC-USD_UM-260904-80000-C", sz=1,
                      ord_type="limit", px=108.0)
    assert e["status"] == "closed"
    entries = ot.load_ledger()
    put_row = [x for x in entries if x["kind"] == "open_put"
               and x["inst_id"] == "BTC-USD_UM-260904-80000-P"][0]
    assert put_row["status"] == "open"  # put 行仍 open，未被 call 平仓误碰


def test_preview_open_call_limit(_mock_sdk):
    p = ot.preview_open_call("BTC-USD_UM-260904-80000-C", 1, "limit", px=110.0)
    assert p["ok"] is True
    assert p["opt_type"] == "C"
    assert p["est_premium_usd"] == pytest.approx(110.0 * 0.01)
    assert p["td_mode"] == "isolated"
    assert "covered" in p["note"]
    assert p["gate"] is None  # 未提供 cost_basis 不显示保本门


def test_preview_open_call_gate(_mock_sdk):
    ok = ot.preview_open_call("BTC-USD_UM-260904-80000-C", 1, "limit",
                              px=110.0, cost_basis=80000.0)
    assert ok["gate"]["ok"] is True
    bad = ot.preview_open_call("BTC-USD_UM-260904-80000-C", 1, "limit",
                               px=110.0, cost_basis=80150.0)
    assert bad["gate"]["ok"] is False


def test_preview_open_call_rejects_put(_mock_sdk):
    with pytest.raises(OkxSdkError):
        ot.preview_open_call("BTC-USD_UM-260904-80000-P", 1, "limit", px=10)


def test_preview_open_call_rejects_market(_mock_sdk):
    with pytest.raises(OkxSdkError):
        ot.preview_open_call("BTC-USD_UM-260904-80000-C", 2, "market")
