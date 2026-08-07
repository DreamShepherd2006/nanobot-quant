"""Unit tests for wallet WebUI handlers (no CLI binary / no gatekeeper needed).

The handlers are thin wrappers around tools_wallet functions; here we
verify routing registration, auth guards and CLI aggregation logic using
mocked tool functions. Async handlers are driven via asyncio.run() so the
tests stay plain-sync (no pytest-asyncio / anyio plugin dependency).
"""

import asyncio
import json

from nanobot_quant.wallet_handlers import (
    _call,
    _merge_tracked_tokens,
    register_wallet_routes,
)


class _FakeGatekeeper:
    """Minimal gatekeeper stand-in exposing platform auth helpers."""

    class _Platform:
        data_root = "/data/legion"

        def is_commander(self, user) -> bool:
            return bool(user and user.get("commander"))

    _platform = _Platform()

    def _log(self, msg):
        pass


class _FakeApp:
    def __init__(self):
        self.state = type("State", (), {"gatekeeper": _FakeGatekeeper()})()
        self.routes = []

    def add_route(self, path, handler, methods):
        self.routes.append((path, methods))


class _FakeRequest:
    """Request with session + app, optional body."""

    def __init__(self, user=None, body=None, app=None):
        self.session = {"user": user} if user is not None else {}
        self.app = app or _FakeApp()
        self._body = body or {}

    async def json(self):
        return self._body


def _make_handlers(gatekeeper=None):
    """Register routes on a capturing app; return (app, {path: handler})."""
    handlers = {}
    gk = gatekeeper or _FakeGatekeeper()

    class _CapturingApp(_FakeApp):
        def add_route(self, path, handler, methods):
            handlers[path] = handler
            super().add_route(path, handler, methods)

    app = _CapturingApp()
    register_wallet_routes(app, gk)
    return app, handlers


def _run(coro):
    return asyncio.run(coro)


class TestRegister:
    def test_routes_registered(self):
        app, handlers = _make_handlers()
        assert set(handlers) == {
            "/config/wallet",
            "/config/wallet/data",
            "/config/wallet/login",
            "/config/wallet/add",
            "/config/wallet/switch",
            "/config/wallet/send",
            "/config/wallet/send/confirm",
            "/config/wallet/address-book/add",
            "/config/wallet/address-book/remove",
            "/config/wallet/address-book/limit",
        }


