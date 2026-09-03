"""Tests for OKX CEX credential spec form logic (nanobot_quant.okx_cex_spec)."""

import pytest

from nanobot_quant import okx_cex_spec as m
from nanobot_quant.credential_registry import discover

CREDS = {"api_key": "api-key-x", "secret_key": "test-secret", "passphrase": "pp"}


def _form_row(i, name="bot1", uid="881574754615066858"):
    return {
        f"sub_{i}_name": name,
        f"sub_{i}_uid": uid,
        f"sub_{i}_api_key": "k",
        f"sub_{i}_secret_key": "s",
        f"sub_{i}_passphrase": "p",
    }


# ── registry / shape ─────────────────────────────────────────────


def test_spec_registered():
    specs = discover()
    assert "okx_cex" in specs
    assert specs["okx_cex"].normalize is not None
    assert specs["okx_cex"].fields_for is not None


def test_stored_shape_defaults():
    assert m.normalize_stored({}) == {
        "max_sub_accounts": 10,
        "sub_accounts": [],
    }


def test_stored_shape_migrates_flat():
    out = m.normalize_stored({"api_key": "k", "secret_key": "s", "passphrase": "p"})
    assert len(out["sub_accounts"]) == 1
    assert out["sub_accounts"][0]["passphrase"] == "p"


# ── normalize (flat form → stored) ───────────────────────────────


def test_normalize_two_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(
        m, "read_credential", lambda name: {}  # no stored history
    )
    form = {m._MAX_KEY: "5"}
    form.update(_form_row(0, "bot1", "111"))
    form.update(_form_row(1, "bot2", "222"))
    out = m._normalize_okx_cex_form(form)
    assert out["max_sub_accounts"] == 5
    assert [s["name"] for s in out["sub_accounts"]] == ["bot1", "bot2"]
    assert out["sub_accounts"][0]["secret_key"] == "s"


def test_normalize_empty_secrets_fall_back_to_stored(monkeypatch):
    monkeypatch.setattr(
        m, "read_credential",
        lambda name: {"sub_accounts": [{
            "name": "bot1", "uid": "111",
            "api_key": "old-k", "secret_key": "old-s", "passphrase": "old-p",
        }]},
    )
    form = {m._MAX_KEY: "5"}
    form.update(_form_row(0, "bot1", "111"))
    form["sub_0_api_key"] = ""
    form["sub_0_secret_key"] = ""
    form["sub_0_passphrase"] = ""
    out = m._normalize_okx_cex_form(form)
    e = out["sub_accounts"][0]
    assert e["api_key"] == "old-k"
    assert e["secret_key"] == "old-s"
    assert e["passphrase"] == "old-p"


def test_normalize_empty_row_dropped(monkeypatch):
    monkeypatch.setattr(m, "read_credential", lambda name: {})
    form = {m._MAX_KEY: "5"}
    form.update(_form_row(0, "bot1", "111"))
    form["sub_1_name"] = ""
    form["sub_1_uid"] = ""
    form["sub_1_api_key"] = ""
    form["sub_1_secret_key"] = ""
    form["sub_1_passphrase"] = ""
    out = m._normalize_okx_cex_form(form)
    assert len(out["sub_accounts"]) == 1


def test_normalize_cleared_name_keeps_old_name(monkeypatch):
    monkeypatch.setattr(
        m, "read_credential",
        lambda name: {"sub_accounts": [{
            "name": "bot1", "uid": "111", "api_key": "k", "secret_key": "s",
            "passphrase": "p",
        }]},
    )
    form = {m._MAX_KEY: "5"}
    form.update(_form_row(0, "", "111"))
    out = m._normalize_okx_cex_form(form)
    assert out["sub_accounts"][0]["name"] == "bot1"


def test_normalize_new_row_without_name_gets_default(monkeypatch):
    monkeypatch.setattr(m, "read_credential", lambda name: {})
    form = {m._MAX_KEY: "5"}
    form.update(_form_row(0, "", "999"))
    out = m._normalize_okx_cex_form(form)
    assert out["sub_accounts"][0]["name"] == "okx_sub1"


# ── denormalize (stored → flat form) ─────────────────────────────


def test_denormalize_never_emits_secrets():
    flat = m._denormalize_okx_cex_form({
        "max_sub_accounts": 5,
        "sub_accounts": [{
            "name": "bot1", "uid": "111",
            "api_key": "k", "secret_key": "s", "passphrase": "p",
        }],
    })
    assert flat["sub_0_name"] == "bot1"
    assert flat["sub_0_uid"] == "111"
    assert flat["sub_0_api_key"] == ""
    assert flat["sub_0_secret_key"] == ""
    assert flat["sub_0_passphrase"] == ""


# ── fields_for ───────────────────────────────────────────────────


def test_fields_for_emits_rows_until_first_empty():
    flat = {"max_sub_accounts": "5",
            "sub_0_name": "bot1", "sub_0_uid": "111",
            "sub_1_name": "bot2", "sub_1_uid": "222"}
    fields = m._fields_for(flat)
    names = [f.name for f in fields]
    assert "sub_0_uid" in names
    assert "sub_1_passphrase" in names
    assert "sub_2_uid" not in names  # stops after first empty row


def test_fields_for_row_includes_all_five_fields():
    flat = {"max_sub_accounts": "5", "sub_0_name": "bot1", "sub_0_uid": "111"}
    fields = m._fields_for(flat)
    assert [f.name for f in fields] == [
        "sub_0_name", "sub_0_uid", "sub_0_api_key",
        "sub_0_secret_key", "sub_0_passphrase",
    ]
