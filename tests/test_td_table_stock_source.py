"""td-table 股票数据源（?source=stock / yfinance）测试。

覆盖：yfinance 数据归一化（列名小写、naive index、tail limit）、
空数据/异常处理、快照与历史区间渲染的 source 分支。
yfinance 在测试容器不可用（conftest stub），全部 mock yf.download。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.td_table_handlers import (
    _fetch_stock_kline,
    _render_history,
    _render_snapshot,
    td_table_page,
)


class FakeRequest:
    def __init__(self, params: dict):
        self.query_params = params


def _stock_df(n: int = 70, tz: bool = True) -> pd.DataFrame:
    closes = [float(i) for i in range(n)]
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    if tz:
        idx = idx.tz_localize("America/New_York")
    return pd.DataFrame(
        {"Open": closes, "High": [c + 1 for c in closes],
         "Low": [c - 1 for c in closes], "Close": closes,
         "Volume": [1000] * n},
        index=idx,
    )


def test_fetch_stock_kline_normalises(monkeypatch):
    """大写列 + tz-aware index + 超 limit 行 → 小写列/naive/尾部截断。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_download(ticker, **kw):
        calls.update(kw)
        return _stock_df(n=70, tz=True)

    monkeypatch.setattr(m.yf, "download", fake_download)
    df = _fetch_stock_kline("AAPL", bar="1D", limit=60)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None
    assert len(df) == 60
    assert calls["interval"] == "1d"


def test_fetch_stock_kline_interval_map(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_download(ticker, **kw):
        calls.update(kw)
        return _stock_df(n=5, tz=False)

    monkeypatch.setattr(m.yf, "download", fake_download)
    _fetch_stock_kline("TSLA", bar="1H", limit=20)
    assert calls["interval"] == "60m"


def test_fetch_stock_kline_empty_raises(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(RuntimeError, match="yfinance 无数据"):
        _fetch_stock_kline("SPY", bar="1D", limit=60)


def test_fetch_stock_kline_error_propagates(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(m.yf, "download", boom)
    with pytest.raises(ConnectionError):
        _fetch_stock_kline("AAPL", bar="1D", limit=60)


def test_render_snapshot_stock_source(monkeypatch):
    """股票源快照：标注 yfinance、渲染表格行。"""
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=40, tz=False))
    result = _render_snapshot(
        "AAPL", "1D", 60, "td_sequential_futu", {"setup_period": 9}, 9, source="stock"
    )
    html = result[0] if isinstance(result, tuple) else result
    assert "yfinance" in html and "AAPL" in html
    assert "<table>" in html


def test_render_history_stock_source(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=50, tz=False))
    result = _render_history(
        "AAPL", "1D", "2026-03-01", "2026-08-05",
        "td_sequential_futu", {"setup_period": 9}, 9, source="stock"
    )
    html = result[0] if isinstance(result, tuple) else result
    assert "yfinance" in html
    assert "9 信号" in html


def test_page_source_param_persisted(monkeypatch):
    """URL ?source=stock：表单保留选中态、tab 链接带 source。"""
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=40, tz=False))
    resp = td_table_page(FakeRequest({"tab": "snapshot", "ticker": "AAPL", "source": "stock"}))
    body = resp.body.decode("utf-8")
    assert 'value="stock" selected' in body
    assert 'placeholder="AAPL / SPY"' in body
