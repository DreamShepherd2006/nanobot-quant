"""Unit tests for Gate CEX account management: gate_spec normalize/
denormalize, slot_map loading, main→sub transfer and /config/gate handlers.

Handlers are thin wrappers around gate_credentials; async handlers are
driven via asyncio.run() so tests stay plain-sync (no pytest-asyncio).
"""

import asyncio
import json
import time

import pytest

from nanobot_quant.gate_credentials import (
    fetch_all_balances,
    load_slot_map,
    sub_account_transfer,
)
from nanobot_quant.gate_handlers import (
    _TRANSFER_PENDING,
    gate_transfer,
    gate_transfer_confirm,
    register_gate_routes,
    sync_subaccounts,
)
from nanobot_quant.gate_spec import GATE_SPEC

CREDS = {
    "main": {"api_key": "k", "api_secret": "s", "uid": "15119093"},
    "slot_map": {"1": "gate_bot1", "2": "gate_bot2", "3": "gate_bot3", "4": "gate_bot4", "5": "gate_bot5"},
    "sub_accounts": {
        "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"},
        "gate_bot2": {"uid": "59175258", "api_key": "k2", "api_secret": "s2"},
        "gate_bot3": {"uid": "59175298", "api_key": "k3", "api_secret": "s3"},
        "gate_bot4": {"uid": "59175332", "api_key": "k4", "api_secret": "s4"},
        "gate_bot5": {"uid": "59175360", "api_key": "k5", "api_secret": "s5"},
    },
}

FLAT_FORM = {
    "api_key": "k", "api_secret": "s", "uid": "15119093",
    "max_sub_accounts": "10",
    "sub_0_name": "gate_bot1", "sub_0_uid": "59175220",
    "sub_1_name": "gate_bot2", "sub_1_uid": "59175258",
    "sub_2_name": "gate_bot3", "sub_2_uid": "59175298",
    "sub_3_name": "gate_bot4", "sub_3_uid": "59175332",
    "sub_4_name": "gate_bot5", "sub_4_uid": "59175360",
}


