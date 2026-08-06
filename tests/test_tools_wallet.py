"""Unit tests for onchainos wallet management tools (no CLI binary needed)."""

import json
import os
import types

import pytest
from nanobot_quant.tools.tools_wallet import (
    ONCHAINOS_BIN,
    _ok_data,
    _run_cli,
    get_active_wallet_address,
    wallet_accounts,
    wallet_add,
    wallet_addresses,
    wallet_balance,
    wallet_chains,
    wallet_history,
    wallet_send,
    wallet_status,
    wallet_switch,
)


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def fake_subprocess(monkeypatch):
    """Replace subprocess.run in tools_wallet with a recorded fake."""
    calls = []

    def _fake_run(args, capture_output=True, text=True, timeout=30):
        calls.append(args)
        return _FakeProc(0, json.dumps({"ok": True, "data": {"probe": "ok"}}))

    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _fake_run)
    monkeypatch.setattr("nanobot_quant.tools.tools_wallet.sys.stderr", types.SimpleNamespace(
        write=lambda *a, **k: None, flush=lambda: None,
    ))
    return calls


class TestWalletSend:
    def test_send_native_coin(self, fake_subprocess):
        resp = wallet_send("solana", "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq", "1.5")
        assert resp.get("status") == "ok"
        args = fake_subprocess[-1]
        assert args[:3] == [ONCHAINOS_BIN, "wallet", "send"]
        assert args[args.index("--chain") + 1] == "solana"
        assert args[args.index("--to") + 1] == "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq"
        assert args[args.index("--readable-amount") + 1] == "1.5"
        assert "--contract-token" not in args

    def test_send_token_with_from_and_force(self, fake_subprocess):
        resp = wallet_send(
            "solana", "6HWbojG7Kb6vRHsWbUX5858yVHxQqcWTxv8k8nHNyN1s", "0.01",
            contract_token="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            from_address="E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq",
            force=True,
        )
        assert resp.get("status") == "ok"
        args = fake_subprocess[-1]
        assert args[args.index("--contract-token") + 1] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        assert args[args.index("--from") + 1] == "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq"
        assert args[-1] == "--force"

    def test_send_missing_args(self):
        resp = wallet_send("", "", "")
        assert resp["status"] == "error"

    def test_send_cli_error_propagated(self, monkeypatch):
        def _fake_run(args, capture_output=True, text=True, timeout=30):
            return _FakeProc(1, json.dumps({"ok": False, "error": "[52001] Insufficient balance"}))

        monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _fake_run)
        resp = wallet_send("solana", "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq", "9999")
        assert resp["status"] == "error"
        assert "52001" in resp["error"]


class TestRunCli:
    def test_envelope_ok(self):
        resp = _run_cli([ONCHAINOS_BIN, "wallet", "status"], label="wallet_status")
        assert resp.get("ok") is True
        assert resp["data"] == {"probe": "ok"}

    def test_envelope_error(self, monkeypatch):
        def _fake_run(args, capture_output=True, text=True, timeout=30):
            return _FakeProc(1, json.dumps({"ok": False, "error": "[52001] Insufficient balance"}))

        monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _fake_run)
        resp = _run_cli([ONCHAINOS_BIN, "wallet", "balance"], label="wallet_balance")
        assert resp.get("ok") is False
        assert resp["error"] == "[52001] Insufficient balance"

    def test_missing_binary(self, monkeypatch):
        def _raise(args, capture_output=True, text=True, timeout=30):
            raise FileNotFoundError("no such file")

        monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _raise)
        resp = _run_cli([ONCHAINOS_BIN, "wallet", "status"], label="wallet_status")
        assert "not found" in resp["error"]

    def test_timeout(self, monkeypatch):
        import subprocess as _sp

        def _raise(args, capture_output=True, text=True, timeout=30):
            raise _sp.TimeoutExpired(args, timeout)

        monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _raise)
        resp = _run_cli([ONCHAINOS_BIN, "wallet", "status"], label="wallet_status")
        assert "timed out" in resp["error"]

    def test_non_json_output(self, monkeypatch):
        def _fake_run(args, capture_output=True, text=True, timeout=30):
            return _FakeProc(0, "raw text output")

        monkeypatch.setattr("nanobot_quant.tools.tools_wallet.subprocess.run", _fake_run)
        resp = _run_cli([ONCHAINOS_BIN, "wallet", "chains"], label="wallet_chains")
        assert resp["data"] == "raw text output"


class TestOkData:
    def test_ok(self):
        assert _ok_data({"ok": True, "data": {"a": 1}}) == {"status": "ok", "data": {"a": 1}}

    def test_error(self):
        assert _ok_data({"ok": False, "error": "boom"}) == {"status": "error", "error": "boom"}

    def test_error_dict(self):
        assert _ok_data({"ok": False, "error": {"code": 5}}) == {"status": "error", "error": '{"code": 5}'}


