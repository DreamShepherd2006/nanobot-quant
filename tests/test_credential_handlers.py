"""credential_handlers tests — POST /config/credentials/{name}/save.

Regression: credential_save referenced an undefined `spec` variable
(NameError) — the gate spec is the first one with a normalizer, so the
normalize branch had never executed before. Driven via asyncio.run() so
tests stay plain-sync (no pytest-asyncio).
"""

import asyncio

import nanobot_quant.gate_spec  # noqa: F401 — registers the gate spec
from nanobot_quant.credential_handlers import credential_save
from nanobot_quant.credential_registry import discover


class _FakeRequest:
    def __init__(self, name: str, body: dict):
        self.path_params = {"name": name}
        self._body = body

    async def json(self):
        return self._body


class TestCredentialSave:
    def test_unknown_credential_404(self):
        resp = asyncio.run(credential_save(_FakeRequest("nope", {})))
        assert resp.status_code == 404

    def test_save_gate_applies_normalizer(self, monkeypatch):
        written: dict = {}
        monkeypatch.setattr(
            "nanobot_quant.credential_handlers.write_credential",
            lambda name, data: written.update({name: data}),
        )
        monkeypatch.setattr(
            "nanobot_quant.credential_handlers.discover",
            lambda: discover(),
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: None,
        )
        body = {
            "api_key": "",
            "api_secret": "",
            "uid": "15119093",
            "sub_gate_bot1_uid": "59175220",
        }
        resp = asyncio.run(credential_save(_FakeRequest("gate", body)))
        assert resp.status_code == 200
        saved = written["gate"]
        assert saved["main"]["uid"] == "15119093"
        # No stored key/secret — empty form stays empty.
        assert saved["main"]["api_key"] == ""
        assert saved["main"]["api_secret"] == ""
        assert saved["sub_accounts"]["gate_bot1"]["uid"] == "59175220"
        assert saved["slot_map"]["1"] == "gate_bot1"

    def test_save_gate_keeps_stored_main_keys(self, monkeypatch):
        written: dict = {}
        monkeypatch.setattr(
            "nanobot_quant.credential_handlers.write_credential",
            lambda name, data: written.update({name: data}),
        )
        monkeypatch.setattr(
            "nanobot_quant.credential_handlers.discover",
            lambda: discover(),
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: {"main": {"api_key": "stored-k", "api_secret": "stored-s"}},
        )
        body = {
            "api_key": "",
            "api_secret": "",
            "uid": "15119093",
        }
        resp = asyncio.run(credential_save(_FakeRequest("gate", body)))
        assert resp.status_code == 200
        saved = written["gate"]
        assert saved["main"]["api_key"] == "stored-k"
        assert saved["main"]["api_secret"] == "stored-s"
        assert saved["main"]["uid"] == "15119093"