class TestGateSpec:
    def test_spec_registered(self):
        assert GATE_SPEC.name == "gate"
        assert GATE_SPEC.normalize is not None
        assert GATE_SPEC.denormalize is not None

    def test_normalize_full_form(self):
        out = GATE_SPEC.normalize(dict(FLAT_FORM))
        assert out["main"] == {"api_key": "k", "api_secret": "s", "uid": "15119093"}
        assert out["slot_map"]["1"] == "gate_bot1"
        assert out["slot_map"]["5"] == "gate_bot5"
        assert out["sub_accounts"]["gate_bot1"]["uid"] == "59175220"
        assert len(out["sub_accounts"]) == 5

    def test_normalize_drops_empty_subs(self):
        form = dict(FLAT_FORM)
        form["sub_0_name"] = ""
        form["sub_0_uid"] = ""
        form["sub_0_api_key"] = ""
        form["sub_0_api_secret"] = ""
        out = GATE_SPEC.normalize(form)
        assert "gate_bot1" not in out["sub_accounts"]
        assert len(out["sub_accounts"]) == 4

    def test_normalize_keeps_slot_map(self):
        # slot_map covers all configured subs (dynamic, not fixed at 5)
        out = GATE_SPEC.normalize(dict(FLAT_FORM))
        assert sorted(out["slot_map"]) == ["1", "2", "3", "4", "5"]

    def test_denormalize_roundtrip(self):
        nested = GATE_SPEC.normalize(dict(FLAT_FORM))
        flat = GATE_SPEC.denormalize(nested)
        for k, v in FLAT_FORM.items():
            assert flat.get(k) == v, k

    def test_denormalize_empty(self):
        assert GATE_SPEC.denormalize(None)["api_key"] == ""
        assert GATE_SPEC.denormalize({})["max_sub_accounts"] == "10"

    def test_normalize_keeps_stored_main_keys(self, monkeypatch):
        # Form only edits UIDs after initial setup — main key/secret must survive.
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: {"main": {"api_key": "stored-k", "api_secret": "stored-s"}},
        )
        form = dict(FLAT_FORM)
        form["api_key"] = ""
        form["api_secret"] = ""
        out = GATE_SPEC.normalize(form)
        assert out["main"]["api_key"] == "stored-k"
        assert out["main"]["api_secret"] == "stored-s"
        assert out["main"]["uid"] == "15119093"
        # 子账号行保留 uid；key/secret 空表单不覆盖旧值（增量保存）
        assert out["sub_accounts"]["gate_bot1"]["uid"] == "59175220"
        assert out["sub_accounts"]["gate_bot1"]["api_key"] == ""
        assert out["sub_accounts"]["gate_bot1"]["api_secret"] == ""

    def test_normalize_keeps_stored_sub_keys(self, monkeypatch):
        # 子账号 key/secret 表单留空 → 保留已存值（增量保存）
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: {"sub_accounts": {
                "gate_bot1": {"uid": "59175220", "api_key": "k1", "api_secret": "s1"}}},
        )
        form = dict(FLAT_FORM)
        form["sub_0_api_key"] = ""
        form["sub_0_api_secret"] = ""
        out = GATE_SPEC.normalize(form)
        assert out["sub_accounts"]["gate_bot1"]["api_key"] == "k1"
        assert out["sub_accounts"]["gate_bot1"]["api_secret"] == "s1"

    def test_normalize_drops_all_empty_sub_row(self, monkeypatch):
        # uid/key/secret 全空 → 该子账号行删除
        monkeypatch.setattr("nanobot_quant.gate_spec.read_credential", lambda name: None)
        form = dict(FLAT_FORM)
        form["sub_2_name"] = ""
        form["sub_2_uid"] = ""
        form["sub_2_api_key"] = ""
        form["sub_2_api_secret"] = ""
        out = GATE_SPEC.normalize(form)
        assert "gate_bot3" not in out["sub_accounts"]


class TestSlotMap:
    def test_default_slot_map(self, monkeypatch):
        creds = {k: v for k, v in CREDS.items() if k != "slot_map"}
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: creds)
        assert load_slot_map(creds)["1"] == "gate_bot1"
        assert load_slot_map(creds)["5"] == "gate_bot5"

    def test_persisted_slot_map(self):
        out = load_slot_map(CREDS)
        assert out["1"] == "gate_bot1"

    def test_slot_referencing_unknown_sub_dropped(self):
        creds = dict(CREDS)
        creds["sub_accounts"] = {"gate_bot1": CREDS["sub_accounts"]["gate_bot1"]}
        creds["slot_map"] = {**CREDS["slot_map"], "3": "ghost_bot"}
        out = load_slot_map(creds)
        # explicit slot_map entries referencing unconfigured subs are dropped
        # (normalizer rebuilds a complete map on next save)
        assert "3" not in out
        assert out["1"] == "gate_bot1"

    def test_missing_creds(self):
        assert load_slot_map({}) == {}


