"""OKX CEX 私有账户只读 API 测试（mock okx_sdk.account_for 层）。

批次 A 自写签名客户端已于 2026-09-04 迁移到官方 python-okx SDK
（okx==1.0.9，见 okx_cex_api 模块 docstring）；签名 golden vector 测试
随手写 HMAC 层移除而删除。
"""

import pytest

from nanobot_quant import okx_cex_api as m
from nanobot_quant.okx_sdk import OkxSdkError

CREDS = {"api_key": "api-key-x", "secret_key": "test-secret",
         "passphrase": "pp", "name": "bot1", "uid": "123"}


class _FakeAccount:
    """记录调用并按预设 payload 返回的 Account 替身。"""

    def __init__(self):
        self.calls = []
        self.payloads = {}

    def _pay(self, name, default):
        self.calls.append(name)
        return self.payloads.get(name, default)

    def get_config(self):
        return self._pay("get_config",
                         {"code": "0", "data": [{"uid": "123", "acctLv": "1"}], "msg": ""})

    def get_balance(self, **kw):
        return self._pay("get_balance",
                         {"code": "0", "data": [{
                             "totalEq": "100.5",
                             "details": [{"ccy": "BTC", "cashBal": "1.1", "availBal": "1.0",
                                          "frozenBal": "0.1", "eq": "1.1"}],
                         }], "msg": ""})

    def get_positions(self, **kw):
        return self._pay("get_positions",
                         {"code": "0", "data": [{"instId": "BTC-USD-260911-80000-P"}], "msg": ""})

    def get_trade_fee(self, **kw):
        return self._pay("get_trade_fee",
                         {"code": "0", "data": [{"taker": "0.0005", "maker": "0.0002"}], "msg": ""})


@pytest.fixture
def fake_account(monkeypatch):
    acc = _FakeAccount()

    def _account_for(creds):
        return acc
    monkeypatch.setattr(m.okx_sdk, "account_for", _account_for)
    return acc


@pytest.fixture
def default_creds(monkeypatch):
    monkeypatch.setattr(m, "get_okx_cex_credentials",
                        lambda creds=None: CREDS)


# ── read-only endpoints ─────────────────────────────────────────

def test_get_account_config(fake_account, default_creds):
    cfg = m.get_account_config()
    assert cfg["uid"] == "123"
    assert fake_account.calls == ["get_config"]


def test_get_balance_normalized(fake_account, default_creds):
    bal = m.get_balance(creds=CREDS)
    assert bal["total_eq"] == 100.5
    d = bal["details"][0]
    assert d == {"ccy": "BTC", "cash": 1.1, "avail": 1.0,
                 "frozen": 0.1, "eq": 1.1}
    assert fake_account.calls == ["get_balance"]


def test_get_positions_default_option(fake_account, default_creds):
    pos = m.get_positions()
    assert pos[0]["instId"].endswith("-P")
    assert fake_account.calls == ["get_positions"]


def test_get_trade_fee(fake_account, default_creds):
    fee = m.get_trade_fee("BTC-USD")
    assert fee["taker"] == "0.0005"
    assert fake_account.calls == ["get_trade_fee"]


def test_api_error_raises_okx_sdk_error(fake_account, default_creds):
    fake_account.payloads["get_config"] = {"code": "50111", "msg": "Invalid key", "data": []}
    with pytest.raises(OkxSdkError, match="50111"):
        m.get_account_config()


def test_empty_balance_details(fake_account, default_creds):
    fake_account.payloads["get_balance"] = {"code": "0", "data": [{}], "msg": ""}
    bal = m.get_balance()
    assert bal == {"total_eq": 0.0, "details": []}
