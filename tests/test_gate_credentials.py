"""P1: gate.json credential loading + CEX symbol mapping tests.

Covered:
- gate_pair / okx_ticker mapping (tokens.json gate_symbol / okx_symbol)
- get_api_credentials (main / sub-account by name / by uid)
- fetch_spot_balances (signed GET /spot/accounts parsing)
"""

import pytest

from nanobot_quant.gate_credentials import (
    fetch_spot_balances,
    gate_pair,
    get_api_credentials,
    load_gate_credentials,
    okx_ticker,
)

TOKENS = [
    {
        "symbol": "CRCLX",
        "chain": "solana",
        "address": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
        "gate_symbol": "CRCLX",
        "okx_symbol": "XCRCL",
    },
    {"symbol": "SOL", "chain": "solana", "address": "So11111111111111111111111111111111111111112"},
]

CREDS = {
    "main": {"api_key": "k", "api_secret": "s", "uid": "15119093"},
    "sub_accounts": {
        "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"},
    },
}


class TestGatePair:
    def test_default_pair(self):
        assert gate_pair("CRCLX", []) == "CRCLXUSDT"

    def test_tokens_mapping(self):
        assert gate_pair("CRCLX", TOKENS) == "CRCLXUSDT"

    def test_dash_normalized(self):
        assert gate_pair("CRCLX-USDT", []) == "CRCLXUSDT"

    def test_already_usdt(self):
        assert gate_pair("CRCLXUSDT", []) == "CRCLXUSDT"


class TestOkxTicker:
    def test_mapped(self):
        assert okx_ticker("CRCLX", TOKENS) == "XCRCL"

    def test_default(self):
        assert okx_ticker("SOL", TOKENS) == "SOL"

    def test_case_insensitive(self):
        assert okx_ticker("crclx", TOKENS) == "XCRCL"


class TestCredentials:
    def test_main(self):
        c = get_api_credentials(CREDS)
        assert c["uid"] == "15119093"

    def test_sub_by_name(self):
        c = get_api_credentials(CREDS, "gate_bot1")
        assert c["uid"] == "59175220"

    def test_sub_by_uid(self):
        c = get_api_credentials(CREDS, "59175220")
        assert c["api_key"] == "k1"

    def test_unknown_sub(self):
        with pytest.raises(KeyError):
            get_api_credentials(CREDS, "nope")

    def test_missing_creds_raises(self):
        with pytest.raises(FileNotFoundError):
            get_api_credentials(None)

    def test_missing_file_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_credentials._credential_paths",
            lambda: ["/nonexistent/gate.json"],
        )
        assert load_gate_credentials() is None


class TestFlatCredentials:
    """P2: WebUI 凭证表单写入 flat gate.json → 归一化 nested（main 键）。"""

    def test_flat_normalised_to_main(self, monkeypatch, tmp_path):
        import json

        p = tmp_path / "gate.json"
        p.write_text(
            json.dumps({"api_key": "k", "api_secret": "s", "uid": "15119093"})
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_credentials._credential_paths",
            lambda: [str(p)],
        )
        creds = load_gate_credentials()
        assert creds["main"] == {"api_key": "k", "api_secret": "s", "uid": "15119093"}
        assert creds["sub_accounts"] == {}
        # flat 兜底：get_api_credentials 在 creds 无 main 键时直接透传
        assert get_api_credentials(creds) == {
            "api_key": "k",
            "api_secret": "s",
            "uid": "15119093",
        }

    def test_nested_untouched(self):
        assert get_api_credentials(CREDS) == CREDS["main"]

    def test_flat_direct_get_api_credentials(self):
        flat = {"api_key": "k", "api_secret": "s", "uid": "15119093"}
        assert get_api_credentials(flat) == flat


class TestFetchSpotBalances:
    def test_ok(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_credentials.signed_request",
            lambda *a, **k: (
                200,
                [{"currency": "USDT", "available": "1.5", "locked": "0"}],
            ),
        )
        b = fetch_spot_balances("k", "s")
        assert b == {"USDT": {"available": 1.5, "locked": 0.0}}

    def test_error_raises(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_credentials.signed_request",
            lambda *a, **k: (403, {"label": "Forbidden"}),
        )
        with pytest.raises(RuntimeError, match="HTTP 403"):
            fetch_spot_balances("k", "s")