class TestSubAccountTransfer:
    def test_calls_sdk_transfer(self, monkeypatch):
        captured = {}

        def fake_transfer(**kw):
            captured.update(kw)
            return {"currency": "USDT", "sub_account": "59175220", "amount": "1.5"}

        monkeypatch.setattr("nanobot_quant.gate_sdk.transfer_to_sub", fake_transfer)
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: CREDS)

        out = sub_account_transfer("1.5", "gate_bot1")
        assert captured["currency"] == "USDT"
        assert captured["sub_uid"] == "59175220"
        assert captured["amount"] == "1.5"
        assert captured["direction"] == "to"  # main → sub
        assert captured["api_key"] == "k"
        assert out["sub_account"] == "59175220"

    def test_by_uid(self, monkeypatch):
        captured = {}

        def fake_transfer(**kw):
            captured.update(kw)
            return {}

        monkeypatch.setattr("nanobot_quant.gate_sdk.transfer_to_sub", fake_transfer)
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: CREDS)
        sub_account_transfer("0.5", "59175220")
        assert captured["sub_uid"] == "59175220"

    def test_missing_main_key(self, monkeypatch):
        creds = {"main": {"api_key": "", "api_secret": ""}, "sub_accounts": {}}
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: creds)
        with pytest.raises(RuntimeError, match="主账号 Key 未配置"):
            sub_account_transfer("1", "gate_bot1")

    def test_sub_without_uid(self, monkeypatch):
        creds = dict(CREDS)
        creds["sub_accounts"] = {"gate_bot1": {"api_key": "k1", "api_secret": "s1"}}
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: creds)
        with pytest.raises(RuntimeError, match="未配置 UID"):
            sub_account_transfer("1", "gate_bot1")

    def test_api_error_raises(self, monkeypatch):
        def fake_transfer(**kw):
            raise RuntimeError("transfer_to_sub failed: HTTP 400")

        monkeypatch.setattr("nanobot_quant.gate_sdk.transfer_to_sub", fake_transfer)
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: CREDS)
        with pytest.raises(RuntimeError, match="400"):
            sub_account_transfer("1", "gate_bot1")


class TestFetchAllBalances:
    def test_aggregates_main_and_subs(self, monkeypatch):
        def fake_spot(key, secret):
            return {"USDT": {"available": 10.0, "locked": 0.0}}

        def fake_list_sub(key, secret):
            return [
                {"uid": "59175220", "available": {"USDT": "5"}, "locking": {}},
                {"uid": "59175258", "available": {"USDT": "3"}, "locking": {"USDT": "1"}},
            ]

        monkeypatch.setattr("nanobot_quant.gate_credentials.fetch_spot_balances", fake_spot)
        monkeypatch.setattr("nanobot_quant.gate_sdk.sub_account_balances", fake_list_sub)
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: CREDS)
        out = fetch_all_balances()
        assert out["main"]["USDT"]["available"] == 10.0
        assert out["sub_accounts"][0]["uid"] == "59175220"
        assert out["sub_accounts"][0]["balances"]["USDT"]["available"] == 5.0
        assert out["sub_accounts"][1]["balances"]["USDT"]["locked"] == 1.0
        assert len(out["sub_accounts"]) == 2

    def test_sub_list_error_surfaces(self, monkeypatch):
        def fake_spot(key, secret):
            return {"USDT": {"available": 1.0, "locked": 0.0}}

        def fake_list_sub(key, secret):
            raise RuntimeError("HTTP 401")

        monkeypatch.setattr("nanobot_quant.gate_credentials.fetch_spot_balances", fake_spot)
        monkeypatch.setattr("nanobot_quant.gate_sdk.sub_account_balances", fake_list_sub)
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: CREDS)
        out = fetch_all_balances()
        assert out["main"]["USDT"]["available"] == 1.0
        assert out["sub_accounts"]["__error"] == "HTTP 401"

    def test_no_main_key(self, monkeypatch):
        creds = dict(CREDS)
        creds["main"] = {}
        monkeypatch.setattr("nanobot_quant.gate_credentials.load_gate_credentials", lambda: creds)
        out = fetch_all_balances()
        assert "主账号 Key 未配置" in out["main"]["__error"]


class _FakeGatekeeper:
    class _Platform:
        def is_commander(self, user) -> bool:
            return bool(user and user.get("commander"))

    _platform = _Platform()


class _FakeApp:
    def __init__(self):
        self.routes = []
        self.gatekeeper = _FakeGatekeeper()

    def get(self, path):
        def deco(fn):
            self.routes.append((path, ["GET"], fn))
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.routes.append((path, ["POST"], fn))
            return fn
        return deco


class _FakeRequest:
    def __init__(self, user=None, body=None):
        self.session = {"user": user} if user is not None else {}
        self._body = body or {}

    async def json(self):
        return self._body


