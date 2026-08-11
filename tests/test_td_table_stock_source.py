"""td-table 股票数据源（?source=stock）测试。

数据链路：东财（EastMoney，主源）→ yfinance（fallback）。
支持美股（NVDA）与 A 股（601127 / 000001 等 6 位数字代码）。
yfinance 在测试容器不可用（conftest stub）；东财通过 mock urlopen 测试。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.td_table_handlers import (
    _fetch_stock_kline,
    _fetch_stock_kline_yahoo,
    _render_history,
    _render_snapshot,
    _stock_secid,
    _yf_symbol,
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


def test_stock_secid_mapping():
    """6 位数字=A股（沪 1./深 0.），字母=美股 105.；yfinance 后缀映射。"""
    assert _stock_secid("601127") == "1.601127"
    assert _stock_secid("000001") == "0.000001"
    assert _stock_secid("300750") == "0.300750"
    assert _stock_secid("NVDA") == "105.NVDA"
    assert _yf_symbol("601127") == "601127.SS"
    assert _yf_symbol("000001") == "000001.SZ"
    assert _yf_symbol("NVDA") == "NVDA"


def test_fetch_stock_kline_eastmoney_parse(monkeypatch):
    """东财主源（A 股 secid=1.）：JSON klines → 小写列/Asia/Shanghai aware/行数。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_urlopen(req, timeout=20):
        calls["url"] = req.full_url
        return FakeResp(_EM_JSON)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    df = _fetch_stock_kline("601127", bar="1D", limit=60)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "Asia/Shanghai"
    assert len(df) == 3
    assert df.index[0] == pd.Timestamp("2026-01-02", tz="Asia/Shanghai")
    assert "secid=1.601127" in calls["url"]
    assert "klt=101" in calls["url"]


def test_fetch_stock_kline_us_secid(monkeypatch):
    """美股走 secid=105.。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_urlopen(req, timeout=20):
        calls["url"] = req.full_url
        return FakeResp(_EM_JSON)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    _fetch_stock_kline("NVDA", bar="1D", limit=60)
    assert "secid=105.NVDA" in calls["url"]


def test_fetch_stock_kline_yahoo_fallback(monkeypatch):
    """东财失败 → fallback yfinance（大写列/tz-aware → 归一化，A 股 .SS）。"""
    import nanobot_quant.td_table_handlers as m

    def fake_urlopen(req, timeout=20):
        raise ConnectionError("eastmoney down")

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    calls = {}

    def fake_download(ticker, **kw):
        calls["ticker"] = ticker
        calls["interval"] = kw.get("interval")
        return _stock_df(n=70, tz=True)

    monkeypatch.setattr(m.yf, "download", fake_download)
    df = _fetch_stock_kline("601127", bar="1D", limit=60)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "America/New_York"  # yfinance exchange tz kept
    assert len(df) == 60


def test_fetch_stock_kline_yahoo_minute_end_extends(monkeypatch):
    """分钟周期 start/end 同一天时 end 必须 +1 天，否则 yfinance 区间为空
    （2026-08-11 AAPL 5m 60 根失败根因：start=end=今天 → 空区间 → 无数据）。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_download(ticker, **kw):
        calls.update(kw)
        return _stock_df(n=10, tz=True)

    monkeypatch.setattr(m.yf, "download", fake_download)
    start = pd.Timestamp("2026-08-11 11:59")
    end = pd.Timestamp("2026-08-11 21:59")
    df = _fetch_stock_kline_yahoo("AAPL", bar="5m", limit=60, start=start, end=end)
    assert calls["start"] == "2026-08-11"
    assert calls["end"] == "2026-08-12"  # +1 天确保区间非空
    assert calls["interval"] == "5m"


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

    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=20: (_ for _ in ()).throw(AssertionError("should not hit network")))
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
    assert 'placeholder="NVDA / 601127"' in body

_EM_JSON_1H = '{"data":{"klines":[' \
    '"2026-08-04 22:30,135.700,138.010,138.580,134.330,198247166",' \
    '"2026-08-04 23:30,140.000,141.000,142.000,139.000,200000000",' \
    '"2026-08-05 04:00,141.500,142.900,143.800,140.500,180000000"' \
    ']}}'


def test_fetch_stock_kline_us_daily_ny_tz(monkeypatch):
    """美股日 K（klt=101）：东财返回美东日期 → America/New_York。"""
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_urlopen(req, timeout=20):
        calls["url"] = req.full_url
        return FakeResp(_EM_JSON)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    df = _fetch_stock_kline("NVDA", bar="1D", limit=60)
    assert "secid=105.NVDA" in calls["url"]
    assert str(df.index.tz) == "America/New_York"


def test_fetch_stock_kline_us_intraday_shanghai_tz(monkeypatch):
    """美股分钟 K（klt=60）：东财时间戳为北京时间 → Asia/Shanghai。

    实测（2026-08-05）：美东 16:00 收盘 = 北京 04:00，东财分钟 K 用
    北京时间标注；日 K 用美东日期标注。
    """
    import nanobot_quant.td_table_handlers as m

    calls = {}

    def fake_urlopen(req, timeout=20):
        calls["url"] = req.full_url
        return FakeResp(_EM_JSON_1H)

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_urlopen)
    df = _fetch_stock_kline("NVDA", bar="1H", limit=60)
    assert "klt=60" in calls["url"]
    assert str(df.index.tz) == "Asia/Shanghai"
    # 北京 08-05 04:00 = UTC 08-04 20:00 = 美东 08-04 16:00（收盘）
    assert df.index[-1].tz_convert("America/New_York").hour == 16


def test_display_utc_column_for_onchainos():
    """链上源（aware UTC 索引）：_time=北京、_time_utc=UTC 两列并存。"""
    import nanobot_quant.td_table_handlers as m

    idx = pd.date_range("2026-08-05 05:15", periods=2, freq="15min", tz="UTC")
    df = pd.DataFrame({"Close": [63.5, 63.7]}, index=idx)
    out = m._display(df)
    assert out["_time"].iloc[0] == "2026-08-05 13:15"      # UTC+8
    assert out["_time_utc"].iloc[0] == "2026-08-05 05:15"  # 原生 UTC
    headers = m._build_headers(True, True, True)
    assert "UTC 时间" in headers
