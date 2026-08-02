"""Unit tests for onchainos wallet management tools (no CLI binary needed)."""

import json
import types

import pytest
from nanobot_quant.tools.tools_wallet import (
    ONCHAINOS_BIN,
    _ok_data,
    _run_cli,
    wallet_add,
    wallet_addresses,
    wallet_balance,
    wallet_chains,
    wallet_history,
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
