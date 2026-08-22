"""TD 序列可视化分析页（/config/td-table）handler 测试。

覆盖：引擎映射、K 线序列计算、9 信号回溯统计、页面渲染（mock 数据源，
避免 CLI 依赖）。handler 直调（FakeRequest），与 td_params 测试同模式。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from nanobot_quant.strategies.registry import resolve_engine_cls
from nanobot_quant.strategies.td_sequential_cycle import CycleDeMarkEngine
from nanobot_quant.strategies.td_sequential_futu import FutuDeMarkEngine
from nanobot_quant.td_table_handlers import (
    _build_rows,
    _engine_run,
    _render_stats_table,
    signal_stats,
    td_table_page,
)
from tests.test_td_sequential_cycle import _falling_df as _cycle_fall  # noqa: F401


def _seq_df() -> pd.DataFrame:
    """Engine output with a completed Buy setup at the last bar + no forward bars."""
    closes = [100, 101, 102, 103, 104, 100, 99, 98, 97, 96, 95, 94, 93, 92]
    df = pd.DataFrame(
        {"Open": closes,
         "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes],
         "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    return _engine_run(df, "td_sequential", {"setup_period": 9, "compare_length": 4})


class FakeRequest:
    def __init__(self, params: dict):
        self.query_params = params


def test_resolve_engine_cls_mapping():
    from nanobot_quant.strategies.td_sequential import _DeMarkEngine
    assert resolve_engine_cls("td_sequential") is _DeMarkEngine
    assert resolve_engine_cls("td_sequential_cycle") is CycleDeMarkEngine
    assert resolve_engine_cls("td_sequential_futu") is FutuDeMarkEngine


def test_engine_run_normalises_columns():
    df = pd.DataFrame(
        {"open": [1, 2, 3], "high": [3, 4, 5], "low": [0, 1, 2],
         "close": [1, 2, 3], "volume": [10, 10, 10]},
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    out = _engine_run(df, "td_sequential_futu", {"setup_period": 9})
    assert "Close" in out.columns
    assert "buy_setup_count" in out.columns
    assert "combined_score" in out.columns


def test_signal_stats_counts_and_forward_returns():
    seq = _seq_df()
    rows, agg = signal_stats(seq, 9)
    # falling 9-bar setup completes at the last bar (count reaches 9)
    buy_rows = [r for r in rows if r["direction"] == "BUY"]
    assert len(buy_rows) == 1
    r = buy_rows[0]
    assert r["price"] == 92.0
    # no forward bars available (range end) → pct None
    assert r["pct3"] is None and r["pct5"] is None and r["pct10"] is None
    assert agg["BUY"][3] is None  # no observable signals → no stats
    assert agg["SELL"][3] is None


def test_signal_stats_win_aggregation():
    # 5 rising bars → 9 falling bars (setup at index 13, price 92) → 10 rebounds
    closes = ([100, 101, 102, 103, 104]
              + [100, 99, 98, 97, 96, 95, 94, 93, 92]
              + [93, 94, 95, 96, 97, 98, 99, 100, 101, 102])
    df = pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    seq = _engine_run(df, "td_sequential", {"setup_period": 9, "compare_length": 4})
    rows, agg = signal_stats(seq, 9)
    buy_rows = [r for r in rows if r["direction"] == "BUY"]
    assert len(buy_rows) == 1
    r = buy_rows[0]
    assert r["price"] == 92.0
    assert r["pct3"] is not None and r["pct5"] is not None and r["pct10"] is not None
    # pct3 after trigger: bar 16 close = 95
    assert r["pct3"] == pytest.approx((95 / 92 - 1) * 100)
    a = agg["BUY"][3]
    assert a["n"] == 1
    assert a["win"] == 1  # 95 > 92 → win
    assert a["rate"] == 100.0


def test_signal_stats_sell_side():
    closes = [100, 99, 98, 97, 96] + [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111]
    df = pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    seq = _engine_run(df, "td_sequential", {"setup_period": 9, "compare_length": 4})
    rows, agg = signal_stats(seq, 9)
    sell_rows = [r for r in rows if r["direction"] == "SELL"]
    assert len(sell_rows) == 1
    assert sell_rows[0]["price"] == 108.0
    assert agg["SELL"][3] is not None


def test_build_rows_highlights_signals():
    from nanobot_quant.td_table_handlers import _display
    seq = _seq_df()
    rows_html = _build_rows(_display(seq), 9)
    # last bar (count==9 → BUY) gets sig-buy class
    assert 'class="sig-buy"' in rows_html
    # signal text present
    assert "BUY (Setup Complete)" in rows_html


def test_render_stats_table_html():
    rows = [
        {"time": "2026-01-12 16:00", "direction": "BUY", "price": 93.0,
         "p3": 92.0, "pct3": -1.08, "p5": 94.0, "pct5": 1.08,
         "p10": None, "pct10": None},
    ]
    agg = {"BUY": {3: {"n": 1, "win": 0, "rate": 0.0}, 5: {"n": 1, "win": 1, "rate": 100.0},
                   10: None},
           "SELL": {3: None, 5: None, 10: None}}
    html = _render_stats_table(rows, agg)
    assert "9 信号回溯" in html
    assert "BUY" in html
    assert "100.0%" in html


def test_page_renders_with_mocked_data(monkeypatch):
    import nanobot_quant.td_table_handlers as mod

    closes = list(range(100, 105)) + list(range(100, 91, -1))  # 14 bars, 9-bar fall
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    monkeypatch.setattr(mod, "_resolve_for_table",
                        lambda ticker: {"ok": True, "chain": "solana",
                                       "address": "So11111111111111111111111111111111111111112"})

    class _FakeOnchain:
        def __init__(self, frame):
            self._df = frame

        def fetch_kline(self, ticker, bar="1D", limit=120, start=None, end=None):
            return self._df

    monkeypatch.setattr(mod, "get_data_source",
                        lambda name: _FakeOnchain(df) if name == "onchainos"
                        else mod.get_data_source(name))
    monkeypatch.setattr(mod, "load_td_params", lambda s=None: {"setup_period": 9, "compare_length": 4})
    monkeypatch.setattr(mod, "load_selected", lambda: "td_sequential")
    monkeypatch.setattr(mod, "get_strategy",
                        lambda n: type("S", (), {"label": "TD Sequential（原版）"})())

    resp = td_table_page(FakeRequest({"tab": "snapshot", "ticker": "SOL", "bar": "1D", "limit": "60"}))
    body = resp.body.decode()
    assert "TD 序列分析" in body
    assert "BUY (Setup Complete)" in body
    assert "当前策略" in body


def test_page_renders_okx_cex_source(monkeypatch):
    """OKX CEX 四平选项 + research 徽标（来源行，不参与执行）。"""
    import nanobot_quant.td_table_handlers as mod

    closes = list(range(100, 105)) + list(range(100, 91, -1))
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )

    class _FakeOkx:
        def fetch_kline(self, ticker, bar="1D", limit=120, start=None, end=None):
            return df

    monkeypatch.setattr(mod, "get_data_source",
                        lambda name: _FakeOkx() if name == "okx_cex"
                        else mod.get_data_source(name))
    monkeypatch.setattr(mod, "okx_ticker", lambda t, tokens: "XSOL-USDT")
    monkeypatch.setattr(mod, "load_tokens_json", lambda: {})
    monkeypatch.setattr(mod, "load_td_params", lambda s=None: {"setup_period": 9, "compare_length": 4})
    monkeypatch.setattr(mod, "load_selected", lambda: "td_sequential")
    monkeypatch.setattr(mod, "get_strategy",
                        lambda n: type("S", (), {"label": "TD Sequential（原版）"})())

    body = td_table_page(FakeRequest({"tab": "snapshot", "ticker": "SOL", "bar": "1D",
                                      "limit": "60", "source": "okx_cex"})).body.decode()
    assert "OKX CEX (回测/展示)" in body  # 四平选项
    assert "OKX CEX（XSOL-USDT）· 回测/展示，不参与执行" in body  # 来源行 research 徽标
    assert "Gate CEX (执行同源)" in body  # 其余选项仍在


def test_page_renders_history_with_mocked_data(monkeypatch):
    import nanobot_quant.td_table_handlers as mod
    closes = list(range(100, 105)) + list(range(100, 91, -1))
    df = pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000_000] * len(closes)},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )
    monkeypatch.setattr(mod, "_resolve_for_table",
                        lambda ticker: {"ok": True, "chain": "solana",
                                       "address": "So11111111111111111111111111111111111111112"})

    class _FakeOnchain:
        def __init__(self, frame):
            self._df = frame

        def fetch_kline(self, ticker, bar="1D", limit=120, start=None, end=None):
            return self._df

    monkeypatch.setattr(mod, "get_data_source",
                        lambda name: _FakeOnchain(df) if name == "onchainos"
                        else mod.get_data_source(name))
    monkeypatch.setattr(mod, "load_td_params", lambda s=None: {"setup_period": 9, "compare_length": 4})
    monkeypatch.setattr(mod, "load_selected", lambda: "td_sequential")
    monkeypatch.setattr(mod, "get_strategy",
                        lambda n: type("S", (), {"label": "TD Sequential（原版）"})())

    resp = td_table_page(FakeRequest({"tab": "history", "ticker": "SOL", "bar": "1D",
                                      "start": "2026-01-01", "end": "2026-01-31"}))
    body = resp.body.decode()
    assert "9 信号回溯" in body
    assert "胜率" in body
    assert "BUY" in body


def test_page_resolve_error_banner(monkeypatch):
    import nanobot_quant.td_table_handlers as mod

    monkeypatch.setattr(mod, "_resolve_for_table",
                        lambda ticker: {"ok": False, "issue": "not supported on solana chain",
                                       "category": "not_found"})
    monkeypatch.setattr(mod, "load_td_params", lambda s=None: {"setup_period": 9, "compare_length": 4})
    monkeypatch.setattr(mod, "load_selected", lambda: "td_sequential")
    monkeypatch.setattr(mod, "get_strategy",
                        lambda n: type("S", (), {"label": "TD Sequential（原版）"})())

    resp = td_table_page(FakeRequest({"tab": "snapshot", "ticker": "XYZZY", "bar": "1D"}))
    body = resp.body.decode()
    assert "标的解析失败" in body
    assert "not_found" in body
def test_trade_rows_filter(monkeypatch):
    """交易记录过滤：只含成交事件、最新在前、查询条件生效（方案 B）。"""
    from nanobot_quant.td_table_handlers import _trade_rows

    events = [
        {"ts": "t1", "symbol": "CRCLX", "event": "SKIP",
         "note": "无可用资金 slot"},
        {"ts": "t2", "symbol": "SPCX", "event": "LONG",
         "slot": 2, "qty": 0.021226, "price": 136.8, "direction": "buy",
         "status": "ok", "tx_hash": "aa11"},
        {"ts": "t3", "symbol": "CRCLX", "event": "BUY_FAIL",
         "slot": 3, "qty": 0.04, "direction": "buy", "status": "fail"},
        {"ts": "t4", "symbol": "RENDER", "event": "HOLD"},
    ]
    rows = _trade_rows(events)
    # SKIP/HOLD 不是交易事件；最新在前（BUY_FAIL 先于 LONG）
    assert [r["event"] for r in rows] == ["BUY_FAIL", "LONG"]
    assert rows[0]["symbol"] == "CRCLX"
    assert rows[1]["qty"] == 0.021226
    assert rows[1]["tx_hash"] == "aa11"

    # 查询条件：只查 SPCX
    rows_spcx = _trade_rows(events, {"tq_sym": "spcx"})
    assert [r["event"] for r in rows_spcx] == ["LONG"]

    # 查询条件：只查失败
    rows_fail = _trade_rows(events, {"tq_st": "fail"})
    assert [r["event"] for r in rows_fail] == ["BUY_FAIL"]

    # 查询条件：只查买入方向
    rows_buy = _trade_rows(events, {"tq_dir": "buy"})
    assert [r["event"] for r in rows_buy] == ["BUY_FAIL", "LONG"]

    # 空事件流
    assert _trade_rows([]) == []


def test_render_live_contains_trade_section(monkeypatch, tmp_path: Path):
    """实时监控 tab 渲染包含「📊 交易记录」区块与查询表单。"""
    from nanobot_quant import td_live_state
    from nanobot_quant.td_table_handlers import _render_live

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)
    td_live_state.append_event({
        "symbol": "SPCX", "event": "LONG", "note": "slot=2 qty=0.021226 price=136.8",
        "slot": 2, "qty": 0.021226, "price": 136.8, "direction": "buy",
        "status": "ok", "tx_hash": "4xKd9aBcDEfGhIjKlMnOpQrStUvWxYz0123456789abc", "chain": "solana",
    })
    td_live_state.append_event({
        "symbol": "CRCLX", "event": "BUY_FAIL", "note": "slot=3",
        "slot": 3, "qty": 0.04, "direction": "buy", "status": "fail",
    })
    html = _render_live(with_script=False, tq={})
    assert "📊 交易记录" in html
    assert "trade-form" in html
    assert "tq_sym" in html
    assert "tq_n" in html
    assert "原因" in html                    # 原因列（失败原因/成交明细）
    assert "4xKd9aBc" in html                # tx_hash 短显
    assert "BUY_FAIL" in html
    assert "🟢 买" in html
    assert "✅" in html and "❌" in html
    # 原因列内容：成功事件显示 slot/qty/price 明细
    assert "slot=2 qty=0.021226 price=136.8" in html
    # tx_hash 可点击（solscan 链接）
    assert 'href="https://solscan.io/tx/4xKd9aBcDEfGhIjKlMnOpQrStUvWxYz0123456789abc"' in html
    assert "↗" in html


def test_tx_cell_link_and_placeholder():
    """tx_hash 单元格：真实 hash → 链浏览器链接；占位 UUID/空 → 纯文本。"""
    from nanobot_quant.td_table_handlers import _tx_cell

    # 空 → 纯文本占位
    assert "muted" in _tx_cell("")
    # 32 位 hex 占位 UUID → 不生成链接
    ph = _tx_cell("9f3d2a1b4c5d6e7f8a9b0c1d2e3f4a5b")
    assert "<a" not in ph
    assert "9f3d2a1b" in ph
    # 真实 base58 hash → solscan 链接（默认链）
    tx = "4xKd9aBcDEfGhIjKlMnOpQrStUvWxYz0123456789abcdefghijk"
    cell = _tx_cell(tx)
    assert f'href="https://solscan.io/tx/{tx}"' in cell
    assert "↗" in cell
    # 链映射：bnb → bscscan
    cell_bnb = _tx_cell(tx, "bnb")
    assert f'href="https://bscscan.com/tx/{tx}"' in cell_bnb
    # 链归一化："solana" 与 "sol" 都能匹配 solscan
    assert "solscan.io" in _tx_cell(tx, "solana")
    assert "solscan.io" in _tx_cell(tx, "sol")


def test_render_live_scene_blocks(monkeypatch, tmp_path: Path):
    """B3（2026-08-21 方案 B）：实时监控 tab 三场景分区并列。

    - 每个启用场景一个独立信号表区块（标题含场景名/周期/标的数/状态）
    - 同标的不同场景数据互不串（LIVE_STATE 嵌套键）
    - per-scene 阈值高亮（high entry=6 时 setup_buy=6 为绿、=5 为橙）
    - 交易记录/信号历史表单含场景过滤下拉（tq_scene/sq_scene）
    """
    from nanobot_quant import exec_params as _ep
    from nanobot_quant import td_live_state
    from nanobot_quant.td_table_handlers import _render_live

    # 覆盖 autouse 路径隔离：直接注入带 scenes 的配置。
    # 注意：td_table_handlers 在模块级 `from ... import load_exec_params` 绑定
    # 了原函数，monkeypatch 必须替换 td_table_handlers 模块内的名字。
    scenes = {
        "high": {"enabled": True, "sleeptime": "1m",
                 "entry_setup": 6, "exit_setup": 6, "exit_countdown": 13,
                 "sub_accounts": ["gate_bot1", "gate_bot2"]},
        "mid": {"enabled": True, "sleeptime": "5m",
                 "entry_setup": 9, "exit_setup": 6, "exit_countdown": 13,
                 "sub_accounts": ["gate_bot3"]},
        "low": {"enabled": False, "sleeptime": "1D",
                 "entry_setup": 9, "exit_setup": 6, "exit_countdown": 13},
    }
    monkeypatch.setattr("nanobot_quant.td_table_handlers.load_exec_params",
                        lambda: {"execution_channel": "gate", "scenes": scenes})

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)
    td_live_state.update_symbol("SOL", {"setup_buy": 9, "price": 89.4, "time": "11:00",
                                "signal": "LONG"}, scene="high")
    td_live_state.update_symbol("SOL", {"setup_buy": 2, "price": 89.1, "time": "11:00"},
                                scene="mid")
    td_live_state.set_loop(True, next_iteration="11:01:00")

    html = _render_live(with_script=False, tq={})
    # 启用场景区块（high/mid）出现，停用场景（low）不出现
    # （注意：过滤下拉含 low 选项属预期，区块标题格式为「🐢 低频 <span …low」）
    assert "📈 高频" in html and "high" in html
    assert "📊 中频" in html and "mid" in html
    assert "🐢 低频 <span" not in html
    # 同标的两场景数据各归其区（两行 SOL，setup 分别 9 和 2）
    assert html.count("<b>SOL</b>") == 2
    # per-scene 阈值高亮：high 场景 entry=6 → setup_buy=9 绿（>=6）；
    # mid 场景 entry=9 → setup_buy=2 普通文本（无高亮）；信号列各归其位
    assert 'color:#1b7f3d">9</b>' in html  # high 行 setup_buy 绿
    assert "sig buy" in html  # high 行信号 LONG
    assert "sig hold" in html  # mid 行信号 HOLD
    # 场景过滤下拉
    assert 'name="tq_scene"' in html and 'name="sq_scene"' in html
    assert "全部场景" in html
    # 场景卡片标题含 slot↔子账号映射（2026-08-22 方案 A）
    assert "slot 1-2 (gate_bot1-2)" in html
    assert "slot 1 (gate_bot3)" in html


def test_slot_map_txt():
    """slot↔子账号映射文本（2026-08-22）。"""
    from nanobot_quant.td_table_handlers import _slot_map_txt

    assert _slot_map_txt(["gate_bot1", "gate_bot2"]) == "slot 1-2 (gate_bot1-2)"
    assert _slot_map_txt(["gate_bot1", "gate_bot2", "gate_bot3"]) == "slot 1-3 (gate_bot1-3)"
    assert _slot_map_txt(["gate_bot1", "gate_bot3"]) == "slot 1-2 (gate_bot1, gate_bot3)"
    assert _slot_map_txt(["gate_bot3"]) == "slot 1 (gate_bot3)"
    assert _slot_map_txt([]) == ""
    assert _slot_map_txt(["my_bot", "other"]) == "slot 1-2 (my_bot, other)"


def test_render_live_scene_columns(monkeypatch, tmp_path: Path):
    """信号历史/交易记录场景列（2026-08-22）。

    - 表头含「场景」列（交易记录与信号历史均位于时间列后）
    - 事件行显示 scene 值；旧事件（无 scene）显示 —
    """
    from nanobot_quant import td_live_state
    from nanobot_quant.td_table_handlers import _render_live

    ev_file = tmp_path / "td_live_events.jsonl"
    monkeypatch.setattr(td_live_state, "events_path", lambda: ev_file)
    td_live_state.append_event({
        "symbol": "CRCLX", "event": "LONG", "scene": "high",
        "note": "slot=2 qty=0.045 price=87.99", "slot": 2, "qty": 0.045,
        "price": 87.99, "direction": "buy", "status": "ok", "chain": "solana",
    })
    td_live_state.append_event({
        "symbol": "SOL", "event": "SKIP", "scene": "mid",
        "note": "无可用资金 slot", "price": 93.99, "score": 26.9,
    })
    td_live_state.append_event({
        "symbol": "RENDER", "event": "EXIT", "note": "slot=3 旧事件",
        "slot": 3, "qty": 3.07, "price": 1.33, "direction": "sell",
        "status": "ok", "chain": "solana",
    })
    html = _render_live(with_script=False, tq={})
    # 表头含场景列（时间后）
    assert "<th>时间</th><th>场景</th>" in html            # 信号历史
    assert "<th>tx_hash</th><th>时间</th><th>场景</th>" in html  # 交易记录
    # 信号历史行场景值
    assert ">high<" in html and ">mid<" in html
    # 交易记录：LONG(high) 场景值 + 旧事件 EXIT 显示 —
    assert ">—<" in html


def test_trade_rows_scene_filter():
    """交易记录按场景过滤（B2：事件带 scene 字段，旧事件无 scene 归「全部」）。"""
    from nanobot_quant.td_table_handlers import _trade_rows

    events = [
        {"symbol": "SOL", "event": "LONG", "scene": "high",
         "slot": 2, "qty": 0.04, "direction": "buy", "status": "ok"},
        {"symbol": "SPYX", "event": "LONG", "scene": "mid",
         "slot": 2, "qty": 0.005, "direction": "buy", "status": "ok"},
        {"symbol": "SPCX", "event": "LONG",  # 旧事件（无 scene 字段）
         "slot": 1, "qty": 0.02, "direction": "buy", "status": "ok"},
    ]
    all_rows = _trade_rows(events, {})
    assert len(all_rows) == 3
    high = _trade_rows(events, {"tq_scene": "high"})
    assert [r["symbol"] for r in high] == ["SOL"]
    mid = _trade_rows(events, {"tq_scene": "mid"})
    assert [r["symbol"] for r in mid] == ["SPYX"]
    # 旧事件（无 scene）在指定场景过滤下不可见，只在「全部」可见
    assert _trade_rows(events, {"tq_scene": "low"}) == []
