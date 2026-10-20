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
    # 数据源：请求未带 → 默认 Gate（与执行同所，零行为变化）
    assert started["source"] == "gate_cex"
    # 诊断日志覆盖请求摘要
    assert any("📈 回测启动" in line for line in gk._log_calls)
    # 诊断日志覆盖请求摘要
    assert any("📈 回测启动" in line for line in gk._log_calls)


def test_start_rejects_unknown_source(client, monkeypatch):
    """未知数据源 → 400 明确报错（不静默回退到 Gate）。"""
    client, _gk = client
    called = {}
    monkeypatch.setattr(
        "nanobot_quant.tools.tools_backtest.run_backtest",
        lambda **kw: called.update(kw) or {"status": "started", "run_id": "x"},
    )
    r = client.post(
        "/config/backtest/start",
        json={"scene": "mid", "symbols": ["SOL"], "source": "binance"},
    )
    assert r.status_code == 400
    assert "不支持的回测数据源" in r.json()["error"]
    assert not called


def test_start_passes_okx_source(client, monkeypatch):
    client, gk = client
    started = {}
    monkeypatch.setattr(
        "nanobot_quant.tools.tools_backtest.run_backtest",
        lambda **kw: started.update(kw) or {"status": "started", "run_id": "r-okx"},
    )
    r = client.post(
        "/config/backtest/start",
        json={"scene": "mid", "symbols": ["SOL"], "source": "okx_cex"},
    )
    assert r.status_code == 200
    assert started["source"] == "okx_cex"
    assert any("source=okx_cex" in line for line in gk._log_calls)


def test_coerce_overrides():
    """覆盖参数归一化：int/float/bool 按字段转换；空串/None 忽略。"""
    assert bh._coerce_overrides(
        {
            "entry_setup": "10",
            "sell_only_profit_high": "0.005",
            "momentum_exit": "on",
            "td_sell_all": False,
            "exit_order": "lifo",
            "stop_loss_pct": "",
            "exit_setup": None,
        }
    ) == {
        "entry_setup": 10,
        "sell_only_profit_high": 0.005,
        "momentum_exit": True,
        "td_sell_all": False,
        "exit_order": "lifo",
    }
    assert bh._coerce_overrides(None) == {}
    assert bh._coerce_overrides({"exit_setup": "null", "min_hold_bars": ""}) == {}
    with pytest.raises(ValueError):
        bh._coerce_overrides({"entry_setup": "abc"})


def test_coerce_overrides_accepts_ema_and_bars_mode():
    """EMA 趋势变量 / 逐 bar 表模式也要能覆盖（2026-10-20）。

    白名单没收录的键会走直通分支——以前 f1_gate_enabled 就是因此
    「页面填了也不生效」，所以这里用显式断言锁住类型转换。
    """
    assert bh._coerce_overrides(
        {
            "ema_gate_enabled": "on",
            "ema_n": "89",
            "ema_k": "5",
            "bars_mode": "full",
        }
    ) == {
        "ema_gate_enabled": True,
        "ema_n": 89,
        "ema_k": 5,
        "bars_mode": "full",
    }
    assert bh._coerce_overrides({"ema_gate_enabled": "off"}) == {
        "ema_gate_enabled": False
    }


def test_start_passes_overrides(client, monkeypatch):
    """回测启动请求携带 overrides → run_backtest 原样透传（仅本次回测）。"""
    client, gk = client
    started = {}

    def _fake_run_backtest(**kw):
        started.update(kw)
        return {"status": "started", "run_id": "test-run-ov"}

    monkeypatch.setattr(
        "nanobot_quant.tools.tools_backtest.run_backtest", _fake_run_backtest
    )

    r = client.post(
        "/config/backtest/start",
        json={
            "scene": "high",
            "symbols": ["ARB"],
            "initial_quote": 60,
            "batches": 3,
            "fixed_amount": 4,
            "overrides": {
                "exit_setup": 12,
                "sell_only_profit_high": "0.005",
                "momentum_exit": "on",
                "td_sell_all": "off",
            },
        },
    )
    assert r.status_code == 200
    assert started["overrides"] == {
        "exit_setup": 12,
        "sell_only_profit_high": 0.005,
        "momentum_exit": True,
        "td_sell_all": False,
    }


def test_page_has_quantity_mode_override():
    """数量模式覆盖 + 固定金额框仅固定金额模式可用（2026-10-20）。

    背景：2026-10-20 线上回测 0 成交——场景 fixed × 10 SOL 撞 position_limit，
    而旧的「TD 固定金额」只覆盖金额不覆盖模式 ⇒ 填了也不生效。
    """
    from pathlib import Path

    html = Path(bh._HERE) / "backtest_page.html"
    html = html.read_text(encoding="utf-8")
    assert 'id="quantity_mode"' in html
    assert 'value="fixed_amount"' in html
    # 覆盖体里透传（留空=跟随场景）
    assert "overrides.quantity_mode = qm" in html
    # 固定金额框随模式启用/禁用
    assert 'id="fixed_amount_hint"' in html
    assert "function syncFixedAmountField" in html
    assert "input.disabled = !active" in html
    # 报告展示实际生效的数量模式
    assert '"数量模式"' in html
