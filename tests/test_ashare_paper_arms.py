"""模拟账本两臂（PR 3/5）单测 —— D 方案出场规则 + 两臂账本隔离。

覆盖：``should_close_high9`` 四象限、配置默认关、臂 B 惰性创建、
读路径不写盘、两臂独立账本、一轮双跑、盘口一轮只取一次、重置清臂 B、
页面开关与回放脚本接线（结构性锁定）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from nanobot_quant.ashare import paper_ledger as pl
from nanobot_quant.ashare import paper_runner as pr
from nanobot_quant.ashare import paper_strategy as ps

CN = timezone(timedelta(hours=8))
SYM = "510050.SH"


@pytest.fixture(autouse=True)
def _tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(pr.creg, "_get_storage_dir", lambda: str(tmp_path))
    yield tmp_path


def _frame(buy: list[int], closes: list[float], *, sell: list[int] | None = None,
           start_min: int = 0) -> pd.DataFrame:
    n = len(buy)
    base = datetime(2026, 11, 25, 9, 30) + timedelta(minutes=start_min)
    sell = sell or [0] * n
    rows = [{
        "dt": base + timedelta(minutes=i),
        "date": "2026-11-25",
        "open": c, "high": c, "low": c, "close": c, "volume": 100,
        "buy_setup_count": b, "sell_setup_count": s,
    } for i, (b, c, s) in enumerate(zip(buy, closes, sell))]
    return pd.DataFrame(rows)


# ── D 方案出场规则（四象限 + DTE 兜底） ──────────────────────

def test_high9_needs_both_high9_and_profit():
    short = {"entry_px": 0.10, "expiry": "20261223"}
    today = date(2026, 11, 25)

    ok, why = ps.should_close_high9(short, 0.07, today, sell_setup=9)
    assert ok is True and why.startswith("high9_profit")

    ok, why = ps.should_close_high9(short, 0.13, today, sell_setup=9)   # 高9 但浮亏
    assert ok is False and why == ""

    ok, why = ps.should_close_high9(short, 0.07, today, sell_setup=3)   # 浮盈但非高9
    assert ok is False and why == ""

    ok, _ = ps.should_close_high9(short, 0.07, today, sell_setup=8)
    assert ok is False


def test_high9_force_closes_on_dte_even_when_losing():
    """DTE 强平是时间维度退出、不是止损 —— 浮亏也平，两臂都保留这条。"""
    today = date(2026, 11, 25)
    expiry = (today + timedelta(days=3)).strftime("%Y%m%d")
    short = {"entry_px": 0.10, "expiry": expiry}
    ok, why = ps.should_close_high9(short, 0.25, today, sell_setup=0)
    assert ok is True and why.startswith("dte_force")


def test_high9_ignores_missing_entry_price():
    short = {"entry_px": 0, "expiry": "20261223"}
    ok, why = ps.should_close_high9(short, 0.01, date(2026, 11, 25), sell_setup=9)
    assert ok is False and why == ""


def test_arm_a_rule_unchanged():
    """臂 A 走的还是原来的止盈/DTE —— 不得被 PR 3 改变。"""
    today = date(2026, 11, 25)
    short = {"entry_px": 0.10, "expiry": "20261223"}
    ok, why = ps.should_close(short, 0.04, today, ps.PaperParams())
    assert ok is True and why.startswith("take_profit")


# ── 配置 ──────────────────────────────────────────────────

def test_second_arm_defaults_off():
    assert pr.load_config()["second_arm"] is False


def test_second_arm_accepts_bool_and_string():
    assert pr.save_config(second_arm=True)["second_arm"] is True
    assert pr.load_config()["second_arm"] is True
    assert pr.save_config(second_arm="false")["second_arm"] is False


# ── 臂 B 惰性创建 / 读不写 ────────────────────────────────

def test_arm_b_is_created_lazily_and_stored_separately():
    st = pr.load_state()
    assert "arm_b" not in st
    lstate, meta = pr._ensure_arm(st, ps.ARM_B)
    assert isinstance(lstate.get("shorts"), list)
    led = pl.PaperLedger.from_state(lstate)
    pr._store_arm(st, ps.ARM_B, led, {"rounds": 3})
    assert st["arm_b"]["meta"]["rounds"] == 3
    # 臂 A 仍在顶层，未被覆盖
    assert isinstance(st["ledger"], dict)


def test_state_payload_does_not_create_arm_b(_tmp_storage):
    payload = pr.state()
    assert set(payload["arms"]) == {ps.ARM_A, ps.ARM_B}
    assert payload["arms"][ps.ARM_A]["active"] is True
    assert payload["arms"][ps.ARM_B]["active"] is False
    assert "arm_b" not in pr.load_state()      # 读路径不写盘


# ── 一轮双臂 ──────────────────────────────────────────────

def _fake_market(monkeypatch, *, sell_counts=None):
    expiry = (date.today() + timedelta(days=10)).strftime("%Y%m%d")
    df = _frame([8, 9], [2.86, 2.85], sell=sell_counts or [0, 0])
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: df)
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)
    monkeypatch.setattr(pr.ac, "contracts", lambda *a, **k: {"items": [
        {"code": "10007000.SH", "undl": SYM, "right": "P",
         "strike": 2.70, "expire": expiry}]})
    monkeypatch.setattr(pr.tape, "fetch_quotes",
                        lambda codes, timeout=12.0: (
                            [{"code": c, "bid": 0.03, "ask": 0.032} for c in codes], 0))
    return df


def test_round_runs_both_arms_with_independent_ledgers(monkeypatch):
    _fake_market(monkeypatch)
    cfg = pr.save_config(second_arm=True)

    out = pr.PaperRunner().do_round(cfg)
    assert set(out["arms"]) == {ps.ARM_A, ps.ARM_B}
    for arm in (ps.ARM_A, ps.ARM_B):
        assert any(a["action"] == "sell_open" for a in out["arms"][arm]["actions"]), arm

    st = pr.load_state()
    a = pl.PaperLedger.from_state(st["ledger"])
    b = pl.PaperLedger.from_state(st["arm_b"]["ledger"])
    assert "10007000.SH" in a.shorts and "10007000.SH" in b.shorts
    # 两本账各自记账：A 的现金只被扣一次，不会因 B 再扣一次
    assert a.cash == pytest.approx(b.cash)
    assert a.cash == pytest.approx(a.initial_cash + 0.03 * 10_000 - a.fills[0]["fee"])


def test_second_arm_off_keeps_single_arm_behavior(monkeypatch):
    _fake_market(monkeypatch)
    cfg = pr.load_config()
    assert cfg["second_arm"] is False
    out = pr.PaperRunner().do_round(cfg)
    assert set(out["arms"]) == {ps.ARM_A}
    assert "arm_b" not in pr.load_state()


def test_arm_b_holds_when_high9_arrives_in_loss(monkeypatch):
    """高9 但浮亏 → 臂 B 不平；臂 A 的权利金止盈照旧生效。"""
    expiry = (date.today() + timedelta(days=10)).strftime("%Y%m%d")
    led = pl.PaperLedger(initial_cash=100_000)
    led.sell_open(code="10007000.SH", underlying=SYM, strike=2.70, expiry=expiry,
                  qty=1, px=0.10, ts="2026-11-25T09:31:00")
    led_b = pl.PaperLedger(initial_cash=100_000)
    led_b.sell_open(code="10007000.SH", underlying=SYM, strike=2.70, expiry=expiry,
                    qty=1, px=0.10, ts="2026-11-25T09:31:00")
    st = pr.load_state()
    st["ledger"] = led.to_state()
    st["arm_b"] = {"ledger": led_b.to_state(),
                   "meta": {"last_bar": {}, "wave_open": {SYM: True}, "rounds": 0}}
    pr.save_state(st)

    # mark 0.13 → 浮亏 30%；同时 sell_setup=9
    df = _frame([1, 2], [2.85, 2.85], sell=[9, 9])
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: df)
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)
    monkeypatch.setattr(pr.tape, "fetch_quotes",
                        lambda codes, timeout=12.0: (
                            [{"code": c, "bid": 0.12, "ask": 0.13} for c in codes], 0))

    out = pr.PaperRunner().do_round(pr.save_config(second_arm=True))
    a_actions = [a["action"] for a in out["arms"][ps.ARM_A]["actions"]]
    b_actions = [a["action"] for a in out["arms"][ps.ARM_B]["actions"]]
    # 臂 A：0.13 vs entry 0.10 → 涨 30%，不触发 50% 止盈 → 也不平
    assert "buy_close" not in a_actions
    assert "buy_close" not in b_actions
    assert pl.PaperLedger.from_state(pr.load_state()["arm_b"]["ledger"]).shorts


def test_quote_cache_hits_network_once_per_round():
    calls: list[list[str]] = []

    class _Fake:
        @staticmethod
        def fetch_quotes(codes, timeout=12.0):
            calls.append(list(codes))
            return [{"code": c, "bid": 1.0, "ask": 1.1} for c in codes], 0

    import types
    fake = types.SimpleNamespace(fetch_quotes=_Fake.fetch_quotes)
    original = pr.tape
    pr.tape = fake
    try:
        cache: dict = {}
        pr._cached_quotes(cache, ["A", "B"])
        pr._cached_quotes(cache, ["B", "C"])
        assert calls == [["A", "B"], ["C"]]      # 第二轮只补 C
        # 取不到的 code 也占位，不会反复重试
        pr._cached_quotes(cache, ["A", "B", "C"])
        assert calls == [["A", "B"], ["C"]]
    finally:
        pr.tape = original


def test_reset_clears_second_arm(monkeypatch):
    _fake_market(monkeypatch)
    pr.PaperRunner().do_round(pr.save_config(second_arm=True))
    assert "arm_b" in pr.load_state()

    pr.reset_state()
    st = pr.load_state()
    assert "arm_b" not in st
    # config.second_arm 仍是 True（配置与账本是两层），但账本已回初始资金
    payload = pr.state()
    assert payload["arms"][ps.ARM_B]["ledger"]["shorts"] == []
    assert payload["arms"][ps.ARM_B]["ledger"]["initial_cash"] == pytest.approx(
        payload["config"]["initial_cash"])


# ── 页面 / 回放脚本接线（结构性） ────────────────────────────

def test_page_has_second_arm_switch_and_arm_table():
    import nanobot_quant
    from pathlib import Path
    html = (Path(nanobot_quant.__file__).parent / "ashare_page.html").read_text(encoding="utf-8")
    assert 'id="pp-arm-b"' in html
    assert 'id="pp-arms"' in html
    assert "renderPaperArms" in html
    assert "second_arm" in html
    assert "臂 B（D 方案：高9 + 浮盈）" in html


def test_replay_cli_supports_both_arms():
    """CLI 应是薄封装：--arm 与两臂逻辑都在模块里（页面与 CLI 同源）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "ashare_paper_replay.py").read_text(encoding="utf-8")
    mod = (root / "src" / "nanobot_quant" / "ashare" /
           "paper_replay.py").read_text(encoding="utf-8")
    assert "paper_replay import main" in script          # 脚本只转调，不重复实现
    assert "--arm" in mod                                 # 选项在模块
    assert "should_close_high9" in mod                    # 臂 B 出场在模块
    assert "ARM_B" in mod
