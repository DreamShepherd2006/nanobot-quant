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
        for key in ("status", "login", "addresses", "balance", "history", "accounts"):
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
