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


def _entry(name="bot1", uid="881574754615066858", **kw):
    e = {"name": name, "uid": uid, "api_key": "k", "secret_key": "s",
         "passphrase": "p"}
    e.update(kw)
    return e


# ── load / migration ─────────────────────────────────────────────


def test_load_returns_none_without_file(fake_path):
    assert m.load_okx_cex_credentials() is None


def test_load_normalizes_nested_file(fake_path):
    _write(fake_path, {"max_sub_accounts": 5, "sub_accounts": [_entry()]})
    got = m.load_okx_cex_credentials()
    assert got["max_sub_accounts"] == 5
    assert got["sub_accounts"][0]["name"] == "bot1"


def test_legacy_flat_migrated_to_first_row(fake_path):
    _write(fake_path, {"api_key": "k", "secret_key": "s", "passphrase": "p"})
    got = m.load_okx_cex_credentials()
    assert len(got["sub_accounts"]) == 1
    assert got["sub_accounts"][0]["api_key"] == "k"
    assert got["sub_accounts"][0]["name"] == ""


def test_load_returns_empty_canonical_on_corrupt_file(fake_path):
    fake_path.write_text("{not json", "utf-8")
    got = m.load_okx_cex_credentials()
    assert got["sub_accounts"] == []
    assert got["max_sub_accounts"] == 10


# ── save ─────────────────────────────────────────────────────────


def test_save_persists_canonical(fake_path):
    m.save_okx_cex_credentials(
        {"max_sub_accounts": 3, "sub_accounts": [_entry()]}
    )
    assert fake_path.exists()
    assert json.loads(fake_path.read_text())["sub_accounts"][0]["uid"] == (
        "881574754615066858"
    )


def test_save_migrates_flat(fake_path):
    m.save_okx_cex_credentials({"api_key": "k", "secret_key": "s", "passphrase": "p"})
    assert json.loads(fake_path.read_text())["sub_accounts"][0]["api_key"] == "k"


# ── listing ──────────────────────────────────────────────────────


def test_list_sub_accounts_config_flags(fake_path):
    _write(fake_path, {
        "sub_accounts": [
            _entry("a", "111"),
            _entry("b", "222", passphrase=""),  # incomplete
        ]
    })
    subs = m.list_sub_accounts()
    assert subs[0] == {"name": "a", "uid": "111", "configured": True, "missing": []}
    assert subs[1]["configured"] is False
    assert subs[1]["missing"] == ["passphrase"]


# ── get by account ───────────────────────────────────────────────


def test_get_returns_first_configured(fake_path):
    _write(fake_path, {
        "sub_accounts": [
            _entry("empty-a", "111", api_key="", secret_key="", passphrase=""),
            _entry("bot1", "881574754615066858"),
        ]
    })
    got = m.get_okx_cex_credentials()
    assert got == {"api_key": "k", "secret_key": "s", "passphrase": "p"}


def test_get_selects_by_uid(fake_path):
    _write(fake_path, {"sub_accounts": [_entry("a", "111"), _entry("b", "222")]})
    got = m.get_okx_cex_credentials(account="222")
    assert got["api_key"] == "k"  # both entries share creds in fixture


def test_get_selects_by_name(fake_path):
    _write(fake_path, {"sub_accounts": [_entry("alpha", "111"), _entry("beta", "222")]})
    got = m.get_okx_cex_credentials(account="alpha")
    assert got == {"api_key": "k", "secret_key": "s", "passphrase": "p"}


def test_get_unknown_account_raises(fake_path):
    _write(fake_path, {"sub_accounts": [_entry()]})
    with pytest.raises(RuntimeError, match="未找到子账户: nope"):
        m.get_okx_cex_credentials(account="nope")


def test_get_no_config_raises(fake_path):
    with pytest.raises(RuntimeError, match="未配置"):
        m.get_okx_cex_credentials()


def test_get_all_empty_raises_incomplete(fake_path):
    _write(fake_path, {"sub_accounts": [_entry(passphrase="")]})
    with pytest.raises(RuntimeError, match="passphrase"):
        m.get_okx_cex_credentials()


def test_get_injected_creds_passthrough():
    creds = {"api_key": "k", "secret_key": "s", "passphrase": "p"}
    assert m.get_okx_cex_credentials(creds=creds) == creds


def test_get_injected_strips_extra_fields():
    creds = {"sub_accounts": [_entry("x", "9", api_key="k", secret_key="s", passphrase="p", extra="z")]}
    got = m.get_okx_cex_credentials(creds=creds)
    assert got == {"api_key": "k", "secret_key": "s", "passphrase": "p"}
