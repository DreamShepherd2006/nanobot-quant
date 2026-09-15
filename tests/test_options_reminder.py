"""C28 Step 1c：到期待办提醒按方向/状态分派动作（put 补买 / call 出货 / 未到期平仓 / 等待 / 核对）。"""
from __future__ import annotations

import json
import time

import pytest

from nanobot_quant import okx_options_trade as ot

NOW = int(time.time() * 1000)
H = 3600_000


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "okx_options_ledger.json"
    monkeypatch.setattr(ot, "ledger_path", lambda: p)

    def write(rows):
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    return write


def _row(rid, kind, exp_ms, status="open", sz=1):
    tail = "C" if kind == "open_call" else "P"
    return {
        "id": rid, "kind": kind, "status": status, "sz": sz, "strike": 94,
        "inst_id": "SOL-USD_UM-260918-94-" + tail, "exp_ms": exp_ms,
    }


def test_upcoming_open_suggests_close(ledger):
    ledger([_row("a", "open_put", NOW + 30 * H)])
    r = ot.expiry_reminder()
    assert len(r) == 1
    assert r[0]["action"] == "close" and r[0]["expired"] is False
    assert r[0]["opt_type"] == "P"


def test_expired_open_waits_for_settlement(ledger):
    ledger([_row("a", "open_put", NOW - H)])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "wait" and r[0]["expired"] is True
    assert "cover" not in [x["action"] for x in r]


def test_far_future_open_not_reminded(ledger):
    ledger([_row("a", "open_put", NOW + 200 * H)])
    assert ot.expiry_reminder() == []


def test_itm_put_needs_cover(ledger):
    ledger([_row("a", "open_put", NOW - H, status=ot.STATUS_SETTLED_ITM)])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "cover" and r[0]["opt_type"] == "P"


def test_itm_call_needs_exit(ledger):
    ledger([_row("a", "open_call", NOW - H, status=ot.STATUS_SETTLED_ITM)])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "exit" and r[0]["opt_type"] == "C"


def test_itm_put_closed_by_filled_cover_is_silent(ledger):
    ledger([
        _row("a", "open_put", NOW - H, status=ot.STATUS_SETTLED_ITM),
        {"id": "b", "kind": "spot_cover", "status": "filled", "inst_id": "SOL-USD"},
    ])
    assert ot.expiry_reminder() == []


def test_itm_call_closed_by_filled_exit_is_silent(ledger):
    ledger([
        _row("a", "open_call", NOW - H, status=ot.STATUS_SETTLED_ITM),
        {"id": "b", "kind": "spot_exit", "status": "filled", "inst_id": "SOL-USD"},
    ])
    assert ot.expiry_reminder() == []


def test_call_cover_row_does_not_close_call_leg(ledger):
    """方向不串：spot_cover 不能把 call 的待出货判为已闭环。"""
    ledger([
        _row("a", "open_call", NOW - H, status=ot.STATUS_SETTLED_ITM),
        {"id": "b", "kind": "spot_cover", "status": "filled", "inst_id": "SOL-USD"},
    ])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "exit"


def test_pending_spot_order_still_reminds(ledger):
    """补买单还在 pending（未成交）→ 仍需提醒，不能当已闭环。"""
    ledger([
        _row("a", "open_put", NOW - H, status=ot.STATUS_SETTLED_ITM),
        {"id": "b", "kind": "spot_cover", "status": "pending", "inst_id": "SOL-USD"},
    ])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "cover"


def test_review_action(ledger):
    ledger([_row("a", "open_call", NOW - H, status=ot.STATUS_SETTLED_REVIEW)])
    r = ot.expiry_reminder()
    assert r and r[0]["action"] == "review"


def test_settled_otm_not_reminded(ledger):
    ledger([_row("a", "open_put", NOW - H, status=ot.STATUS_SETTLED_OTM)])
    assert ot.expiry_reminder() == []


def test_action_priority_order(ledger):
    ledger([
        _row("c", "open_put", NOW + 10 * H),
        _row("r", "open_call", NOW - H, status=ot.STATUS_SETTLED_REVIEW),
        _row("i", "open_put", NOW - 2 * H, status=ot.STATUS_SETTLED_ITM),
    ])
    assert [x["action"] for x in ot.expiry_reminder()] == ["cover", "review", "close"]
