"""Backtest WebUI page handlers (async body parse + driver dispatch)."""

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from nanobot_quant import backtest_handlers as bh


class _FakePlatform:
    def is_commander(self, user):
        return True


class _FakeGatekeeper:
    def __init__(self):
        self._platform = _FakePlatform()
        self._log_calls = []

    def _log(self, msg):
        self._log_calls.append(msg)


class _FakeRequest:
    def __init__(self, payload, raises=False):
        self._payload = payload
        self._raises = raises
        self.session = {"user": "DreamShepherd2006"}

    async def json(self):
        if self._raises:
            raise ValueError("no body")
        return self._payload


# ── _body async 解析（回归：曾同步调用 request.json() 导致 400） ──────

def test_body_parses_json_dict():
    req = _FakeRequest({"scene": "mid", "symbols": ["SOL"]})
    assert asyncio.run(bh._body(req)) == {"scene": "mid", "symbols": ["SOL"]}


def test_body_returns_none_on_missing_body():
    assert asyncio.run(bh._body(_FakeRequest(None, raises=True))) is None
    assert asyncio.run(bh._body(_FakeRequest(["not", "dict"]))) is None


# ── 页面 + start 端点 ───────────────────────────────────────────────

@pytest.fixture()
def client(monkeypatch):
    gk = _FakeGatekeeper()
    app = Starlette()
    bh.register_backtest_routes(app, gk)
    client = TestClient(app, raise_server_exceptions=False)

    # 免登录 + 固定数据源
    monkeypatch.setattr(bh, "_authorized", lambda req, gk: (None, True))
    monkeypatch.setattr(
        bh,
        "_scenes",
        lambda: {
            "mid": {
                "enabled": False,
                "sleeptime": "15m",
                "symbols": ["SOL"],
                "batches": 2,
                "sub_accounts": [],
            }
        },
    )
    monkeypatch.setattr(bh, "_symbol_candidates", lambda: ["SOL", "CRCLX"])
    return client, gk


def test_page_renders(client):
    client, _ = client
    r = client.get("/config/backtest")
    assert r.status_code == 200
    assert "📈 回测" in r.text
    assert "SOL" in r.text and "CRCLX" in r.text


def test_start_requires_symbols(client):
    client, _ = client
    r = client.post("/config/backtest/start", json={"scene": "mid", "symbols": []})
    assert r.status_code == 400
    assert "至少选择一个标的" in r.json()["error"]


def test_start_dispatch_driver_engine(client, monkeypatch):
    client, gk = client
    started = {}

    def _fake_run_backtest(**kw):
        started.update(kw)
        return {"status": "started", "run_id": "test-run-1", "engine": kw["engine"]}

    monkeypatch.setattr(
        "nanobot_quant.tools.tools_backtest.run_backtest", _fake_run_backtest
    )

    r = client.post(
        "/config/backtest/start",
        json={
            "scene": "mid",
            "symbols": ["SOL"],
            "start": "2026-08-01",
            "end": "2026-08-02",
            "initial_quote": 50,
            "batches": 2,
        },
    )
    assert r.status_code == 200
    assert r.json()["run_id"] == "test-run-1"
    assert started["engine"] == "driver"
    assert started["scene"] == "mid"
    assert started["symbols"] == ["SOL"]
    assert started["initial_quote"] == 50.0
    assert started["batches"] == 2
    # 诊断日志覆盖请求摘要
    assert any("📈 回测启动" in line for line in gk._log_calls)
