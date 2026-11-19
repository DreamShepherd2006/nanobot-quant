"""数据陈旧告警的字段通路（C154，2026-11-19）。

背景：2026-10-30 上线「⚠️ 数据滞后 N 分钟（K 线未推进）」——本意是让
K 线冻结这种**静默失效**自暴露。但 `td_live_state.update_symbol()` 是显式
字段白名单拷贝，漏了 `lag_s`：策略写了、`_render_live` 也读了，值在中间
被丢弃 ⇒ 告警结构性失效（页面永远不报）。

这里锁三件事：
1. `update_symbol` 必须保留 `lag_s`；
2. 滞后超阈值时页面真的能渲染出红字；
3. **同类防回归**：`_render_live` 从标的快照里读的每个键都必须在白名单里
   （下次再有人加字段忘了接白名单，测试直接红）。
"""

from __future__ import annotations

import pathlib
import re

from nanobot_quant import td_live_state
from nanobot_quant.td_table_handlers import _render_live

_REPO = pathlib.Path(__file__).resolve().parents[1]
_HANDLERS = _REPO / "src" / "nanobot_quant" / "td_table_handlers.py"
_STATE = _REPO / "src" / "nanobot_quant" / "td_live_state.py"


def test_update_symbol_keeps_lag_s():
    td_live_state.update_symbol(
        "TESTLAG",
        {"setup_buy": 1, "price": 10.0, "signal": "HOLD", "lag_s": 600},
        scene="__t_lag__",
    )
    snap = td_live_state.get_state()["symbols"]["__t_lag__"]["TESTLAG"]
    assert snap["lag_s"] == 600


def _render_with_lag(monkeypatch, tmp_path, lag_s):
    scenes = {
        "high": {"enabled": True, "sleeptime": "1m", "symbols": ["SOL"],
                 "sub_accounts": ["gate_bot1"], "entry_ema_enabled": False},
        "mid": {"enabled": False, "sleeptime": "15m"},
        "low": {"enabled": False, "sleeptime": "1H"},
    }
    monkeypatch.setattr("nanobot_quant.td_table_handlers.load_exec_params",
                        lambda: {"execution_channel": "gate", "scenes": scenes})
    monkeypatch.setattr(td_live_state, "events_path", lambda: tmp_path / "ev.jsonl")
    td_live_state.set_loop(False, next_iteration=None)
    td_live_state.update_symbol(
        "SOL",
        {"setup_buy": 2, "price": 74.0, "signal": "HOLD",
         "time": "2026-11-19 04:00", "lag_s": lag_s},
        scene="high",
    )
    return _render_live(with_script=False, tq={})


def test_stale_warning_renders_when_lag_exceeds_two_bars(monkeypatch, tmp_path):
    """1m 周期、滞后 600s > 2×60s ⇒ 页面出现红字告警。"""
    html = _render_with_lag(monkeypatch, tmp_path, 600)
    assert "数据滞后 10 分钟（K 线未推进）" in html


def test_no_stale_warning_when_lag_is_small(monkeypatch, tmp_path):
    """未超阈值（60s < 2×60s）不得误报。"""
    html = _render_with_lag(monkeypatch, tmp_path, 60)
    assert "数据滞后" not in html


def test_every_symbol_field_read_by_live_render_is_in_whitelist():
    """结构断言：`_render_live` 的 ``d.get("…")`` 全在白名单内。

    白名单是「显式拷贝」而非 **data 透传 —— 少一个键就是一处静默失效
    （lag_s 即此），所以这里把两个文件对上一个账。
    """
    handlers_src = _HANDLERS.read_text("utf-8")
    start = handlers_src.index("def _render_live(")
    # 取到下一个顶层函数定义为止（本函数内部无嵌套 def）
    nxt = handlers_src.index("\ndef ", start + 1)
    live_src = handlers_src[start:nxt]
    read_keys = set(re.findall(r'd\.get\("([a-z0-9_]+)"', live_src))
    assert "lag_s" in read_keys  # 自查：告警分支还在

    state_src = _STATE.read_text("utf-8")
    fn = state_src.index("def update_symbol(")
    nxt = state_src.index("\ndef ", fn + 1)
    whitelist = set(re.findall(r'^\s+"([a-z0-9_]+)":', state_src[fn:nxt], re.M))
    assert whitelist, "白名单解析失败（格式变了就更新这个测试）"

    missing = sorted(read_keys - whitelist)
    assert not missing, f"这些字段页面在读、但 update_symbol 白名单没接：{missing}"
