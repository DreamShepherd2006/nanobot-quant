"""Tests for OKX CEX credential loading (nanobot_quant.okx_cex_credentials)."""

import json

import pytest

from nanobot_quant import okx_cex_credentials as m


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    p = tmp_path / "okx_cex.json"
    monkeypatch.setattr(m, "_get_credential_path", lambda: p)
    return p


def _write(p, data):
    p.write_text(json.dumps(data), "utf-8")


def test_load_returns_none_without_file(fake_path):
    assert m.load_okx_cex_credentials() is None


def test_load_returns_dict_from_file(fake_path):
    _write(fake_path, {"api_key": "k", "secret_key": "s", "passphrase": "p"})
    got = m.load_okx_cex_credentials()
    assert got == {"api_key": "k", "secret_key": "s", "passphrase": "p"}


def test_load_returns_empty_on_corrupt_file(fake_path):
    fake_path.write_text("{not json", "utf-8")
    assert m.load_okx_cex_credentials() == {}


def test_get_validates_all_three_fields(fake_path):
    _write(fake_path, {"api_key": "k", "secret_key": "s"})  # missing passphrase
    with pytest.raises(RuntimeError, match="passphrase"):
        m.get_okx_cex_credentials()


def test_get_with_no_file_raises(fake_path):
    with pytest.raises(RuntimeError, match="凭证不完整"):
        m.get_okx_cex_credentials()


def test_get_injected_creds_passthrough():
    creds = {"api_key": "k", "secret_key": "s", "passphrase": "p"}
    assert m.get_okx_cex_credentials(creds) == creds


def test_get_strips_extra_fields():
    creds = {"api_key": "k", "secret_key": "s", "passphrase": "p", "uid": "x"}
    got = m.get_okx_cex_credentials(creds)
    assert got == {"api_key": "k", "secret_key": "s", "passphrase": "p"}