class TestAuthGuard:
    def test_page_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet"](_FakeRequest(user=None)))
        assert resp.status_code == 307 or resp.status_code == 302  # RedirectResponse → "/"

    def test_page_requires_commander(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet"](_FakeRequest(user={"name": "bob", "commander": False})))
        assert resp.status_code == 403

    def test_data_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/data"](_FakeRequest(user=None)))
        assert resp.status_code == 401

    def test_add_requires_commander(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/add"](_FakeRequest(user={"name": "bob", "commander": False})))
        assert resp.status_code == 403

    def test_switch_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/switch"](_FakeRequest(user=None)))
        assert resp.status_code == 401


class TestDataAggregation:
    def test_data_returns_all_sections(self, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": {"probe": "ok"}}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True})
        resp = _run(h["/config/wallet/data"](req))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        for key in ("status", "login", "addresses", "balance", "history", "accounts",
                    "chains", "address_book"):
            assert key in data


class TestOperations:
    def test_switch_missing_account_id(self, monkeypatch):
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={})
        resp = _run(h["/config/wallet/switch"](req))
        assert resp.status_code == 400

    def test_switch_calls_wallet_switch(self, monkeypatch):
        captured = {}

        async def _fake_call(fn, *args, **kwargs):
            captured["fn"] = fn.__name__
            captured["args"] = args
            return {"status": "ok", "data": {"activeAccountId": "acct-2"}}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"account_id": "acct-2"})
        resp = _run(h["/config/wallet/switch"](req))
        assert resp.status_code == 200
        assert captured["fn"] == "wallet_switch"
        assert captured["args"] == ("acct-2",)

    def test_login_init_returns_url(self, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"login_url": "https://web3.okx.com/login?x=1",
                    "auth_session_id": "sid-1"}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={"phase": "init"})
        resp = _run(h["/config/wallet/login"](req))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert data["login_url"].startswith("https://")

    def test_add_returns_ok(self, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": {"accountId": "acct-3"}}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True})
        resp = _run(h["/config/wallet/add"](req))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True


class TestCallHelper:
    def test_call_timeout(self, monkeypatch):
        async def _slow(*a, **k):
            await asyncio.sleep(10)
            return {"status": "ok"}

        async def _fast_timeout(awaitable, timeout):
            raise asyncio.TimeoutError()

        monkeypatch.setattr("nanobot_quant.wallet_handlers.asyncio.wait_for", _fast_timeout)
        result = _run(_call(_slow, timeout=1))
        assert result["status"] == "error"
        assert "timed out" in result["error"]

    def test_call_exception_captured(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("cli exploded")

        result = _run(_call(_boom, timeout=5))
        assert result["status"] == "error"
        assert "cli exploded" in result["error"]


# ── Transfer (two-step backend confirmation) ────────────────────────────

_SOL_ADDR = "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq"


def _gk_with_book(tmp_path, addresses=None, max_amount=None):
    """Gatekeeper with data_root pinned to tmp_path and an optional pre-seeded book."""
    gk = _FakeGatekeeper()
    gk._platform.data_root = str(tmp_path)
    if addresses is not None:
        path = tmp_path / "credentials" / "address_book.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"addresses": addresses, "max_amount": max_amount}))
    return gk


class TestTransfer:
    def test_send_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/send"](_FakeRequest(user=None)))
        assert resp.status_code == 401

    def test_send_requires_commander(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/send"](_FakeRequest(user={"name": "bob", "commander": False})))
        assert resp.status_code == 403

    def test_confirm_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/send/confirm"](_FakeRequest(user=None)))
        assert resp.status_code == 401

    def test_send_missing_fields(self):
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 400

    def test_send_invalid_address_format(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"chain": "solana", "to_address": "0x1234", "amount": "1"})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 400
        assert "格式无效" in json.loads(resp.body)["error"]

    def test_send_address_not_in_book(self, tmp_path, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": []}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path, addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"chain": "solana", "to_address": "6HWbojG7Kb6vRHsWbUX5858yVHxQqcWTxv8k8nHNyN1s", "amount": "1"})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 400
        assert "地址簿" in json.loads(resp.body)["error"]

    def test_send_over_limit(self, tmp_path, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": []}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}],
                           max_amount=10.0)
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"chain": "solana", "to_address": _SOL_ADDR, "amount": "100"})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 400
        assert "限额" in json.loads(resp.body)["error"]

    def test_send_unsupported_chain(self, tmp_path, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": [{"chainName": "solana"}]}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        evm = "0xe06e734a46f6d7ea98302a68ca50dd7dc26378d3"
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "EVM", "chain": "eth", "address": evm}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"chain": "eth", "to_address": evm, "amount": "1"})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 400
        assert "不支持的链" in json.loads(resp.body)["error"]

    def test_send_preview_returns_tx_id(self, tmp_path, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": [{"chainName": "solana"}]}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"chain": "solana", "to_address": _SOL_ADDR, "amount": "1.5"})
        resp = _run(h["/config/wallet/send"](req))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert len(data["tx_id"]) == 32
        assert data["preview"]["amount"] == "1.5"
        assert data["preview"]["token"] is None

    def test_send_confirm_executes(self, tmp_path, monkeypatch):
        captured = {}

        async def _fake_call(fn, *args, **kwargs):
            if fn.__name__ == "wallet_chains":
                return {"status": "ok", "data": [{"chainName": "solana"}]}
            captured["fn"] = fn.__name__
            captured["args"] = args
            return {"status": "ok", "data": {"txHash": "abc123"}}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        user = {"name": "commander", "commander": True}
        req = _FakeRequest(user=user,
                           body={"chain": "solana", "to_address": _SOL_ADDR, "amount": "1.5"})
        resp = _run(h["/config/wallet/send"](req))
        tx_id = json.loads(resp.body)["tx_id"]
        resp2 = _run(h["/config/wallet/send/confirm"](_FakeRequest(user=user, body={"tx_id": tx_id})))
        assert resp2.status_code == 200
        data2 = json.loads(resp2.body)
        assert data2["ok"] is True
        assert captured["fn"] == "wallet_send"
        assert captured["args"][0] == "solana"
        assert captured["args"][1] == _SOL_ADDR
        assert captured["args"][2] == "1.5"
        assert captured["args"][3] == ""

    def test_send_confirm_unknown_tx(self):
        _, h = _make_handlers()
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={"tx_id": "nope"})
        resp = _run(h["/config/wallet/send/confirm"](req))
        assert resp.status_code == 400

    def test_send_confirm_expired(self, tmp_path, monkeypatch):
        class _FakeTime:
            def __init__(self):
                self.t = 1000.0

            def time(self):
                return self.t

        ft = _FakeTime()
        monkeypatch.setattr("nanobot_quant.wallet_handlers.time", ft)

        async def _fake_call(fn, *args, **kwargs):
            return {"status": "ok", "data": [{"chainName": "solana"}]}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        user = {"name": "commander", "commander": True}
        req = _FakeRequest(user=user,
                           body={"chain": "solana", "to_address": _SOL_ADDR, "amount": "1.5"})
        resp = _run(h["/config/wallet/send"](req))
        tx_id = json.loads(resp.body)["tx_id"]
        ft.t = 2000.0  # 60s later — beyond the 30s TTL
        resp2 = _run(h["/config/wallet/send/confirm"](_FakeRequest(user=user, body={"tx_id": tx_id})))
        assert resp2.status_code == 400
        assert "过期" in json.loads(resp2.body)["error"]

    def test_send_confirm_single_use(self, tmp_path, monkeypatch):
        async def _fake_call(fn, *args, **kwargs):
            if fn.__name__ == "wallet_chains":
                return {"status": "ok", "data": [{"chainName": "solana"}]}
            return {"status": "ok", "data": {"txHash": "abc"}}

        monkeypatch.setattr("nanobot_quant.wallet_handlers._call", _fake_call)
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        user = {"name": "commander", "commander": True}
        req = _FakeRequest(user=user,
                           body={"chain": "solana", "to_address": _SOL_ADDR, "amount": "1"})
        tx_id = json.loads(_run(h["/config/wallet/send"](req)).body)["tx_id"]
        assert _run(h["/config/wallet/send/confirm"](_FakeRequest(user=user, body={"tx_id": tx_id}))).status_code == 200
        resp2 = _run(h["/config/wallet/send/confirm"](_FakeRequest(user=user, body={"tx_id": tx_id})))
        assert resp2.status_code == 400


