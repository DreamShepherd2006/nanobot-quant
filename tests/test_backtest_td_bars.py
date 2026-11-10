"""回测 TD 窗口（td_bars）可覆盖 + 报告可见（C139，2026-11-10 用户拍板 1+2A）。

背景：``min_history`` 原先只读 ``self.params``（实盘 exec_params），于是

* 通过 MCP ``overrides`` 传 ``td_bars`` 会被**静默忽略**（能传但无效）；
* ``backtest_config`` 不记录该值，页面/markdown 也看不到 ⇒ 改了实盘的 K 线数，
  历史回测就不可复现，而报告看起来完全一样。

定稿口径：覆盖优先级 = ``overrides > exec_params > 默认 120``，并在快照里记
生效值与来源（本次覆盖 / 跟随实盘 / 默认 120），页面与 markdown 同源显示。
窗口 <20 容纳不下 setup 周期与比较窗口 ⇒ fail-closed 报错。
"""

import inspect

import pytest

from nanobot_quant.backtest import driver as drv
from nanobot_quant import backtest_markdown as md


def _params(**kw):
    """最小可用场景配置（够构造 driver，不触发取数）。"""
    return {
        "scenes": {
            "high": {"enabled": True, "sleeptime": "1m", "symbols": ["SOL"]}
        },
        **kw,
    }


def _mk(*, params=None, **kw):
    return drv.BacktestDriver(
        scene="high", params=params if params is not None else _params(), **kw
    )


# ── 优先级链 ─────────────────────────────────────────────────────────────


def test_override_wins_over_exec_params():
    """overrides.td_bars 优先于实盘值（此前被静默忽略）。"""
    d = _mk(params=_params(td_bars=300), overrides={"td_bars": 60})
    assert d.td_bars == 60
    assert d.min_history == 60
    assert d.td_bars_source == "本次覆盖"


def test_follows_exec_params_when_not_overridden():
    """未覆盖时跟随实盘 exec_params 的 td_bars。"""
    d = _mk(params=_params(td_bars=300))
    assert d.td_bars == 300
    assert d.min_history == 300
    assert d.td_bars_source == "跟随实盘"


def test_defaults_to_120_when_absent():
    """两边都没有时回退 120（与策略类默认一致）。"""
    d = _mk()
    assert d.td_bars == 120
    assert d.min_history == 120
    assert d.td_bars_source == "默认 120"


def test_too_small_fails_closed():
    """<20 无法容纳 setup 周期 + 比较窗口 ⇒ 报错而不是静默凑合。"""
    with pytest.raises(ValueError) as ei:
        _mk(overrides={"td_bars": 10})
    assert "td_bars=10" in str(ei.value)


def test_strategy_receives_min_history():
    """策略侧读的是 min_history，必须等于解析出的窗口（注入 dict 同源）。"""
    src = inspect.getsource(drv)
    assert '"min_history": self.min_history' in src


# ── 报告可见性（结构性断言）─────────────────────────────────────────────


def test_snapshot_records_td_bars():
    """backtest_config 必须记 td_bars 与来源，否则报告不可复现。"""
    src = inspect.getsource(drv)
    assert '"td_bars": self.td_bars' in src
    assert '"td_bars_source": self.td_bars_source' in src


def test_page_and_markdown_expose_td_bars():
    """页面输入框 + 参数表行 + markdown 行缺一不可（展示层三处同源）。"""
    from pathlib import Path

    import nanobot_quant

    root = Path(nanobot_quant.__file__).resolve().parent
    html = (root / "backtest_page.html").read_text(encoding="utf-8")
    assert 'id="td_bars"' in html, "回测页缺少 TD 窗口输入框"
    assert '"td_bars","fee_rate"' in html, "collectOverrides 未收集 td_bars"
    assert '["TD 窗口"' in html, "结果参数表未显示 TD 窗口"

    mdsrc = inspect.getsource(md)
    assert '("TD 窗口"' in mdsrc, "markdown 参数表未显示 TD 窗口"
