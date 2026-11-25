"""模拟账本 runner（PR 2/5）单测 —— 全离线，不碰网络与真实存储。

覆盖：账本状态 round-trip、真实盘口价覆盖、低9 首穿与去重、同浪门、
非交易时段跳过、到期结算、止盈买回、端到端一轮落盘。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from datetime import timezone

import pandas as pd
import pytest

from nanobot_quant.ashare import paper_ledger as pl
from nanobot_quant.ashare import paper_runner as pr
from nanobot_quant.ashare import paper_strategy as ps

CN = timezone(timedelta(hours=8))
SYM = "510050.SH"


@pytest.fixture(autouse=True)
def _tmp_storage(tmp_path, monkeypatch):
    """所有读写都落到 tmp，绝不碰真实 /data/legion/credentials。"""
    monkeypatch.setattr(pr.creg, "_get_storage_dir", lambda: str(tmp_path))
    yield tmp_path


def _frame(counts: list[int], closes: list[float], day: str = "2026-11-25",
           start_min: int = 0) -> pd.DataFrame:
    """构造已切分好的 1m 帧（只保留 runner 用到的列）。"""
    n = len(counts)
    base = datetime(2026, 11, 25, 9, 30) + timedelta(minutes=start_min)
    rows = [{
        "dt": base + timedelta(minutes=i),
        "date": day,
        "open": c, "high": c, "low": c, "close": c, "volume": 100,
        "buy_setup_count": k, "sell_setup_count": 0,
    } for i, (k, c) in enumerate(zip(counts, closes))]
    return pd.DataFrame(rows)


# ── 配置 ──────────────────────────────────────────────────

def test_config_defaults_are_off_and_510050_only():
    cfg = pr.load_config()
    assert cfg["enabled"] is False
    assert cfg["symbols"] == ["510050.SH"]
    assert cfg["interval_s"] == 60
    assert cfg["params"]["max_per_wave"] == 1


def test_config_rejects_unknown_symbol(_tmp_storage):
    with pytest.raises(ValueError, match="不支持的标的"):
        pr.save_config(symbols=["999999.SH"])


def test_config_clamps_interval_and_persists(_tmp_storage):
    cfg = pr.save_config(interval_s=1)
    assert cfg["interval_s"] == pr.INTERVAL_RANGE[0]
    assert json.loads((pr.paper_dir() / "config.json").read_text())["interval_s"] == 30


# ── 账本状态 round-trip ──────────────────────────────────

def test_ledger_state_roundtrip_preserves_positions_and_fills():
    led = pl.PaperLedger(initial_cash=100_000)
    led.sell_open(code="10007000.SH", underlying=SYM, strike=2.70, expiry="20261223",
                  qty=1, px=0.03, ts="2026-11-25T10:00:00")
    led.mark_short("10007000.SH", 0.02)
    st = json.loads(json.dumps(led.to_state()))       # 过一遍 JSON，模拟落盘

    back = pl.PaperLedger.from_state(st)
    assert back.cash == pytest.approx(led.cash)
    assert list(back.shorts) == ["10007000.SH"]
    assert back.shorts["10007000.SH"].entry_px == pytest.approx(0.03)
    assert back.shorts["10007000.SH"].mark_px == pytest.approx(0.02)
    assert len(back.fills) == 1
    assert back.check_consistency()["ok"] is True


def test_ledger_state_skips_malformed_rows():
    st = {"initial_cash": 1000, "cash": 1000, "shorts": [
        {"code": "X", "strike": 2.0},                       # 缺 qty/entry_px
        {"code": "Y", "underlying": SYM, "strike": 2.0, "expiry": "20261223",
         "qty": 1, "entry_px": 0.05, "entry_ts": "t"},
    ], "etf": [{"underlying": SYM}], "fills": [{"action": "x"}]}
    led = pl.PaperLedger.from_state(st)
    assert list(led.shorts) == ["Y"]
    assert led.etf == {}


# ── 盘口价优先 ────────────────────────────────────────────

def test_sell_open_uses_real_bid_over_bar_approximation():
    led = pl.PaperLedger(initial_cash=100_000)
    r = led.sell_open(code="A", underlying=SYM, strike=2.7, expiry="20261223",
                      qty=1, bar_close=0.01, px=0.05, ts="t")
    assert r["ok"] is True
    assert r["fill"]["px"] == pytest.approx(0.05)        # 不叠 tick、不用 bar_close
    assert led.shorts["A"].entry_px == pytest.approx(0.05)


def test_buy_close_uses_real_ask():
    led = pl.PaperLedger(initial_cash=100_000)
    led.sell_open(code="A", underlying=SYM, strike=2.7, expiry="20261223",
                  qty=1, px=0.05, ts="t")
    r = led.buy_close(code="A", px=0.02, ts="t2")
    assert r["ok"] is True
    assert r["fill"]["px"] == pytest.approx(0.02)
    assert led.shorts == {}


# ── 信号 ──────────────────────────────────────────────────

def test_first_cross_detects_9_and_dedupes_same_bar():
    runner = pr.PaperRunner()
    df = _frame([8, 9], [2.86, 2.85])
    last_bar: dict = {}
    sig = runner._first_cross(df, last_bar, SYM)
    assert sig is not None and sig["count"] == 9
    assert runner._first_cross(df, last_bar, SYM) is None   # 同一根 bar 不重复触发


def test_first_cross_ignores_accumulation_after_9():
    runner = pr.PaperRunner()
    df = _frame([9, 10, 11], [2.85, 2.84, 2.83])
    assert runner._first_cross(df, {}, SYM) is None         # 累加期不算首穿


def test_first_cross_ignores_below_threshold():
    runner = pr.PaperRunner()
    df = _frame([7, 8], [2.87, 2.86])
    assert runner._first_cross(df, {}, SYM) is None


# ── 一轮 ──────────────────────────────────────────────────

def test_round_skips_outside_session(monkeypatch):
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: False)
    out = pr.PaperRunner().do_round(pr.load_config())
    assert out.get("skipped") == "非交易时段"


def test_round_opens_position_on_low9_and_persists(monkeypatch):
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    df = _frame([8, 9], [2.86, 2.85])
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: df)
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)
    expiry = (date.today() + timedelta(days=10)).strftime("%Y%m%d")
    monkeypatch.setattr(pr.ac, "contracts", lambda *a, **k: {"items": [
        {"code": "10007000.SH", "name": "510050P2.700", "undl": SYM,
         "right": "P", "strike": 2.70, "expire": expiry},
    ]})
    monkeypatch.setattr(pr.tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([{"code": c, "bid": 0.03, "ask": 0.032}
                                                      for c in codes], 0))

    out = pr.PaperRunner().do_round(pr.load_config())
    actions = [a["action"] for a in out["actions"]]
    assert "sell_open" in actions

    st = pr.load_state()
    led = pr.ledger_of(st)
    assert "10007000.SH" in led.shorts
    assert led.shorts["10007000.SH"].entry_px == pytest.approx(0.03)


def test_round_wave_gate_blocks_second_open_same_symbol(monkeypatch):
    """同浪门：本浪已开过仓时，同一标的再出低9 也只 SKIP。"""
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    expiry = (date.today() + timedelta(days=10)).strftime("%Y%m%d")
    monkeypatch.setattr(pr.ac, "contracts", lambda *a, **k: {"items": [
        {"code": "10007000.SH", "undl": SYM, "right": "P",
         "strike": 2.70, "expire": expiry}]})
    monkeypatch.setattr(pr.tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([{"code": c, "bid": 0.03, "ask": 0.032}
                                                      for c in codes], 0))

    frames = [(_frame([8, 9], [2.86, 2.85]), ),
              (_frame([8, 9], [2.85, 2.84], start_min=2), )]
    it = iter(frames)
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: next(it)[0])
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)

    runner = pr.PaperRunner()
    out1 = runner.do_round(pr.load_config())
    assert any(a["action"] == "sell_open" for a in out1["actions"])

    # 第二根 bar 也到 9，但同一浪已开仓 → 只 SKIP
    out2 = runner.do_round(pr.load_config())
    assert any(a["action"] == "skip" and "同浪" in a.get("reason", "")
               for a in out2["actions"])
    assert sum(1 for f in pr.ledger_of(pr.load_state()).fills
               if f["action"] == "sell_open") == 1


def test_round_take_profit_buys_back_with_ask(monkeypatch):
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    expiry = (date.today() + timedelta(days=10)).strftime("%Y%m%d")

    # 先摆一张 0.10 的持仓（权利金已回落 60% → 触发 50% 止盈）
    led = pl.PaperLedger(initial_cash=100_000)
    led.sell_open(code="10007000.SH", underlying=SYM, strike=2.70, expiry=expiry,
                  qty=1, px=0.10, ts="2026-11-25T09:31:00")
    st = pr.load_state()
    st["ledger"] = led.to_state()
    pr.save_state(st)

    df = _frame([1, 2], [2.85, 2.85])
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: df)
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)
    monkeypatch.setattr(pr.tape, "fetch_quotes",
                        lambda codes, timeout=12.0: ([{"code": c, "bid": 0.038, "ask": 0.041}
                                                      for c in codes], 0))

    out = pr.PaperRunner().do_round(pr.load_config())
    closes = [a for a in out["actions"] if a["action"] == "buy_close"]
    assert closes and closes[0]["px_src"] == "quote"
    assert closes[0]["detail"]["px"] == pytest.approx(0.041)   # 吃 ask，不是 mid
    assert pr.ledger_of(pr.load_state()).shorts == {}


def test_round_settles_expired_otm(monkeypatch):
    monkeypatch.setattr(pr.tape, "in_session", lambda now=None: True)
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
    led = pl.PaperLedger(initial_cash=100_000)
    led.sell_open(code="10007000.SH", underlying=SYM, strike=2.60, expiry=yesterday,
                  qty=1, px=0.05, ts="t")
    st = pr.load_state()
    st["ledger"] = led.to_state()
    pr.save_state(st)

    df = _frame([1, 2], [2.85, 2.85])          # 标的 2.85 > 2.60 → OTM 作废
    monkeypatch.setattr(pr.tp, "fetch_klines", lambda **kw: df)
    monkeypatch.setattr(pr.tp, "drop_unclosed", lambda d, s, now=None: d)
    monkeypatch.setattr(pr.tp, "setup_by_session", lambda d: d)
    monkeypatch.setattr(pr.tape, "fetch_quotes", lambda codes, timeout=12.0: ([], 0))

    out = pr.PaperRunner().do_round(pr.load_config())
    assert any(a["action"] == "settle" for a in out["actions"])
    back = pr.ledger_of(pr.load_state())
    assert back.shorts == {}
    assert back.realized() > 0                 # 权利金全收


def test_state_payload_has_label_and_consistency(_tmp_storage):
    payload = pr.state()
    assert payload["label"] == "模拟账本（不接券商）"
    assert payload["consistency"]["ok"] is True
    assert payload["config"]["enabled"] is False


def test_reset_state_restores_initial_cash(_tmp_storage):
    st = pr.load_state()
    led = pr.ledger_of(st)
    led.cash -= 1000
    st["ledger"] = led.to_state()
    pr.save_state(st)

    pr.reset_state()
    assert pr.ledger_of(pr.load_state()).cash == pytest.approx(
        pr.load_config()["initial_cash"])


# ── 页面与接线（结构性锁定，防「代码有、页面没有」） ──────────

def test_page_has_paper_block_marked_as_not_live():
    import nanobot_quant
    from pathlib import Path
    html = (Path(nanobot_quant.__file__).parent / "ashare_page.html").read_text(encoding="utf-8")
    assert "模拟账本（不接券商）" in html
    assert "100% 全损担保" in html
    for path in ("/config/ashare/paper/state", "/config/ashare/paper/save",
                 "/config/ashare/paper/round", "/config/ashare/paper/reset"):
        assert path in html, path


def test_handlers_register_paper_routes():
    import nanobot_quant.ashare_handlers as h
    from pathlib import Path
    src = Path(h.__file__).read_text(encoding="utf-8")
    for path in ("/config/ashare/paper/state", "/config/ashare/paper/save",
                 "/config/ashare/paper/round", "/config/ashare/paper/reset"):
        assert f'@app.get("{path}")' in src or f'@app.post("{path}")' in src, path
    # 启动时同步（enabled=true 自动恢复）
    assert "pr.sync()" in src
