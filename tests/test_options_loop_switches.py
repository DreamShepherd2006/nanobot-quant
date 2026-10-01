"""期权循环：子线开关联动 + 一键启停（2026-10-01，方案 B）。

背景：``put_enabled`` / ``call_enabled`` 原先只关「开仓支线」，止盈巡检（``_exits``）
与自动补买（``_auto_cover``）恒跑 —— 用户想手动接管 call 仓时，关掉「卖 call」
照样会被止盈线自动买回。本批把两条线的管理范围收紧为「不卖、不补买、不止盈」，
**到期判定（``_settle_expired``）保留**：它只处理已到期合约、负责闭账，不干扰手动操作。
"""

from __future__ import annotations

from pathlib import Path

from nanobot_quant.strategies.okx_options_put_strategy import OkxOptionsPutStrategy

PAGE = (Path(__file__).resolve().parents[1]
        / "src" / "nanobot_quant" / "okx_options_page.html").read_text(encoding="utf-8")


def _loop(monkeypatch, *, put: bool = True, call: bool = False):
    """构造只跑决策骨架的策略实例，记录各子步骤是否被调用。"""
    s = OkxOptionsPutStrategy()
    s.parameters = {**dict(OkxOptionsPutStrategy.parameters), "live_mode": False,
                    "put_enabled": put, "call_enabled": call}
    s._cycle_state = {}
    seen: list[str] = []

    def rec(name, ret=None):
        def _f(*a, **kw):
            seen.append(name)
            return [] if ret is None else ret
        return _f

    monkeypatch.setattr(s, "_log", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(s, "_settle_expired", rec("settle"), raising=False)
    monkeypatch.setattr(s, "_resolve_pending", rec("pending"), raising=False)
    monkeypatch.setattr(s, "_positions", rec("positions", []), raising=False)
    monkeypatch.setattr(s, "_auto_cover", rec("cover"), raising=False)
    monkeypatch.setattr(s, "_entries", rec("entry"), raising=False)
    monkeypatch.setattr(s, "_call_entries", rec("call_entry"), raising=False)
    monkeypatch.setattr(
        s, "_exits",
        lambda a, p, d, positions, opt_type="P": (seen.append("exit_" + opt_type) or []),
        raising=False)
    monkeypatch.setattr(s, "_finish", lambda *a, **kw: seen.append("finish"), raising=False)
    return s, seen


# ───────────────── 子线开关：关某条线 = 该方向不卖 / 不补买 / 不止盈 ─────────────────

def test_put_line_off_skips_cover_and_put_exits(monkeypatch):
    s, seen = _loop(monkeypatch, put=False, call=True)
    s.on_trading_iteration()
    assert "cover" not in seen, "关 put 线后不得再自动补买"
    assert "exit_P" not in seen, "关 put 线后不得再跑 put 止盈巡检"
    assert "exit_C" in seen, "另一条线不受影响"
    assert "settle" in seen, "到期判定恒跑"


def test_call_line_off_skips_call_exits(monkeypatch):
    s, seen = _loop(monkeypatch, put=True, call=False)
    s.on_trading_iteration()
    assert "exit_C" not in seen, "关卖 call 后不得再自动买回 call 仓（本批核心）"
    assert "exit_P" in seen and "cover" in seen, "put 线不受影响"
    assert "settle" in seen


def test_both_lines_off_only_settles(monkeypatch):
    s, seen = _loop(monkeypatch, put=False, call=False)
    s.on_trading_iteration()
    assert "cover" not in seen
    assert "exit_P" not in seen and "exit_C" not in seen
    assert "settle" in seen and "finish" in seen, "只剩「到期判定 + 状态落盘」"


def test_defaults_match_legacy_behaviour(monkeypatch):
    """未配置子线字段时与旧版一致：put 线开、call 线关（live.DEFAULT_STRATEGY 默认值）。"""
    s = OkxOptionsPutStrategy()
    s.parameters = {**dict(OkxOptionsPutStrategy.parameters), "live_mode": False}
    s._cycle_state = {}
    seen: list[str] = []
    monkeypatch.setattr(s, "_log", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(s, "_settle_expired", lambda: seen.append("settle") or [], raising=False)
    monkeypatch.setattr(s, "_resolve_pending", lambda account: [], raising=False)
    monkeypatch.setattr(s, "_positions", lambda account: [], raising=False)
    monkeypatch.setattr(s, "_auto_cover", lambda a, p, d: seen.append("cover") or [], raising=False)
    monkeypatch.setattr(s, "_entries", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(s, "_call_entries", lambda *a, **kw: [], raising=False)
    monkeypatch.setattr(
        s, "_exits",
        lambda a, p, d, positions, opt_type="P": (seen.append("exit_" + opt_type) or []),
        raising=False)
    monkeypatch.setattr(s, "_finish", lambda *a, **kw: None, raising=False)
    s.on_trading_iteration()
    assert "cover" in seen and "exit_P" in seen   # put 线默认开
    assert "exit_C" not in seen                   # call 线默认关


# ───────────────── 一键启停按钮（只 POST enabled，不动其他参数） ─────────────────

def test_page_has_one_click_loop_toggle():
    assert 'id="loopToggleBtn"' in PAGE
    assert "async function toggleLoop()" in PAGE
    assert '$("loopToggleBtn").addEventListener("click", toggleLoop);' in PAGE
    assert 'ltb.textContent = liveRunning ? "⏹ 停止循环" : "▶️ 启动循环"' in PAGE


def test_toggle_posts_only_enabled():
    body = PAGE.split("async function toggleLoop()", 1)[1].split("\nasync function ", 1)[0]
    assert 'jpost("/config/okx-options/live", { enabled: want })' in body, "启停只 POST enabled"
    assert body.count("jpost(") == 1, "启停不得顺带写别的参数（interval/strategy 保持原值）"


def test_note_documents_subline_semantics():
    assert "子线开关（2026-10-01）" in PAGE
    assert "不卖 call、不止盈 call" in PAGE
    assert "到期判定恒跑" in PAGE
