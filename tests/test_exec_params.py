"""Tests for execution parameters: defaults, validation, persistence,
WebUI handlers (/config/exec) and the pipeline wiring (run_from_signals
reads exec_params.json; OnchainOSBroker receives slippage/buffer)."""

import asyncio
import json

import pytest

from nanobot_quant import exec_params as exec_params_mod
from nanobot_quant.exec_params import (
    DEFAULT_EXEC_PARAMS,
    PARAM_META,
    load_exec_params,
    save_exec_params,
    validate_exec_param,
)
from nanobot_quant.exec_params_handlers import register_exec_params_routes


# ── Helpers ─────────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, body=None, session_user=None):
        self._body = body
        self.session = {"user": session_user} if session_user else {}

    async def json(self):
        return self._body


class _FakeApp:
    def __init__(self):
        self.routes = []

    def add_route(self, path, fn, methods=None):
        self.routes.append((path, fn, methods))


class _FakePlatform:
    data_root = "/data"

    def is_commander(self, user):
        return user == "commander"


class _FakeGatekeeper:
    def __init__(self):
        self._platform = _FakePlatform()
        self.logs = []

    def _log(self, msg):
        self.logs.append(msg)


@pytest.fixture(autouse=True)
def _isolated_params(tmp_path, monkeypatch):
    """Point exec_params_path (module + handler import sites) at temp file."""
    target = tmp_path / "exec_params.json"

    def fake_path():
        return target

    monkeypatch.setattr(exec_params_mod, "exec_params_path", fake_path)
    yield target


def _call(handler, body=None, user="commander"):
    return asyncio.run(handler(_FakeRequest(body, user)))


# ── Defaults / validation ────────────────────────────────────────────────

def test_defaults_match_pre_parameterisation_hardcoded():
    assert DEFAULT_EXEC_PARAMS["max_position_pct"] == 0.20
    assert DEFAULT_EXEC_PARAMS["max_drawdown_pct"] == 0.15
    assert DEFAULT_EXEC_PARAMS["stop_loss_pct"] == 0.10
    assert DEFAULT_EXEC_PARAMS["slippage"] == 0.01
    assert DEFAULT_EXEC_PARAMS["sol_buffer_pct"] == 0.05
    # TD 自主运行（P2 B2）默认值
    assert DEFAULT_EXEC_PARAMS["td_symbol"] == "SOL"
    assert DEFAULT_EXEC_PARAMS["td_sleeptime"] == "1D"
    assert DEFAULT_EXEC_PARAMS["quantity_mode"] == "fixed"


def test_meta_covers_all_defaults_and_three_groups():
    # 全部默认值（含 td 组 5 字段 + batch 组 3 字段）都有 PARAM_META 元数据；
    # P1 loop 模式（execution_mode/loop_interval_seconds）已随 B3 退役。
    ui_params = set(DEFAULT_EXEC_PARAMS)
    assert set(PARAM_META) == ui_params
    groups = {m["group"] for m in PARAM_META.values()}
    assert groups == {"risk", "exec", "td", "batch"}


@pytest.mark.parametrize(
    "key,bad",
    [
        ("max_position_pct", 0.0),
        ("max_position_pct", 1.01),
        ("max_drawdown_pct", -0.1),
        ("stop_loss_pct", 2.0),
        ("slippage", -0.01),
        ("slippage", 1.01),
        ("sol_buffer_pct", 1.5),
        ("sol_buffer_pct", "0.05"),
        ("max_position_pct", True),
    ],
)
def test_validation_rejects_out_of_range(key, bad):
    assert validate_exec_param(key, bad) is not None


@pytest.mark.parametrize(
    "key,good",
    [
        ("max_position_pct", 0.25),
        ("max_drawdown_pct", 0.30),
        ("stop_loss_pct", 0.05),
        ("slippage", 0.02),
        ("sol_buffer_pct", 0.10),
        ("td_symbol", "CRCLX"),
        ("td_sleeptime", "1H"),
        ("td_sleeptime", "1W"),
        ("quantity_mode", "value"),
        ("td_quantity", 1),
        ("td_quantity", 100000),
        ("td_enabled", True),
        ("td_enabled", False),
    ],
)
def test_validation_accepts_in_range(key, good):
    assert validate_exec_param(key, good) is None


