"""okx_options_trade 单元测试（mock okx_sdk 层，不触网）。

批次 C：卖 put 执行层 — 预览/下单参数/台账/两步确认/平仓/补买。
mock 对象替代 okx_sdk.public/market/trade_for/account_for 与凭证存储，
验证逻辑与参数构造；HTTP 与 SDK 请求不发出。
"""

import time
from datetime import datetime, timezone

import pytest

from nanobot_quant import okx_options_trade as ot
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


class _FakeTrade:
    def __init__(self):
        self.calls = []
        self.last_px = None

    def set_order(self, **params):
        self.calls.append(params)
        self.last_px = params.get("px", params.get("sz", "1"))
        return {"code": "0", "data": [{"ordId": f"ord-{len(self.calls)}", "clOrdId": ""}]}

    def get_order(self, instId=None, ordId=None, **kw):
        # 模拟限价单全部以限价成交：avg = 该单 px
        return {"code": "0", "data": [{"instId": instId, "ordId": ordId,
                                       "state": "filled", "avgPx": str(self.last_px),
                                       "accFillSz": "1", "fee": "-0.02"}]}


class _FakeAccount:
    def __init__(self):
        self.margin = 0.0          # 该仓当前保证金（get_positions 返回，0 → imr fallback）
        self.margin_calls = []     # set_margin_balance 调用记录
        self.margin_fail = None    # 非空时 set_margin_balance 抛 RuntimeError

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
    e = ot.spot_cover("bot1", spot_inst="BTC-USDC", quote_amt=50.0)
    call = _mock_sdk.calls[-1]
    assert call["instId"] == "BTC-USDC"
    assert call["side"] == "buy"
    assert call["tdMode"] == "cash"
    assert call["ordType"] == "market"
    assert call["sz"] == "50.00"
    assert call["tgtCcy"] == "quote_ccy"
    assert call["tag"] == ot.TAG_COVER
    assert e["status"] == "filled"


def test_spot_cover_base_qty(_mock_sdk, _patch_entry):
    ot.spot_cover("bot1", spot_inst="BTC-USDC", base_qty=0.01)
    call = _mock_sdk.calls[-1]
    assert call["tgtCcy"] == "base_ccy"
    assert call["sz"] == "0.01"


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