class TestWalletTools:
    def test_status(self, fake_subprocess):
        r = wallet_status()
        assert r["status"] == "ok"
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "status"]

    def test_addresses_plain(self, fake_subprocess):
        wallet_addresses()
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "addresses"]

    def test_addresses_chain(self, fake_subprocess):
        wallet_addresses(chain="solana")
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "addresses", "--chain", "solana"]

    def test_balance_all_force(self, fake_subprocess):
        wallet_balance(all_accounts=True, force=True)
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "balance", "--all", "--force"]

    def test_balance_chain_token(self, fake_subprocess):
        wallet_balance(chain="solana", token_address="abc")
        assert fake_subprocess[-1] == [
            ONCHAINOS_BIN, "wallet", "balance", "--chain", "solana", "--token-address", "abc",
        ]

    def test_chains(self, fake_subprocess):
        wallet_chains()
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "chains"]

    def test_history_filters(self, fake_subprocess):
        wallet_history(chain="solana", limit="5")
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "history", "--chain", "solana", "--limit", "5"]

    def test_add(self, fake_subprocess):
        wallet_add()
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "add"]

    def test_switch(self, fake_subprocess):
        wallet_switch(account_id="acct-1")
        assert fake_subprocess[-1] == [ONCHAINOS_BIN, "wallet", "switch", "acct-1"]

    def test_switch_missing_id(self, fake_subprocess):
        r = wallet_switch(account_id="")
        assert r["status"] == "error"
        assert "account_id is required" in r["error"]
        assert len(fake_subprocess) == 0

    def test_get_active_wallet_address_solana(self, monkeypatch, fake_subprocess):
        def _fake_wallet_addresses(chain=""):
            return {"status": "ok", "data": {
                "accountId": "acct-1",
                "xlayer": [],
                "evm": [],
                "solana": [{"address": "E71V4Qe...", "chainIndex": "501", "chainName": "Solana"}],
            }}
        monkeypatch.setattr(
            "nanobot_quant.tools.tools_wallet.wallet_addresses",
            _fake_wallet_addresses,
        )
        assert get_active_wallet_address("solana") == "E71V4Qe..."
        # default chain is solana
        assert get_active_wallet_address() == "E71V4Qe..."

    def test_get_active_wallet_address_xlayer(self, monkeypatch):
        def _fake_wallet_addresses(chain=""):
            return {"status": "ok", "data": {
                "accountId": "acct-1",
                "xlayer": [{"address": "0xabc", "chainIndex": "196", "chainName": "X Layer"}],
                "evm": [],
                "solana": [],
            }}
        monkeypatch.setattr(
            "nanobot_quant.tools.tools_wallet.wallet_addresses",
            _fake_wallet_addresses,
        )
        assert get_active_wallet_address("xlayer") == "0xabc"

    def test_get_active_wallet_address_not_logged_in(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.tools.tools_wallet.wallet_addresses",
            lambda chain="": {"status": "error", "error": "not logged in"},
        )
        assert get_active_wallet_address("solana") is None

    def test_get_active_wallet_address_empty_group(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.tools.tools_wallet.wallet_addresses",
            lambda chain="": {"status": "ok", "data": {"xlayer": [], "evm": [], "solana": []}},
        )
        assert get_active_wallet_address("solana") is None


class TestWalletAccounts:
    """wallet_accounts reads ~/.onchainos/wallets.json (no CLI binary)."""

    @pytest.fixture(autouse=True)
    def _no_symlink(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.tools.tools_wallet._ensure_onchainos_dir",
            lambda: None,
        )

    @pytest.fixture
    def wallets_json(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        onchainos = home / ".onchainos"
        onchainos.mkdir(parents=True)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(home / p.lstrip("~/")))
        return onchainos / "wallets.json"

    def test_reads_accounts_and_addresses(self, wallets_json):
        wallets_json.write_text(json.dumps({
            "selected_account_id": "acct-1",
            "accounts": [
                {"account_id": "acct-1", "account_name": "My Wallet", "is_default": True},
                {"account_id": "acct-2", "account_name": "Savings", "is_default": False},
            ],
            "accounts_map": {
                "acct-1": {"address_list": [
                    {"chain_name": "Solana", "chain_index": "501", "address": "SolAddr1", "address_type": "wallet"},
                    {"chain_name": "EVM", "chain_index": "196", "address": "EvmAddr1", "address_type": "wallet"},
                ]},
                "acct-2": {"address_list": [
                    {"chain_name": "Solana", "chain_index": "501", "address": "SolAddr2", "address_type": "wallet"},
                ]},
            },
        }), encoding="utf-8")
        r = wallet_accounts()
        assert r["status"] == "ok"
        data = r["data"]
        assert data["selected_account_id"] == "acct-1"
        assert len(data["accounts"]) == 2
        first = data["accounts"][0]
        assert first["account_id"] == "acct-1"
        assert first["account_name"] == "My Wallet"
        assert first["is_default"] is True
        assert first["is_active"] is True
        assert len(first["addresses"]) == 2
        assert first["addresses"][0] == {
            "chain": "Solana", "chain_index": "501",
            "address": "SolAddr1", "type": "wallet",
        }
        second = data["accounts"][1]
        assert second["is_active"] is False
        assert second["addresses"][0]["address"] == "SolAddr2"

    def test_supports_camelcase_fields(self, wallets_json):
        wallets_json.write_text(json.dumps({
            "selectedAccountId": "acct-9",
            "accounts": [
                {"accountId": "acct-9", "accountName": "Alpha", "isDefault": True},
            ],
            "accountsMap": {
                "acct-9": {"addressList": [
                    {"chainName": "Solana", "chainIndex": "501", "address": "SolAddr9", "addressType": "wallet"},
                ]},
            },
        }), encoding="utf-8")
        r = wallet_accounts()
        assert r["status"] == "ok"
        acc = r["data"]["accounts"][0]
        assert acc["account_id"] == "acct-9"
        assert acc["account_name"] == "Alpha"
        assert acc["is_active"] is True
        assert acc["addresses"][0]["chain"] == "Solana"

    def test_missing_file(self, wallets_json):
        r = wallet_accounts()
        assert r["status"] == "error"
        assert "wallets.json" in r["error"]

    def test_invalid_json(self, wallets_json):
        wallets_json.write_text("{not json", encoding="utf-8")
        r = wallet_accounts()
        assert r["status"] == "error"
        assert "解析失败" in r["error"] or "读取失败" in r["error"]