@pytest.mark.parametrize(
    "key,bad",
    [
        ("td_symbol", ""),
        ("td_symbol", "  "),
        ("td_symbol", 123),
        ("td_sleeptime", "4H"),
        ("td_sleeptime", "1D "),
        ("td_sleeptime", "1"),
        ("quantity_mode", "fixedx"),
        ("quantity_mode", "10"),
    ],
)
def test_validation_rejects_bad_td_fields(key, bad):
    assert validate_exec_param(key, bad) is not None


@pytest.mark.parametrize(
    "key,bad",
    [
        ("td_quantity", 0),
        ("td_quantity", -1),
        ("td_quantity", 100001),
        ("td_quantity", "5"),
        ("td_quantity", 5.5),
        ("td_enabled", "true"),
        ("td_enabled", 1),
    ],
)
def test_validation_rejects_bad_td_runtime_fields(key, bad):
    assert validate_exec_param(key, bad) is not None


def test_td_quantity_default_and_roundtrip(tmp_path):
    assert DEFAULT_EXEC_PARAMS["td_quantity"] == 10
    res = save_exec_params({"td_quantity": 30})
    assert res["ok"] is True
    assert load_exec_params()["td_quantity"] == 30
    # 非法值保存被拒且不落盘
    res2 = save_exec_params({"td_quantity": 0})
    assert res2["ok"] is False
    assert load_exec_params()["td_quantity"] == 30


def test_td_enabled_roundtrip(tmp_path):
    assert DEFAULT_EXEC_PARAMS["td_enabled"] is False
    res = save_exec_params({"td_enabled": True})
    assert res["ok"] is True
    assert load_exec_params()["td_enabled"] is True
    res2 = save_exec_params({"td_enabled": "yes"})
    assert res2["ok"] is False
    assert load_exec_params()["td_enabled"] is True


def test_unknown_key_rejected():
    assert validate_exec_param("nope", 0.1) is not None


# ── Load / save ──────────────────────────────────────────────────────────

def test_load_without_file_returns_defaults(tmp_path):
    assert load_exec_params() == DEFAULT_EXEC_PARAMS


def test_save_then_load_roundtrip(tmp_path):
    params = dict(DEFAULT_EXEC_PARAMS)
    params["max_position_pct"] = 0.30
    params["slippage"] = 0.02
    res = save_exec_params(params)
    assert res["ok"] is True
    loaded = load_exec_params()
    assert loaded["max_position_pct"] == 0.30
    assert loaded["slippage"] == 0.02
    # untouched keys keep defaults
    assert loaded["stop_loss_pct"] == 0.10


def test_save_rejects_partial_invalid(tmp_path):
    params = dict(DEFAULT_EXEC_PARAMS)
    params["max_position_pct"] = 1.5  # invalid
    res = save_exec_params(params)
    assert res["ok"] is False
    assert "error" in res
    # file must NOT be written
    assert not exec_params_mod.exec_params_path().exists()


def test_save_partial_update_keeps_rest(tmp_path):
    res = save_exec_params({"max_position_pct": 0.35})
    assert res["ok"] is True
    loaded = load_exec_params()
    assert loaded["max_position_pct"] == 0.35
    assert loaded["max_drawdown_pct"] == 0.15


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    exec_params_mod.exec_params_path().write_text("{not json", encoding="utf-8")
    assert load_exec_params() == DEFAULT_EXEC_PARAMS


def test_reset_removes_file(tmp_path):
    save_exec_params(dict(DEFAULT_EXEC_PARAMS, max_position_pct=0.5))
    assert exec_params_mod.exec_params_path().is_file()
    res = save_exec_params({"reset": True})
    assert res["ok"] is True
    assert not exec_params_mod.exec_params_path().exists()
    assert load_exec_params() == DEFAULT_EXEC_PARAMS


