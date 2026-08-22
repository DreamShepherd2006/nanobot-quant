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
            "sub_0_name": "gate_bot1",
            "sub_0_uid": "59175220",
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


class TestRenderGroups:
    """FieldSpec.group — gate form renders per-account card sections."""

    def test_gate_renders_main_group_only_when_empty(self, monkeypatch):
        import nanobot_quant.credential_handlers as ch
        from nanobot_quant.credential_registry import discover

        monkeypatch.setattr(ch, "read_credential", lambda name: None)
        spec = discover()["gate"]
        html = ch._render_detail_form(spec)
        # 无配置时仅渲染主账号组；子账号行动态渲染（工具栏按钮添加/同步）
        assert html.count('<div class="cred-group">') == 1
        assert "从 Gate 同步子账号" in html
        assert "syncSubs" in html
        assert '🏛️ 主账号' in html
        # 空配置不渲染子账号行（动态行靠按钮添加/同步）
        assert 'id="sub_0_name"' not in html
        assert 'id="sub_0_uid"' not in html
        # 主账号字段也在
        assert 'id="api_key"' in html and 'id="uid"' in html

    def test_ungrouped_spec_renders_flat(self, monkeypatch):
        """无 group 的 spec（如 OKX）保持平铺，不出现分组卡片。"""
        import nanobot_quant.credential_handlers as ch
        from nanobot_quant.credential_registry import discover

        monkeypatch.setattr(ch, "read_credential", lambda name: None)
        spec = discover()["okx"]
        assert all(not f.group for f in spec.fields)
        html = ch._render_detail_form(spec)
        assert '<div class="cred-group">' not in html
