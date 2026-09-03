"""Tests for the OKX v5 signed client (nanobot_quant.okx_cex_api)."""

import pytest

from nanobot_quant import okx_cex_api as m

CREDS = {"api_key": "api-key-x", "secret_key": "test-secret", "passphrase": "pp"}


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)[:300]

    def json(self):
        return self._payload


@pytest.fixture
def fake_request(monkeypatch):
    """Capture signed requests; default payload is an OKX success envelope."""
    captured = {}

    def _req(method, url, headers=None, data=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return captured.get("resp", _FakeResp({"code": "0", "data": [], "msg": ""}))

    monkeypatch.setattr(m.requests, "request", _req)
    return captured


# ── signing ──────────────────────────────────────────────────────


def test_sign_golden_vector():
    # Golden vectors computed with the same HMAC-SHA256 formula.
    assert (
        m._sign("test-secret", "2026-09-03T00:00:00.000Z", "GET",
                "/api/v5/account/balance", "")
        == "Wi2PR5kvn1VSOLo8Q8kslS3+r+8XuyOLZU4p1t6LMWE="
    )


def test_sign_path_includes_query():
    assert (
        m._sign("test-secret", "2026-09-03T00:00:00.000Z", "GET",
                "/api/v5/account/positions?instType=OPTION", "")
        == "g1aRsmLazxdLydnFj3/DEZkT0K4qFTvfjYfgHuOEz0A="
    )


def test_headers_shape(fake_request):
    fake_request["resp"] = _FakeResp({"code": "0", "data": [{}]})
    m.get_account_config(creds=CREDS)
    h = fake_request["headers"]
    assert h["OK-ACCESS-KEY"] == "api-key-x"
    assert h["OK-ACCESS-PASSPHRASE"] == "pp"
    assert h["OK-ACCESS-TIMESTAMP"].endswith("Z")
    # SIGN must be valid base64 of the HMAC of (ts + method + path + body)
    import base64
    base64.b64decode(h["OK-ACCESS-SIGN"], validate=True)


# ── request envelope / errors ────────────────────────────────────


def test_okx_request_returns_data(fake_request):
    fake_request["resp"] = _FakeResp({"code": "0", "data": [{"uid": "1"}]})
    assert m.okx_request("GET", "/api/v5/account/config", creds=CREDS) == [
        {"uid": "1"}
    ]


def test_okx_request_raises_on_api_error(fake_request):
    fake_request["resp"] = _FakeResp({"code": "50111", "msg": "Invalid OK-ACCESS-KEY"})
    with pytest.raises(RuntimeError, match="50111"):
        m.okx_request("GET", "/api/v5/account/config", creds=CREDS)


def test_okx_request_raises_on_http_error(fake_request):
    fake_request["resp"] = _FakeResp({"code": "0"}, status=401)
    with pytest.raises(RuntimeError, match="401"):
        m.okx_request("GET", "/api/v5/account/config", creds=CREDS)


def test_okx_request_posts_body_json(fake_request):
    fake_request["resp"] = _FakeResp({"code": "0", "data": {}})
    m.okx_request("POST", "/api/v5/trade/order", creds=CREDS, body={"instId": "x"})
    import json

    assert json.loads(fake_request["data"]) == {"instId": "x"}
    assert fake_request["url"] == "https://www.okx.com/api/v5/trade/order"


# ── read-only endpoints / parsing ────────────────────────────────


def test_get_account_config(fake_request):
    fake_request["resp"] = _FakeResp(
        {"code": "0", "data": [{"uid": "881", "acctLv": "1", "perm": "read_only,trade"}]}
    )
    cfg = m.get_account_config(creds=CREDS)
    assert cfg["uid"] == "881"
    assert "read_only" in cfg["perm"]
    assert fake_request["url"].endswith("/api/v5/account/config")


def test_get_balance_normalizes_fields(fake_request):
    fake_request["resp"] = _FakeResp(
        {
            "code": "0",
            "data": [
                {
                    "totalEq": "1234.5",
                    "details": [
                        {
                            "ccy": "BTC",
                            "cashBal": "0.01",
                            "availBal": "0.009",
                            "frozenBal": "0.001",
                            "eq": "0.01",
                        },
                        {"ccy": "USDC", "cashBal": "", "eq": "0"},
                    ],
                }
            ],
        }
    )
    bal = m.get_balance(creds=CREDS)
    assert bal["total_eq"] == 1234.5
    assert bal["details"][0] == {
        "ccy": "BTC",
        "cash": 0.01,
        "avail": 0.009,
        "frozen": 0.001,
        "eq": 0.01,
    }
    assert bal["details"][1]["cash"] == 0.0  # empty string coerced


def test_get_positions_uses_option_insttype(fake_request):
    fake_request["resp"] = _FakeResp(
        {"code": "0", "data": [{"instId": "BTC-USD-260904-78000-P", "pos": "1"}]}
    )
    pos = m.get_positions(creds=CREDS)
    assert len(pos) == 1
    assert pos[0]["instId"].endswith("-P")
    assert "instType=OPTION" in fake_request["url"]


def test_get_trade_fee(fake_request):
    fake_request["resp"] = _FakeResp(
        {"code": "0", "data": [{"maker": "-0.0003", "taker": "-0.0003"}]}
    )
    fee = m.get_trade_fee(inst_family="ETH-USD", creds=CREDS)
    assert fee["maker"] == "-0.0003"
    assert "ETH-USD" in fake_request["url"]