def _commander():
    return {"commander": True, "name": "DreamShepherd2006"}


def _register(monkeypatch):
    """Register /config/gate routes; auth/td-lock live in the guard closures."""
    monkeypatch.setattr("nanobot_quant.exec_params.load_exec_params", lambda: {})
    app = _FakeApp()
    register_gate_routes(app, _FakeGatekeeper())
    return app


def _guarded(app, path):
    return next(fn for p, _, fn in app.routes if p == path)


class TestGateHandlers:
    def test_register_routes(self):
        app = _FakeApp()
        register_gate_routes(app, _FakeGatekeeper())
        paths = [r[0] for r in app.routes]
        assert "/config/gate" in paths
        assert "/config/gate/data" in paths
        assert "/config/gate/transfer" in paths
        assert "/config/gate/transfer/confirm" in paths

    def test_transfer_requires_auth(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        resp = asyncio.run(handler(_FakeRequest(user=None, body={"sub": "gate_bot1", "amount": "1"})))
        assert resp.status_code == 401

    def test_transfer_requires_sub_and_amount(self, monkeypatch):
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={})))
        assert resp.status_code == 400

    def test_transfer_rejects_bad_amount(self, monkeypatch):
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={"sub": "gate_bot1", "amount": "abc"})))
        assert resp.status_code == 400

    def test_transfer_rejects_non_sub_target(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={"sub": "main", "amount": "1"})))
        assert resp.status_code == 400
        assert "子账号" in resp.body.decode()

    def test_transfer_creates_pending(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        _TRANSFER_PENDING.clear()
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={"sub": "gate_bot1", "amount": "1.5"})))
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["ttl"] == 30
        assert data["tx_id"]
        assert data["tx_id"] in _TRANSFER_PENDING

    def test_confirm_requires_txid(self, monkeypatch):
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer/confirm")
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={})))
        assert resp.status_code == 400

    def test_confirm_expired(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer/confirm")
        _TRANSFER_PENDING["deadbeef"] = {
            "sub": "gate_bot1", "amount": "1", "currency": "USDT",
            "expires_at": time.time() - 5,
        }
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={"tx_id": "deadbeef"})))
        assert resp.status_code == 400
        assert "过期" in resp.body.decode()

    def test_confirm_executes(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        app = _register(monkeypatch)
        confirm = _guarded(app, "/config/gate/transfer/confirm")
        transfer = _guarded(app, "/config/gate/transfer")
        called = {}

        def fake_transfer(amount, target_sub, currency):
            called["amount"] = amount
            called["sub"] = target_sub
            called["currency"] = currency
            return {"currency": currency, "sub_account": "59175220", "amount": amount}

        monkeypatch.setattr("nanobot_quant.gate_handlers.sub_account_transfer", fake_transfer)
        _TRANSFER_PENDING.clear()
        tx = asyncio.run(transfer(_FakeRequest(user=_commander(), body={"sub": "gate_bot1", "amount": "2.25"})))
        tx_id = json.loads(tx.body.decode())["tx_id"]
        resp = asyncio.run(confirm(_FakeRequest(user=_commander(), body={"tx_id": tx_id})))
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert called["sub"] == "gate_bot1"
        assert called["amount"] == "2.25"
        assert called["currency"] == "USDT"  # default when omitted
        assert "USDT" in data["summary"]

    def test_transfer_custom_currency(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        handler = _guarded(_register(monkeypatch), "/config/gate/transfer")
        _TRANSFER_PENDING.clear()
        resp = asyncio.run(handler(_FakeRequest(user=_commander(), body={"sub": "gate_bot1", "amount": "0.01", "currency": "btc"})))
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["summary"] == "主账号 → gate_bot1 0.01 BTC"
        tx = _TRANSFER_PENDING[data["tx_id"]]
        assert tx["currency"] == "BTC"  # normalized to upper case
        assert tx["amount"] == "0.01"

    def test_confirm_failure_returns_502(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: CREDS)
        app = _register(monkeypatch)
        confirm = _guarded(app, "/config/gate/transfer/confirm")
        transfer = _guarded(app, "/config/gate/transfer")

        def boom(amount, target_sub, currency):
            raise RuntimeError("HTTP 400 INVALID_PARAM")

        monkeypatch.setattr("nanobot_quant.gate_handlers.sub_account_transfer", boom)
        _TRANSFER_PENDING.clear()
        tx = asyncio.run(transfer(_FakeRequest(user=_commander(), body={"sub": "gate_bot1", "amount": "1"})))
        tx_id = json.loads(tx.body.decode())["tx_id"]
        resp = asyncio.run(confirm(_FakeRequest(user=_commander(), body={"tx_id": tx_id})))
        assert resp.status_code == 502
        assert "划转失败" in resp.body.decode()


class TestGateSpecDynamic:
    """2026-08-22: dynamic sub-account rows + rename migration + limit."""

    def test_normalize_adds_new_row_and_slot(self):
        form = dict(FLAT_FORM)
        form["sub_5_name"] = "gate_bot6"
        form["sub_5_uid"] = "59175388"
        out = GATE_SPEC.normalize(form)
        assert out["sub_accounts"]["gate_bot6"]["uid"] == "59175388"
        assert out["slot_map"]["6"] == "gate_bot6"
        assert len(out["sub_accounts"]) == 6

    def test_normalize_rename_migrates_slot_map_and_scene_pool(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: {
                "main": {"api_key": "k"},
                "slot_map": {"1": "gate_bot1", "2": "gate_bot2"},
                "sub_accounts": {
                    "gate_bot1": {"uid": "59175220", "api_key": "k1"},
                    "gate_bot2": {"uid": "59175258"},
                },
            },
        )
        ep_path = tmp_path / "exec_params.json"
        ep_path.write_text(json.dumps({"scenes": {"high": {"sub_accounts": ["gate_bot1", "gate_bot2"]}}}))
        monkeypatch.setattr("nanobot_quant.exec_params.exec_params_path", lambda: ep_path)
        form = {
            "api_key": "k",
            "sub_0_name": "gate_bot6", "sub_0_uid": "59175220",
            "sub_1_name": "gate_bot2", "sub_1_uid": "59175258",
        }
        out = GATE_SPEC.normalize(form)
        assert "gate_bot6" in out["sub_accounts"]
        assert "gate_bot1" not in out["sub_accounts"]
        assert out["slot_map"] == {"1": "gate_bot6", "2": "gate_bot2"}
        params = json.loads(ep_path.read_text())
        assert params["scenes"]["high"]["sub_accounts"] == ["gate_bot6", "gate_bot2"]

    def test_normalize_uid_match_keeps_old_name_when_name_blank(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_spec.read_credential",
            lambda name: {"sub_accounts": {"gate_bot1": {"uid": "59175220", "api_key": "k1"}}},
        )
        form = {"sub_0_uid": "59175220", "sub_0_api_key": ""}  # name 留空
        out = GATE_SPEC.normalize(form)
        assert out["sub_accounts"]["gate_bot1"]["api_key"] == "k1"

    def test_normalize_new_uid_without_name_auto_names(self):
        form = {"sub_0_uid": "59175388", "sub_0_api_key": "x"}
        out = GATE_SPEC.normalize(form)
        assert out["sub_accounts"]["gate_bot1"]["uid"] == "59175388"

    def test_normalize_duplicate_name_raises(self):
        form = {"sub_0_name": "dup", "sub_0_uid": "1", "sub_1_name": "dup", "sub_1_uid": "2"}
        with pytest.raises(ValueError):
            GATE_SPEC.normalize(form)

    def test_normalize_max_subs_parsing(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_spec.read_credential", lambda name: None)
        assert GATE_SPEC.normalize({"max_sub_accounts": "0"})["max_sub_accounts"] == 10
        assert GATE_SPEC.normalize({"max_sub_accounts": "abc"})["max_sub_accounts"] == 10
        assert GATE_SPEC.normalize({"max_sub_accounts": "20"})["max_sub_accounts"] == 20

    def test_fields_for_only_configured_rows(self):
        flat = {"max_sub_accounts": "10", "sub_0_name": "gate_bot1", "sub_0_uid": "59175220"}
        names = [f.name for f in GATE_SPEC.fields_for(flat)]
        assert "sub_0_name" in names
        assert "sub_1_name" not in names

    def test_form_extra_contains_toolbar(self):
        html = GATE_SPEC.form_extra({"max_sub_accounts": "10"})
        assert "syncSubs" in html
        assert "从 Gate 同步子账号" in html


class TestSyncSubaccounts:
    def test_sync_returns_added(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_handlers.load_gate_credentials",
            lambda: {
                "main": {"api_key": "k", "api_secret": "s"},
                "sub_accounts": {"gate_bot1": {"uid": "59175220"}},
            },
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_sdk.sub_account_balances",
            lambda *a, **k: [{"uid": "59175220"}, {"uid": "59175360"}, {"uid": "59175388"}],
        )
        resp = asyncio.run(sync_subaccounts(_FakeRequest()))
        assert resp.status_code == 200
        data = json.loads(resp.body.decode())
        assert data["ok"] is True
        assert data["added"] == [
            {"name": "gate_bot2", "uid": "59175360"},
            {"name": "gate_bot3", "uid": "59175388"},
        ]
        assert data["total"] == 3

    def test_sync_no_new(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_handlers.load_gate_credentials",
            lambda: {"main": {"api_key": "k", "api_secret": "s"},
                     "sub_accounts": {"gate_bot1": {"uid": "59175220"}}},
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_sdk.sub_account_balances", lambda *a, **k: [{"uid": "59175220"}]
        )
        resp = asyncio.run(sync_subaccounts(_FakeRequest()))
        assert json.loads(resp.body.decode())["added"] == []

    def test_sync_missing_main_key(self, monkeypatch):
        monkeypatch.setattr("nanobot_quant.gate_handlers.load_gate_credentials", lambda: {"main": {}})
        resp = asyncio.run(sync_subaccounts(_FakeRequest()))
        assert resp.status_code == 400
        assert "主账号" in resp.body.decode()

    def test_sync_exceeds_limit(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_handlers.load_gate_credentials",
            lambda: {
                "main": {"api_key": "k", "api_secret": "s"},
                "max_sub_accounts": 5,
                "sub_accounts": {f"gate_bot{i}": {"uid": f"59{i}"} for i in range(1, 6)},
            },
        )
        monkeypatch.setattr(
            "nanobot_quant.gate_sdk.sub_account_balances",
            lambda *a, **k: [{"uid": f"59{i}"} for i in range(1, 6)] + [{"uid": "599999"}],
        )
        resp = asyncio.run(sync_subaccounts(_FakeRequest()))
        assert resp.status_code == 400
        assert "上限" in resp.body.decode()

    def test_sync_api_error(self, monkeypatch):
        monkeypatch.setattr(
            "nanobot_quant.gate_handlers.load_gate_credentials",
            lambda: {"main": {"api_key": "k", "api_secret": "s"}, "sub_accounts": {}},
        )

        def boom(*a, **k):
            raise RuntimeError("401")

        monkeypatch.setattr("nanobot_quant.gate_sdk.sub_account_balances", boom)
        resp = asyncio.run(sync_subaccounts(_FakeRequest()))
        assert resp.status_code == 502
        assert "查询失败" in resp.body.decode()

    def test_register_includes_sync(self):
        app = _FakeApp()
        register_gate_routes(app, _FakeGatekeeper())
        paths = [r[0] for r in app.routes]
        assert "/config/gate/sync-subaccounts" in paths
