"""OKX 期权 handlers 分派测试（批 1 Step 2，2026-09-08）。

验证 /sell /close /preview 的模块级 dispatch helper 按 inst_id 的
opt_type（P/C）正确分派到 put/call 路径，call 透传 cost_basis 保本门。
"""

import pytest

from nanobot_quant import okx_options_handlers as oh
from nanobot_quant import okx_options_trade as ot


@pytest.fixture(autouse=True)
def _dispatch_mocks(monkeypatch):
    """把 resolve_instrument 与 put/call 函数替换为记录桩，不触网络。"""
    calls = {}

    def fake_resolve(inst_id):
        return {"opt_type": "C" if inst_id.endswith("-C") else "P",
                "lot": 0.1, "strike": 101.0, "exp_ms": 0, "inst_family": "SOL-USD_UM"}

    monkeypatch.setattr(oh.ot, "resolve_instrument", fake_resolve)
    for name in ("open_put", "open_call", "close_put", "close_call",
                 "preview_open_put", "preview_open_call"):
        monkeypatch.setattr(oh.ot, name, _make_fake(calls, name))
    return calls


def _make_fake(calls, name):
    def _f(*a, **kw):
        calls[name] = {"a": a, "kw": kw}
        return {"ok": True, "kind": name, "opt_type": kw.get("inst_id", a[0] if a else "").rsplit("-", 1)[-1][:1]}
    return _f


# ── 分派：卖期权（sell）──


def test_dispatch_sell_call(_dispatch_mocks):
    """C 合约 → open_call，cost_basis 透传（保本门强制在后端执行）。"""
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-C", 1, "limit", 1.1, cost_basis=106.0)
    assert _dispatch_mocks["open_call"]["kw"]["cost_basis"] == 106.0
    assert _dispatch_mocks["open_call"]["kw"]["inst_id"].endswith("-C")
    assert "open_put" not in _dispatch_mocks


def test_dispatch_sell_put(_dispatch_mocks):
    """P 合约 → open_put（保持原路径，cost_basis 不适用不传递）。"""
    oh._dispatch_sell("bot1", "SOL-USD_UM-260910-101-P", 1, "limit", 0.3)
    assert _dispatch_mocks["open_put"]["kw"]["inst_id"].endswith("-P")
    assert "open_call" not in _dispatch_mocks


# ── 分派：买回平仓（close）──


def test_dispatch_close_call(_dispatch_mocks):
    """C 合约平仓 → close_call（只匹配 open_call 台账行）。"""
    oh._dispatch_close("bot1", "SOL-USD_UM-260910-106-C", 1, "limit", 0.2)
    assert "close_call" in _dispatch_mocks
    assert "close_put" not in _dispatch_mocks


def test_dispatch_close_put(_dispatch_mocks):
    """P 合约平仓 → close_put（原路径）。"""
    oh._dispatch_close("bot1", "SOL-USD_UM-260910-101-P", 1, "limit", 0.2)
    assert "close_put" in _dispatch_mocks
    assert "close_call" not in _dispatch_mocks


# ── 分派：预览（preview）──


def test_dispatch_preview_call_gate(_dispatch_mocks):
    """C 合约预览 → preview_open_call 带 cost_basis（返回保本门状态）。"""
    oh._dispatch_preview("SOL-USD_UM-260910-101-C", 1, "limit", 1.1, cost_basis=106.0)
    kw = _dispatch_mocks["preview_open_call"]["kw"]
    assert kw["cost_basis"] == 106.0
    assert "preview_open_put" not in _dispatch_mocks


def test_dispatch_preview_put(_dispatch_mocks):
    """P 合约预览 → preview_open_put（原路径，无 cost_basis）。"""
    oh._dispatch_preview("SOL-USD_UM-260910-101-P", 1, "limit", 0.3)
    assert "preview_open_put" in _dispatch_mocks
    assert "preview_open_call" not in _dispatch_mocks
