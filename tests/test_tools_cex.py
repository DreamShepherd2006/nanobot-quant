"""Tests for cex_sub_order (sub-account real order tool)."""

from __future__ import annotations

import sys
from unittest.mock import patch

sys.path.insert(0, "src")  # noqa: E402

from nanobot_quant.tools.tools_cex import cex_sub_order


def _creds_with(sub_account="gate_bot2", key="k2", secret="s2", uid="59175258"):
    return {
        "main": {"uid": "15119093", "api_key": "mk", "api_secret": "ms"},
        "slot_map": {"1": "gate_bot1", "2": "gate_bot2"},
        "sub_accounts": {
            "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"},
            sub_account: {"uid": uid, "api_key": key, "api_secret": secret},
        },
    }


class _FakeOrder:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self):
        return dict(self.__dict__)


def test_side_invalid():
    assert cex_sub_order("CRCLX", "hold", 1.0, "gate_bot2")["status"] == "error"


def test_amount_invalid():
    assert cex_sub_order("CRCLX", "buy", 0, "gate_bot2")["status"] == "error"
    assert cex_sub_order("CRCLX", "buy", "abc", "gate_bot2")["status"] == "error"


def test_sub_account_missing():
    with patch("nanobot_quant.tools.tools_cex.load_gate_credentials", return_value=_creds_with()):
        r = cex_sub_order("CRCLX", "buy", 3.0, "gate_bot9")
    assert r["status"] == "error"
    assert "gate_bot9" in r["error"]


def test_sub_account_missing_keys():
    with patch("nanobot_quant.tools.tools_cex.load_gate_credentials",
               return_value=_creds_with(key="", secret="")):
        r = cex_sub_order("CRCLX", "buy", 3.0, "gate_bot2")
    assert r["status"] == "error"
    assert "API Key/Secret" in r["error"]


def test_filled_path():
    """市价单 → 轮询 → closed → filled 明细。"""
    creds = _creds_with()

    def fake_create_order(api_key, api_secret, pair, side, amount, time_in_force=None):
        assert api_key == "k2" and api_secret == "s2"  # 子账号自己的 key
        assert pair == "CRCLX_USDT"
        assert side == "sell"
        assert amount == "0.05"
        assert time_in_force == "ioc"
        return {"id": "111", "status": "open", "pair": pair}

    closed = {
        "id": "111", "status": "closed", "filled_amount": "0.05",
        "avg_deal_price": "72.95", "fee": "0.00005", "finish_as": "filled",
    }
    with patch("nanobot_quant.tools.tools_cex.load_gate_credentials", return_value=creds), \
         patch("nanobot_quant.gate_sdk.create_order", side_effect=fake_create_order), \
         patch("nanobot_quant.gate_sdk.get_order", return_value=closed), \
         patch("nanobot_quant.tools.tools_cex.time.sleep"):
        r = cex_sub_order("CRCLX", "sell", 0.05, "gate_bot2")
    assert r["status"] == "filled"
    assert r["sub_account"] == "gate_bot2"
    assert r["filled_amount"] == "0.05"
    assert r["avg_deal_price"] == "72.95"
    assert r["order_id"] == "111"


def test_pending_path():
    """下单后一直 open → 返回 pending。"""
    open_order = {"id": "222", "status": "open"}
    with patch("nanobot_quant.tools.tools_cex.load_gate_credentials",
               return_value=_creds_with()), \
         patch("nanobot_quant.gate_sdk.create_order", return_value=open_order), \
         patch("nanobot_quant.gate_sdk.get_order", return_value=open_order), \
         patch("nanobot_quant.tools.tools_cex.time.sleep"):
        r = cex_sub_order("CRCLX", "buy", 3.0, "gate_bot2")
    assert r["status"] == "pending"
    assert r["order_id"] == "222"


def test_rejected_path():
    """订单被拒（无订单号）→ error 明确原因。"""
    with patch("nanobot_quant.tools.tools_cex.load_gate_credentials",
               return_value=_creds_with()), \
         patch("nanobot_quant.gate_sdk.create_order",
               return_value={"error": "balance insufficient"}):
        r = cex_sub_order("CRCLX", "buy", 3.0, "gate_bot2")
    assert r["status"] == "error"
    assert "balance insufficient" in r["error"]
