"""td-table 股票数据源（?source=stock）测试。

数据链路：东财（EastMoney，主源）→ yfinance（fallback）。
yfinance 在测试容器不可用（conftest stub）；东财通过 mock urlopen 测试。
覆盖：东财解析、yfinance fallback、双源失败、4H 限制、渲染分支。
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


class FakeResp:
    """urllib.response-like wrapper for mocked EastMoney JSON."""

    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._text.encode("utf-8")


_EM_JSON = '{"data":{"klines":[' \
    '"2026-01-02,135.700,138.010,138.580,134.330,198247166",' \
    '"2026-01-05,140.000,141.000,142.000,139.000,200000000",' \
    '"2026-01-06,141.500,142.900,143.800,140.500,180000000"' \
    ']}}'


def test_fetch_stock_kline_eastmoney_parse(monkeypatch):
    """东财主源：JSON klines → 小写列/naive index/行数。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_urlopen(req, timeout=20):
        calls["url"] = req.full_url
        return FakeResp(_EM_JSON)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    df = _fetch_stock_kline("NVDA", bar="1D", limit=60)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2026-01-02")
    assert "secid=105.NVDA" in calls["url"]
    assert "klt=101" in calls["url"]


def test_fetch_stock_kline_yahoo_fallback(monkeypatch):
    """东财失败 → fallback yfinance（大写列/tz-aware → 归一化）。"""
    import nanobot_quant.td_table_handlers as m

    def fake_urlopen(req, timeout=20):
        raise ConnectionError("eastmoney down")

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    calls = {}

    def fake_download(ticker, **kw):
        calls["interval"] = kw.get("interval")
        return _stock_df(n=70, tz=True)

    monkeypatch.setattr(m.yf, "download", fake_download)
    df = _fetch_stock_kline("NVDA", bar="1D", limit=60)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is None
    assert len(df) == 60
    assert calls["interval"] == "1d"


def test_fetch_stock_kline_both_fail(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    def fake_urlopen(req, timeout=20):
        raise ConnectionError("eastmoney down")

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(m.yf, "download", lambda *a, **k: pd.DataFrame())
    with pytest.raises(RuntimeError) as ei:
        _fetch_stock_kline("NVDA", bar="1D", limit=60)
    msg = str(ei.value)
    assert "东财" in msg and "yfinance" in msg


def test_fetch_stock_kline_4h_unsupported(monkeypatch):
    """4H 两个源都不支持 → 明确报错（不发起网络请求）。"""
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m.urllib.request, "urlopen", lambda req, timeout=20: (_ for _ in ()).throw(AssertionError("should not hit network")))
    with pytest.raises(RuntimeError, match="暂不支持 4H"):
        _fetch_stock_kline("NVDA", bar="4H", limit=60)


def test_render_snapshot_stock_source(monkeypatch):
    """股票源快照：标注来源、渲染表格行。"""
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=40, tz=False))
    result = _render_snapshot(
        "AAPL", "1D", 60, "td_sequential_futu", {"setup_period": 9}, 9, source="stock"
    )
    html = result[0] if isinstance(result, tuple) else result
    assert "股票（AAPL）" in html
    assert "<table>" in html


def test_render_history_stock_source(monkeypatch):
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=50, tz=False))
    result = _render_history(
        "AAPL", "1D", "2026-03-01", "2026-08-05",
        "td_sequential_futu", {"setup_period": 9}, 9, source="stock"
    )
    html = result[0] if isinstance(result, tuple) else result
    assert "股票（AAPL）" in html
    assert "9 信号" in html


def test_page_source_param_persisted(monkeypatch):
    """URL ?source=stock：表单保留选中态、placeholder 变化。"""
    import nanobot_quant.td_table_handlers as m

    monkeypatch.setattr(m, "_fetch_stock_kline", lambda ticker, **kw: _stock_df(n=40, tz=False))
    resp = td_table_page(FakeRequest({"tab": "snapshot", "ticker": "AAPL", "source": "stock"}))
    body = resp.body.decode("utf-8")
    assert 'value="stock" selected' in body
    assert 'placeholder="AAPL / SPY"' in body