# ── WebUI handlers ───────────────────────────────────────────────────────

def test_register_adds_get_and_post():
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    paths = {p for p, _, m in app.routes}
    assert "/config/exec" in paths
    assert any("GET" in m for _, _, m in app.routes)
    assert any("POST" in m for _, _, m in app.routes)


def test_page_requires_login():
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    page = next(fn for p, fn, m in app.routes if p == "/config/exec" and "GET" in m)
    resp = asyncio.run(page(_FakeRequest(session_user=None)))
    assert resp.status_code == 401


def test_page_forbidden_for_non_commander():
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    page = next(fn for p, fn, m in app.routes if p == "/config/exec" and "GET" in m)
    resp = asyncio.run(page(_FakeRequest(session_user="someone")))
    assert resp.status_code == 403


def test_page_renders_groups_with_current_values(tmp_path):
    save_exec_params(dict(DEFAULT_EXEC_PARAMS, max_position_pct=0.40))
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    page = next(fn for p, fn, m in app.routes if p == "/config/exec" and "GET" in m)
    resp = asyncio.run(page(_FakeRequest(session_user="commander")))
    html = resp.body.decode()
    assert "执行参数" in html
    assert "风险控制" in html
    assert "执行质量" in html
    assert "TD 自主运行" in html
    assert 'value="0.4"' in html  # current value rendered


def test_page_renders_td_fields(tmp_path):
    """TD 自主运行组渲染：周期/数量模式下拉 + 标的候选（默认值选中）。"""
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    page = next(fn for p, fn, m in app.routes if p == "/config/exec" and "GET" in m)
    html = asyncio.run(page(_FakeRequest(session_user="commander"))).body.decode()
    assert 'id="td_sleeptime"' in html
    assert 'value="1D" selected' in html
    assert 'id="quantity_mode"' in html
    assert 'value="fixed" selected' in html
    assert 'id="td_symbol"' in html


def test_save_via_handler_persists(tmp_path):
    app = _FakeApp()
    gk = _FakeGatekeeper()
    register_exec_params_routes(app, gk)
    save = next(fn for p, fn, m in app.routes if p == "/config/exec" and "POST" in m)
    body = dict(DEFAULT_EXEC_PARAMS, slippage=0.03)
    resp = asyncio.run(save(_FakeRequest(body, "commander")))
    data = json.loads(resp.body.decode())
    assert data["ok"] is True
    assert load_exec_params()["slippage"] == 0.03
    assert any("执行参数" in log for log in gk.logs)


def test_save_via_handler_persists_td_fields(tmp_path):
    """TD 自主运行字段通过 handler 保存并即时生效。"""
    app = _FakeApp()
    gk = _FakeGatekeeper()
    register_exec_params_routes(app, gk)
    save = next(fn for p, fn, m in app.routes if p == "/config/exec" and "POST" in m)
    body = dict(DEFAULT_EXEC_PARAMS, td_symbol="CRCLX", td_sleeptime="1H",
                quantity_mode="value", td_quantity=25, td_enabled=True)
    resp = asyncio.run(save(_FakeRequest(body, "commander")))
    data = json.loads(resp.body.decode())
    assert data["ok"] is True
    loaded = load_exec_params()
    assert loaded["td_symbol"] == "CRCLX"
    assert loaded["td_sleeptime"] == "1H"
    assert loaded["quantity_mode"] == "value"
    assert loaded["td_quantity"] == 25
    assert loaded["td_enabled"] is True
    assert any("td_symbol=CRCLX" in log for log in gk.logs)


def test_save_rejects_invalid(tmp_path):
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    save = next(fn for p, fn, m in app.routes if p == "/config/exec" and "POST" in m)
    body = dict(DEFAULT_EXEC_PARAMS, stop_loss_pct=5.0)
    resp = asyncio.run(save(_FakeRequest(body, "commander")))
    data = json.loads(resp.body.decode())
    assert data["ok"] is False
    assert "error" in data


