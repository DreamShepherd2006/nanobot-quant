"""C120 / C127 / C129：展示层与日志口径修复的回归用例。

三条都不是交易决策改动，但都曾造成「页面/日志与执行口径不一致」，属于
本项目反复强调的硬规矩（页面显示必须等于实际执行），因此用测试锁住。
"""
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nanobot_quant"


# ── C127：跨所提示按执行通道数据源判定，不硬编码交易所 ────────────────
def test_exec_source_follows_execution_channel(monkeypatch):
    from nanobot_quant import backtest_handlers as bh

    monkeypatch.setattr(
        "nanobot_quant.exec_params.load_exec_params",
        lambda *a, **k: {"execution_channel": "okx_cex"},
    )
    assert bh._exec_source_name() == "okx_cex"

    monkeypatch.setattr(
        "nanobot_quant.exec_params.load_exec_params",
        lambda *a, **k: {"execution_channel": "gate"},
    )
    assert bh._exec_source_name() == "gate_cex"


def test_exec_source_falls_back_when_params_unreadable(monkeypatch):
    """读不到 exec_params 时回退 gate_cex——回退 okx_cex 会让跨所提示永远失效。"""
    from nanobot_quant import backtest_handlers as bh

    def _boom(*a, **k):
        raise RuntimeError("io error")

    monkeypatch.setattr("nanobot_quant.exec_params.load_exec_params", _boom)
    assert bh._exec_source_name() == "gate_cex"


def test_backtest_page_cross_venue_banner_uses_exec_source():
    html = (SRC / "backtest_page.html").read_text(encoding="utf-8")
    assert "const EXEC_SOURCE = __EXEC_SOURCE__;" in html
    assert 'banner.classList.toggle("hide", sameVenue)' in html
    # 旧实现把「选 OKX」当同源（方向写反）
    assert 'banner.classList.toggle("hide", !isOkx)' not in html
    assert "__EXEC_SOURCE__" in (
        SRC / "backtest_handlers.py"
    ).read_text(encoding="utf-8")


# ── C129：启动期 broker 与运行期 slot broker 同一计价币 ────────────────
def test_exec_quote_reads_quote_ccy_for_okx_cex():
    from nanobot_quant.td_live import _exec_quote

    assert _exec_quote("okx_cex", {"quote_ccy": "USDC"}) == "USDC"
    assert _exec_quote("okx_cex", {"quote_ccy": "usdt"}) == "USDT"
    assert _exec_quote("okx_cex", {}) == "USDT"
    assert _exec_quote("okx_cex", {"quote_ccy": "bogus"}) == "USDT"


def test_exec_quote_gate_stays_usdt():
    """Gate 现货只有 USDT 对：不能把 WebUI 的 quote_ccy 泄漏过去。"""
    from nanobot_quant.td_live import _exec_quote

    assert _exec_quote("gate", {"quote_ccy": "USDC"}) == "USDT"


def test_td_live_passes_quote_to_startup_brokers():
    src = (SRC / "td_live.py").read_text(encoding="utf-8")
    # 主 broker 与场景 broker 两处都必须传 quote
    assert src.count("quote=_quote_ccy,") == 2


# ── C120：成交明细记录入场周期（多周期规则逐笔可核验）────────────────
def _mk(periods, detail, es=9, ec=13):
    from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

    s = TdSequentialStrategy.__new__(TdSequentialStrategy)
    s._entry_periods = periods
    s._last_multi = detail
    s._td_params = {"entry_setup": es, "entry_countdown": ec}
    return s


def test_entry_period_hit_picks_smallest_triggering_period():
    s = _mk([1, 2, 3, 8], {1: (0, 0), 2: (3, 0), 3: (9, 0), 8: (5, 0)})
    assert s._entry_period_hit() == 3


def test_entry_period_hit_cd_channel_and_base():
    assert _mk([1, 2, 8], {1: (0, 0), 2: (0, 13), 8: (0, 0)})._entry_period_hit() == 2
    assert _mk([1], {1: (9, 0)})._entry_period_hit() == 1


def test_entry_period_hit_none_without_multi_detail():
    assert _mk([1, 2, 8], {})._entry_period_hit() is None


def test_entry_period_hit_uses_threshold():
    detail = {1: (0, 0), 2: (6, 0), 8: (6, 0)}
    assert _mk([1, 2, 8], detail, es=9)._entry_period_hit() is None
    assert _mk([1, 2, 8], detail, es=6)._entry_period_hit() == 2


def test_fills_detail_carries_entry_period():
    driver_src = (SRC / "backtest" / "driver.py").read_text(encoding="utf-8")
    broker_src = (SRC / "brokers" / "backtest_broker.py").read_text(encoding="utf-8")
    strat_src = (SRC / "strategies" / "td_sequential_strategy.py").read_text(encoding="utf-8")
    assert '"entry_period": meta.get("entry_period")' in driver_src
    assert '"entry_period": (order.custom_params or {}).get("entry_period")' in broker_src
    assert 'order.custom_params["entry_period"] = _ep_hit' in strat_src

    page = (SRC / "backtest_page.html").read_text(encoding="utf-8")
    md = (SRC / "backtest_markdown.py").read_text(encoding="utf-8")
    assert "<th>入场周期</th>" in page
    assert '"入场周期"' in md
