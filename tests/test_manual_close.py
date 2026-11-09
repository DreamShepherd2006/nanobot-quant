"""手动平仓命令队列 + 策略消费 + 页面端点（2026-11-09）。

三条线分开测：
1. ``manual_close`` 队列语义（入队/待取/超时/mark/recent）；
2. 策略侧 ``_manual_close_round``（复用 ``_sell_lot``，fail-closed）；
3. ``td_manual_close`` 端点（两步确认 + ack_loss 必填 + TD 未运行拒绝）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nanobot_quant import manual_close as mc
from nanobot_quant.td_table_handlers import td_manual_close
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ── 1. 队列 ────────────────────────────────────────────────────────


def test_enqueue_pending_and_mark():
    mc.clear()
    rec = mc.enqueue("high", "crcl", 6, 0.0598)
    assert rec["status"] == "queued" and rec["symbol"] == "CRCL"
    pend = mc.pending_for("high", "CRCL")
    assert [p["slot"] for p in pend] == [6]
    assert mc.pending_for("mid", "CRCL") == []  # 场景隔离

    mc.mark(rec["id"], "done", "已平仓 @ 65.9", price=65.9)
    assert mc.pending_for("high", "CRCL") == []
    latest = mc.recent(1)[0]
    assert latest["status"] == "done" and latest["note"] == "已平仓 @ 65.9"
    assert latest["price"] == 65.9


def test_expired_command_is_skipped(monkeypatch):
    mc.clear()
    rec = mc.enqueue("high", "SOL", 1, 0.03)
    # 把创建时间推到 TTL 之前
    items = mc.load()
    items[0]["created_ts"] = items[0]["created_ts"] - mc.TTL_S - 1
    mc._save(items)  # noqa: SLF001 - 测试直接改落盘内容
    assert mc.pending_for("high", "SOL") == []
    assert mc.recent(1)[0]["status"] == "expired"


def test_clear_resets_queue():
    mc.enqueue("high", "BTC", 1, 0.0001)
    mc.clear()
    assert mc.load() == []


# ── 2. 策略侧消费 ─────────────────────────────────────────────────


class _FakeBM:
    def __init__(self, slots, close_on_sell=True):
        self._slots = list(slots)
        self._closed: set[int] = set()
        self.close_on_sell = close_on_sell
        self.sold: list[tuple] = []

    def open_slots(self):
        return [{"slot": s, "lot": {"qty": 0.01, "entry_price": 100.0}}
                for s in self._slots if s not in self._closed]

    def get_lot(self, slot):
        if slot in self._closed or slot not in self._slots:
            return None
        return {"qty": 0.01, "entry_price": 100.0}

    def close(self, slot):
        self._closed.add(slot)


def _mk_strategy(bm, *, live=True, price=101.0, is_cex=True):
    st = TdSequentialStrategy.__new__(TdSequentialStrategy)
    st.parameters = {"live_mode": live}
    st.symbol = "CRCL"
    st._current_scene = "high"
    st.batch_manager = bm
    st._is_cex = lambda: is_cex
    st._cex_price_of = lambda _s: price

    def _sell(slot, price_, signal, reason):
        bm.sold.append((slot["slot"], price_, reason))
        if bm.close_on_sell:
            bm.close(slot["slot"])

    st._sell_lot = _sell
    return st


def test_strategy_consumes_and_marks_done():
    mc.clear()
    bm = _FakeBM([1, 6])
    mc.enqueue("high", "CRCL", 1, 0.01)
    st = _mk_strategy(bm)
    assert st._manual_close_round() is True
    assert bm.sold and bm.sold[0][0] == 1 and bm.sold[0][2] == "manual"
    assert mc.recent(1)[0]["status"] == "done"
    # 只剩 slot6 还 open
    assert [s["slot"] for s in bm.open_slots()] == [6]


def test_strategy_marks_failed_when_not_filled():
    mc.clear()
    bm = _FakeBM([1], close_on_sell=False)  # 卖出未成交（台账仍 open）
    mc.enqueue("high", "CRCL", 1, 0.01)
    st = _mk_strategy(bm)
    assert st._manual_close_round() is True
    row = mc.recent(1)[0]
    assert row["status"] == "failed" and "仍 open" in row["note"]


def test_strategy_marks_failed_when_no_slot():
    mc.clear()
    bm = _FakeBM([1])
    mc.enqueue("high", "CRCL", 9, 0.01)
    st = _mk_strategy(bm)
    assert st._manual_close_round() is True
    assert "台账无该批次" in mc.recent(1)[0]["note"]


def test_strategy_fails_closed_without_price():
    mc.clear()
    bm = _FakeBM([1])
    mc.enqueue("high", "CRCL", 1, 0.01)
    st = _mk_strategy(bm, price=0.0)
    st._manual_close_round()
    assert "取价失败" in mc.recent(1)[0]["note"]
    assert bm.sold == []


def test_strategy_ignores_commands_in_backtest():
    mc.clear()
    bm = _FakeBM([1])
    mc.enqueue("high", "CRCL", 1, 0.01)
    st = _mk_strategy(bm, live=False)
    assert st._manual_close_round() is False
    assert bm.sold == []
    # 命令仍留在队列里（回测不得消费实盘命令）
    assert mc.pending_for("high", "CRCL")


def test_dex_channel_unsupported():
    mc.clear()
    bm = _FakeBM([1])
    mc.enqueue("high", "CRCL", 1, 0.01)
    st = _mk_strategy(bm, is_cex=False)
    st._manual_close_round()
    assert "DEX" in mc.recent(1)[0]["note"]


# ── 3. 端点 ───────────────────────────────────────────────────────


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _call(body):
    resp = asyncio.run(td_manual_close(_Req(body)))
    return resp.status_code, json.loads(resp.body)


@pytest.fixture()
def _running(monkeypatch):
    import nanobot_quant.td_table_handlers as h

    monkeypatch.setattr(h, "_mc_live_state", lambda: {"running": True})
    monkeypatch.setattr(
        h, "_mc_open_lots",
        lambda scene, sym: [
            {"slot": 1, "qty": 0.05, "entry_price": 76.0, "entry_time": ""},
            {"slot": 6, "qty": 0.06, "entry_price": 66.0, "entry_time": ""},
        ],
    )
    monkeypatch.setattr(h, "_mc_price", lambda scene, sym: 65.9)
    mc.clear()


def test_start_requires_td_running(monkeypatch):
    import nanobot_quant.td_table_handlers as h

    monkeypatch.setattr(h, "_mc_live_state", lambda: {"running": False})
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 1})
    assert code == 409 and "TD 未运行" in data["error"]


def test_start_unknown_slot(_running):
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 9})
    assert code == 404 and "台账无" in data["error"]


def test_start_returns_preview(_running):
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 1})
    assert code == 200 and data["ok"]
    assert data["qty"] == 0.05 and data["price"] == 65.9
    assert data["pnl_usd"] is not None and data["pnl_usd"] < 0
    assert [s["slot"] for s in data["siblings"]] == [6]
    assert data["token"]


def test_confirm_requires_ack(_running):
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 1})
    token = data["token"]
    code, data = _call({"phase": "confirm", "token": token, "ack_loss": False})
    assert code == 400 and "勾选" in data["error"]
    # token 已被消费（单次使用）
    code, data = _call({"phase": "confirm", "token": token, "ack_loss": True})
    assert code == 400 and "过期" in data["error"]


def test_confirm_enqueues(_running):
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 1})
    code, data = _call({"phase": "confirm", "token": data["token"], "ack_loss": True})
    assert code == 200 and data["count"] == 1
    pend = mc.pending_for("high", "CRCL")
    assert [p["slot"] for p in pend] == [1]


def test_confirm_all_slots(_running):
    code, data = _call({"phase": "start", "scene": "high", "symbol": "CRCL", "slot": 1})
    code, data = _call({"phase": "confirm", "token": data["token"],
                        "ack_loss": True, "all_slots": True})
    assert code == 200 and data["count"] == 2
    assert sorted(p["slot"] for p in mc.pending_for("high", "CRCL")) == [1, 6]


def test_status_phase(_running):
    mc.enqueue("high", "CRCL", 1, 0.05)
    code, data = _call({"phase": "status", "scene": "high", "symbol": "CRCL"})
    assert code == 200 and data["items"][0]["slot"] == 1


def test_unknown_phase(_running):
    code, data = _call({"phase": "whatever"})
    assert code == 400 and "未知 phase" in data["error"]


def test_page_has_manual_close_ui():
    """页面模板必须有按钮/弹窗/端点调用（防模板回退）。"""
    from nanobot_quant.td_table_handlers import _load_template

    page = _load_template()
    for token in ("mc-btn", "mc-mask", "mc-ack", "/config/td-table/manual-close",
                  "ack_loss", "all_slots"):
        assert token in page, token
    # 回归（2026-11-09 实机 bug）：勾选 ack 后确认按钮仍灰 —— 必须有 change 监听
    assert "addEventListener('change'" in page
    assert "mc-ack" in page and "disabled = !ev.target.checked" in page
