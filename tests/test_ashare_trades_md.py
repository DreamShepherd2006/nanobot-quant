"""模拟账本成交/持仓 Markdown（📋 复制成交）：分臂、截断、空账本、定点格式。"""

from __future__ import annotations

import pytest

from nanobot_quant.ashare import paper_strategy as ps
from nanobot_quant.ashare import trades_md as tm


def fake_payload(second_arm: bool = True) -> dict:
    """构造与 ``paper_runner.state()`` 同形的 payload（不碰 /data）。"""
    def arm(arm_id, fills, shorts, equity, realized, unrealized):
        return {
            "arm": arm_id,
            "label": ps.ARM_LABELS[arm_id],
            "active": arm_id == ps.ARM_A or second_arm,
            "ledger": {
                "equity": equity, "return_pct": round((equity / 500000 - 1) * 100, 4),
                "realized": realized, "unrealized": unrealized,
                "collateral_locked": 28500.0, "available_cash": 471500.0,
                "shorts": shorts, "etf": [],
            },
            "consistency": {"ok": True},
            "fills": fills,
        }

    fills_a = [
        {"ts": "2026-11-26T05:00:00Z", "action": "sell_open", "code": "10009013",
         "underlying": "510050.SH", "strike": 2.85, "expiry": "20261223", "qty": 1,
         "px": 0.0123, "premium": 123.0, "fee": 1.2, "cash_delta": 121.8, "reason": "低9首穿"},
        {"ts": "2026-11-26T06:00:00Z", "action": "buy_close", "code": "10009013",
         "underlying": "510050.SH", "strike": 2.85, "expiry": "20261223", "qty": 1,
         "px": 0.0061, "cost": 61.0, "fee": 1.7, "cash_delta": -62.7,
         "realized": 60.1, "hold_ts": "2026-11-26T05:00:00Z", "reason": "止盈回落 50%"},
    ]
    fills_b = [
        {"ts": "2026-11-26T05:30:00Z", "action": "sell_open", "code": "10009013",
         "underlying": "510050.SH", "strike": 2.85, "expiry": "20261223", "qty": 1,
         "px": 0.0123, "premium": 123.0, "fee": 1.2, "cash_delta": 121.8, "reason": "低9首穿"},
    ]
    shorts = [{
        "code": "10009013", "underlying": "510050.SH", "strike": 2.85,
        "expiry": "20261223", "qty": 1, "entry_px": 0.0123,
        "entry_ts": "2026-11-26T05:30:00Z", "collateral": 28500.0,
        "mark_px": 0.0150, "pnl_pct": -21.95, "unrealized": -27.0, "entry_fee": 1.2,
        "reason": "低9首穿",
    }]
    return {
        "config": {}, "error": "",
        "runner": {"thread_alive": False, "last_run": None, "totals": {"rounds": 208}},
        "arms": {
            ps.ARM_A: arm(ps.ARM_A, fills_a, [], 500060.1, 60.1, 0.0),
            ps.ARM_B: arm(ps.ARM_B, fills_b, shorts, 499973.0, 0.0, -27.0),
        },
    }


@pytest.fixture()
def payload(monkeypatch):
    p = fake_payload()
    monkeypatch.setattr(tm.pr, "state", lambda **kw: p)
    monkeypatch.setattr(tm.pr, "paper_dir", lambda: "/data/legion/credentials/ashare_paper")
    return p


# ── 汇总 ────────────────────────────────────────────────────

def test_two_arms_listed_separately(payload):
    md = tm.build()
    assert "## 臂 A · " in md and "## 臂 B · " in md
    # 同一合约在两臂各出现一次，属预期（两本账），不是重复下单
    assert md.count("10009013") >= 4


def test_summary_win_rate_over_closed_fills(payload):
    md = tm.build()
    a = tm.arm_stats(payload, ps.ARM_A)
    assert a["closed"] == 1 and a["win_rate"] == 100.0
    assert "100.00%" in md


def test_inactive_arm_hidden(monkeypatch):
    p = fake_payload(second_arm=False)
    monkeypatch.setattr(tm.pr, "state", lambda **kw: p)
    monkeypatch.setattr(tm.pr, "paper_dir", lambda: "/tmp/x")
    md = tm.build()
    assert "## 臂 A" in md and "## 臂 B" not in md


# ── 截断 ────────────────────────────────────────────────────

def _many_fills():
    p = fake_payload()
    p["arms"][ps.ARM_A]["fills"] = [
        {"ts": f"2026-11-26T0{i}:00:00Z", "action": "sell_open", "code": f"9{i}",
         "underlying": "510050.SH", "strike": 2.85, "expiry": "20261223", "qty": 1,
         "px": 0.01, "premium": 100.0, "fee": 1.2, "cash_delta": 98.8, "reason": "低9首穿"}
        for i in range(5)
    ]
    return p


def test_limit_truncates_to_newest(monkeypatch):
    p = _many_fills()
    monkeypatch.setattr(tm.pr, "state", lambda **kw: p)
    monkeypatch.setattr(tm.pr, "paper_dir", lambda: "/tmp/x")
    md = tm.build(limit=2)
    assert "最近 **2** 笔" in md
    assert "| 90 (" not in md and "| 93 (" in md and "| 94 (" in md

def test_limit_zero_shows_all(monkeypatch):
    p = _many_fills()
    monkeypatch.setattr(tm.pr, "state", lambda **kw: p)
    monkeypatch.setattr(tm.pr, "paper_dir", lambda: "/tmp/x")
    md = tm.build(limit=0)
    assert "全部保留成交" in md
    for i in range(5):
        assert f"| 9{i} (" in md


# ── 明细与持仓 ──────────────────────────────────────────────

def test_fill_rows_use_action_labels_and_both_times(payload):
    md = tm.build()
    assert "卖出开仓" in md and "买入平仓" in md
    assert "11-26 13:00:00" in md and "11-26 05:00:00" in md   # CST / UTC 双列


def test_position_rows_include_collateral_and_live_mark(payload):
    md = tm.build()
    assert "28500.00" in md          # 担保占用
    assert "0.0150" in md            # 现价（4 位定点，不用科学计数法）
    assert "剩余" in md


def test_empty_ledger_renders_placeholders(monkeypatch):
    p = fake_payload()
    p["arms"][ps.ARM_A]["fills"] = []
    p["arms"][ps.ARM_B]["fills"] = []
    p["arms"][ps.ARM_B]["ledger"]["shorts"] = []
    monkeypatch.setattr(tm.pr, "state", lambda **kw: p)
    monkeypatch.setattr(tm.pr, "paper_dir", lambda: "/tmp/x")
    md = tm.build()
    assert "（暂无成交）" in md and "（暂无持仓）" in md


def test_never_scientific_notation(payload):
    p = payload
    p["arms"][ps.ARM_A]["fills"][0]["px"] = 0.0005
    md = tm.build()
    assert "e-0" not in md and "0.0005" in md


def test_amounts_are_fixed_point(payload):
    md = tm.build()
    assert "500060.10" in md and "60.10" in md and "28500.00" in md


# ── 页面接线 ────────────────────────────────────────────────

def test_page_and_route_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "nanobot_quant" / "ashare_page.html").read_text(encoding="utf-8")
    assert 'id="btn-trades-md"' in html
    assert "'/config/ashare/trades.md?limit=50&_=' + Date.now()" in html
    src = (root / "src" / "nanobot_quant" / "ashare_handlers.py").read_text(encoding="utf-8")
    assert '@app.get("/config/ashare/trades.md")' in src