# ── Address book ────────────────────────────────────────────────────────


class TestAddressBook:
    def test_add_valid(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"name": "个人钱包", "chain": "solana", "address": _SOL_ADDR})
        resp = _run(h["/config/wallet/address-book/add"](req))
        assert resp.status_code == 200
        data = json.loads(resp.body)
        assert data["ok"] is True
        assert len(data["address_book"]["addresses"]) == 1
        saved = json.loads((tmp_path / "credentials" / "address_book.json").read_text())
        assert saved["addresses"][0]["name"] == "个人钱包"
        assert saved["addresses"][0]["chain"] == "solana"
        assert saved["addresses"][0]["address"] == _SOL_ADDR

    def test_add_requires_commander(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/address-book/add"](_FakeRequest(user={"name": "bob", "commander": False})))
        assert resp.status_code == 403

    def test_add_duplicate(self, tmp_path):
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"name": "重复", "chain": "solana", "address": _SOL_ADDR})
        resp = _run(h["/config/wallet/address-book/add"](req))
        assert resp.status_code == 400
        assert "已存在" in json.loads(resp.body)["error"]

    def test_add_invalid_address(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"name": "坏地址", "chain": "solana", "address": "0x1234"})
        resp = _run(h["/config/wallet/address-book/add"](req))
        assert resp.status_code == 400
        assert "格式无效" in json.loads(resp.body)["error"]

    def test_add_evm_address(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True},
                           body={"name": "EVM", "chain": "eth", "address": "0xe06e734a46f6d7ea98302a68ca50dd7dc26378d3"})
        resp = _run(h["/config/wallet/address-book/add"](req))
        assert resp.status_code == 200

    def test_remove_valid(self, tmp_path):
        gk = _gk_with_book(tmp_path,
                           addresses=[{"id": "e1", "name": "个人钱包", "chain": "solana", "address": _SOL_ADDR}])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={"id": "e1"})
        resp = _run(h["/config/wallet/address-book/remove"](req))
        assert resp.status_code == 200
        assert json.loads(resp.body)["address_book"]["addresses"] == []

    def test_remove_unknown(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={"id": "ghost"})
        resp = _run(h["/config/wallet/address-book/remove"](req))
        assert resp.status_code == 404

    def test_limit_set_and_clear(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        user = {"name": "commander", "commander": True}
        resp = _run(h["/config/wallet/address-book/limit"](_FakeRequest(user=user, body={"max_amount": 500})))
        assert resp.status_code == 200
        assert json.loads(resp.body)["address_book"]["max_amount"] == 500
        resp2 = _run(h["/config/wallet/address-book/limit"](_FakeRequest(user=user, body={"max_amount": None})))
        assert resp.status_code == 200
        assert json.loads(resp2.body)["address_book"]["max_amount"] is None

    def test_limit_invalid(self, tmp_path):
        gk = _gk_with_book(tmp_path, addresses=[])
        _, h = _make_handlers(gk)
        req = _FakeRequest(user={"name": "commander", "commander": True}, body={"max_amount": -5})
        resp = _run(h["/config/wallet/address-book/limit"](req))
        assert resp.status_code == 400

    def test_limit_requires_login(self):
        _, h = _make_handlers()
        resp = _run(h["/config/wallet/address-book/limit"](_FakeRequest(user=None)))
        assert resp.status_code == 401


# ── _merge_tracked_tokens ─────────────────────────────────────────


class TestMergeTrackedTokens:
    def _bal(self, assets=None):
        return {"status": "ok", "data": {"assets": assets if assets is not None else []}}

    def test_error_result_passthrough(self):
        res = {"status": "error", "error": "boom"}
        assert _merge_tracked_tokens(res, [{"symbol": "RENDER"}]) is res

    def test_non_dict_data_passthrough(self):
        res = {"status": "ok", "data": []}
        assert _merge_tracked_tokens(res, [{"symbol": "RENDER"}]) is res

    def test_appends_tracked_zero_balance(self):
        res = self._bal([{"symbol": "SOL", "amount": "1.2"}])
        out = _merge_tracked_tokens(res, [{"symbol": "RENDER", "chain": "solana",
                                           "address": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof"}])
        assets = out["data"]["assets"]
        assert [a["symbol"] for a in assets] == ["SOL", "RENDER"]
        assert assets[1]["amount"] == "0"
        assert assets[1]["tracked"] is True
        assert assets[1]["chain"] == "solana"
        assert assets[1]["address"] == "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof"

    def test_wallet_address_resolved_from_addr_map(self):
        res = self._bal([{"symbol": "SOL", "amount": "1.2"}])
        addr_map = {"sol": "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq",
                    "xlayer_test": "0xe06e734a46f6d7ea98302a68ca50dd7dc26378d3"}
        out = _merge_tracked_tokens(res, [
            {"symbol": "RENDER", "chain": "solana", "address": "rndrizK..."},
            {"symbol": "CRCLX", "chain": "xlayer", "address": "0x4ae46a..."},
        ], addr_map=addr_map)
        by_sym = {a["symbol"]: a for a in out["data"]["assets"]}
        assert by_sym["RENDER"]["wallet_address"] == "E71V4QebmxDoQrDUAvRZun5xt879trqyxH2TeoaDLeQq"
        assert by_sym["CRCLX"]["wallet_address"] == "0xe06e734a46f6d7ea98302a68ca50dd7dc26378d3"

    def test_wallet_address_unknown_chain_empty(self):
        res = self._bal([{"symbol": "SOL", "amount": "1.2"}])
        out = _merge_tracked_tokens(
            res, [{"symbol": "FOO", "chain": "unknownchain", "address": "abc"}],
            addr_map={"sol": "E71V4Qe..."},
        )
        assert out["data"]["assets"][1]["wallet_address"] == ""

    def test_no_addr_map_no_wallet_address(self):
        res = self._bal([{"symbol": "SOL", "amount": "1.2"}])
        out = _merge_tracked_tokens(res, [{"symbol": "RENDER", "chain": "solana"}])
        assert "wallet_address" not in out["data"]["assets"][1]

    def test_existing_symbol_not_duplicated(self):
        res = self._bal([{"symbol": "render", "amount": "3.5"}])  # case-insensitive match
        out = _merge_tracked_tokens(res, [{"symbol": "RENDER", "chain": "solana"}])
        assets = out["data"]["assets"]
        assert len(assets) == 1
        assert "tracked" not in assets[0]

    def test_multiple_tokens_and_case_normalization(self):
        res = self._bal([{"token": "usdc", "amount": "5"}])
        out = _merge_tracked_tokens(res, [
            {"symbol": "USDC", "chain": "solana"},
            {"symbol": "crclx", "chain": "xlayer"},
        ])
        assets = out["data"]["assets"]
        assert [a.get("symbol") or a.get("token") for a in assets] == ["usdc", "CRCLX"]
        assert assets[1]["tracked"] is True

    def test_empty_token_list_noop(self):
        res = self._bal([{"symbol": "SOL", "amount": "1"}])
        out = _merge_tracked_tokens(res, [])
        assert out["data"]["assets"] == [{"symbol": "SOL", "amount": "1"}]

    def test_balances_key_used_when_no_assets(self):
        res = {"status": "ok", "data": {"balances": [{"symbol": "SOL", "amount": "2"}]}}
        out = _merge_tracked_tokens(res, [{"symbol": "RENDER", "chain": "solana"}])
        assert [a["symbol"] for a in out["data"]["assets"]] == ["SOL", "RENDER"]