def test_save_forbidden_for_non_commander(tmp_path):
    app = _FakeApp()
    register_exec_params_routes(app, _FakeGatekeeper())
    save = next(fn for p, fn, m in app.routes if p == "/config/exec" and "POST" in m)
    resp = asyncio.run(save(_FakeRequest({}, "someone")))
    data = json.loads(resp.body.decode())
    assert data["ok"] is False


# ── Pipeline wiring ──────────────────────────────────────────────────────

def test_run_from_signals_uses_file_values(monkeypatch, tmp_path):
    """Custom exec_params.json values flow into run_from_signals and the
    OnchainOSBroker construction (slippage/buffer)."""
    import sys
    import types

    from nanobot_quant import pipeline as pipeline_mod

    save_exec_params(dict(
        DEFAULT_EXEC_PARAMS,
        max_position_pct=0.35,
        slippage=0.02,
        sol_buffer_pct=0.10,
    ))

    captured = {}

    # ── fake lumibot.brokers (onchainos_broker imports Broker at module
    # level; MUST be in place before anything imports that module) ──
    _brokers = types.ModuleType("lumibot.brokers")

    class Broker:
        def __init__(self, *a, **k):
            pass

    _brokers.Broker = Broker
    monkeypatch.setitem(sys.modules, "lumibot.brokers", _brokers)

    # ── fake OnchainOSBroker (imported inside live path) ──
    class FakeBroker:
        def __init__(self, tokens_json=None, slippage="0.01", sol_buffer_pct=0.05, **kw):
            captured["slippage"] = slippage
            captured["sol_buffer_pct"] = sol_buffer_pct
        def _submit_order(self, order):
            captured["submitted"] = True
            order.identifier = "mock-tx"
            order.status = "filled"

    monkeypatch.setattr(
        "nanobot_quant.brokers.onchainos_broker.OnchainOSBroker", FakeBroker
    )

    # ── fake AnalysisPipeline: risk checks always pass ──
    class _FakeRisk:
        def check_position_limit(self, **kw):
            return types.SimpleNamespace(approved=True, reason="")
        def check_max_drawdown(self, **kw):
            return types.SimpleNamespace(approved=True, reason="")
        def check_stop_loss(self, **kw):
            return types.SimpleNamespace(approved=True, reason="")

    class FakePipe:
        def __init__(self, **kw):
            self._risk = _FakeRisk()
            captured["max_position_pct"] = kw.get("max_position_pct")
        def _calculate_quantity(self, pv, price):
            return int(pv * captured["max_position_pct"] / price) or 1

    monkeypatch.setattr(pipeline_mod, "AnalysisPipeline", FakePipe)

    # ── fake lumibot.entities (conftest only stubs strategies) ──
    _entities = types.ModuleType("lumibot.entities")

    class Asset:
        def __init__(self, *a, **k):
            pass

    class Order:
        def __init__(self, **k):
            self.quantity = k.get("quantity")
            self.identifier = None
            self.status = "new"

    _entities.Asset = Asset
    _entities.Order = Order
    monkeypatch.setitem(sys.modules, "lumibot.entities", _entities)

    # ── fake lumibot.brokers (onchainos_broker imports Broker at module level) ──
    _brokers = types.ModuleType("lumibot.brokers")

    class Broker:
        def __init__(self, *a, **k):
            pass

    _brokers.Broker = Broker
    monkeypatch.setitem(sys.modules, "lumibot.brokers", _brokers)

    signal = {
        "ticker": "SOL", "recommendation": "BUY", "score": 3.0,
        "price": 170.0, "confidence": 0.9,
        "setup_buy": 9, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0,
        "tdst_support": None, "tdst_resistance": None, "rvol": 1.0,
    }
    results = pipeline_mod.run_from_signals(
        [signal], live=True, portfolio_value=1000.0
    )

    assert captured["max_position_pct"] == 0.35
    assert captured["slippage"] == "0.02"
    assert captured["sol_buffer_pct"] == 0.10
    assert captured.get("submitted") is True
    assert results[0]["risk_passed"] is True
    assert results[0]["tx_hash"] == "mock-tx"
