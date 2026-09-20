"""Tests for the Sina A-share data source.

新浪两个接口（kline / 实时报价）都不封数据中心 IP，是云端唯一可用的 A 股
源（东财 2026-09-20 实测被 IP 封禁）。这里覆盖：

① 代码 → sh/sz 前缀映射（含 5/9 开头的沪市 ETF）与非法输入拒绝；
② 不支持周期 fail-closed（scale=1 / 120 实测返回 null）；
③ kline 正常路径的列名/时区/裁剪；
④ 非法 JSON、空数组不能静默变成「无数据」；
⑤ datalen 超上限时截断并留 stderr 痕迹；
⑥ 实时报价的 GBK 解析与 fail-closed 取价。
"""

import json

import pandas as pd
import pytest

from nanobot_quant.data_sources import REGISTRY, get_data_source
from nanobot_quant.data_sources import sina as S


def _mk(n=50, scale_min=5):
    """构造新浪风格的 kline 回报（day/open/high/low/close/volume）。"""
    rows = []
    ts = pd.Timestamp("2026-09-01 09:35:00")
    for i in range(n):
        t = ts + pd.Timedelta(minutes=scale_min * i)
        px = 100.0 + i * 0.1
        rows.append({"day": t.strftime("%Y-%m-%d %H:%M:%S"), "open": f"{px:.3f}",
                     "high": f"{px + 0.2:.3f}", "low": f"{px - 0.2:.3f}",
                     "close": f"{px + 0.05:.3f}", "volume": str(1000 + i)})
    return rows


# ── 注册表 ─────────────────────────────────────────────────────────────

def test_sina_is_registered_as_research_source():
    spec = get_data_source("sina")
    assert spec.kind == "research"
    assert spec.bars == ("5m", "15m", "30m", "1H", "1D")
    assert "sina" in REGISTRY


def test_sina_rejects_unsupported_periods():
    spec = get_data_source("sina")
    for bad in ("1m", "2H", "4H", "1W"):
        with pytest.raises(KeyError):
            spec.interval_for(bad)


# ── symbol 映射 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("600519", "sh600519"),   # 沪市主板
    ("688981", "sh688981"),   # 科创板（6 开头）
    ("510050", "sh510050"),   # 沪市 ETF（5 开头）
    ("000858", "sz000858"),   # 深市主板
    ("300750", "sz300750"),   # 创业板
    ("159915", "sz159915"),   # 深市 ETF
])
def test_sina_symbol_prefix(code, expected):
    assert S.sina_symbol(code) == expected


def test_sina_symbol_rejects_non_a_share():
    for bad in ("AAPL", "BTC-USDT", "60051", "6005199", ""):
        with pytest.raises(ValueError):
            S.sina_symbol(bad)


# ── fetch_kline ────────────────────────────────────────────────────────

def test_fetch_kline_normalises_shape_and_timezone(monkeypatch):
    monkeypatch.setattr(S, "_get", lambda url, timeout=20: json.dumps(_mk(50)))
    df = S.fetch_kline("600519", bar="5m", limit=50)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "time"
    assert str(df.index.tz) == "Asia/Shanghai"
    assert len(df) == 50
    assert df["close"].dtype.kind == "f"


def test_fetch_kline_trims_to_limit(monkeypatch):
    monkeypatch.setattr(S, "_get", lambda url, timeout=20: json.dumps(_mk(120)))
    df = S.fetch_kline("600519", bar="5m", limit=30)
    assert len(df) == 30                      # tail(limit)


def test_fetch_kline_rejects_unsupported_bar():
    with pytest.raises(ValueError):
        S.fetch_kline("600519", bar="1m")


def test_fetch_kline_raises_on_empty_array(monkeypatch):
    monkeypatch.setattr(S, "_get", lambda url, timeout=20: "null")
    with pytest.raises(RuntimeError):
        S.fetch_kline("600519", bar="5m")


def test_fetch_kline_raises_on_bad_json(monkeypatch):
    monkeypatch.setattr(S, "_get", lambda url, timeout=20: "<html>oops</html>")
    with pytest.raises(RuntimeError) as ei:
        S.fetch_kline("600519", bar="5m")
    assert "非 JSON" in str(ei.value)


def test_fetch_kline_clips_to_api_max_and_warns(monkeypatch, capsys):
    """超上限要截断并留痕——绝不静默返回不完整数据。"""
    seen = {}

    def fake_get(url, timeout=20):
        seen["url"] = url
        return json.dumps(_mk(10))

    monkeypatch.setattr(S, "_get", fake_get)
    S.fetch_kline("600519", bar="5m", limit=99999)
    assert f"datalen={S._SINA_MAX_BARS}" in seen["url"]
    assert "接口上限" in capsys.readouterr().err


def test_fetch_kline_range_filter(monkeypatch):
    monkeypatch.setattr(S, "_get", lambda url, timeout=20: json.dumps(_mk(200)))
    from datetime import datetime
    start = datetime(2026, 9, 1, 10, 0, 0)
    end = datetime(2026, 9, 1, 11, 0, 0)
    df = S.fetch_kline("600519", bar="5m", limit=5000, start=start, end=end)
    assert len(df) > 0
    assert df.index.min() >= pd.Timestamp(start).tz_localize("Asia/Shanghai")
    assert df.index.max() <= pd.Timestamp(end).tz_localize("Asia/Shanghai")


# ── get_price ──────────────────────────────────────────────────────────

class _QuoteResp:
    def __init__(self, text):
        self._b = text.encode("gbk")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_price_parses_gbk_quote(monkeypatch):
    # 名称,今开,昨收,现价,...
    body = 'var hq_str_sh600519="贵州茅台,1250.00,1266.98,1257.12,1260.00,1240.00' + ',0' * 20 + '";'
    monkeypatch.setattr(S.urllib.request, "urlopen",
                        lambda req, timeout=15: _QuoteResp(body))
    assert S.get_price("600519") == pytest.approx(1257.12)


def test_get_price_falls_back_to_prev_close_when_halted(monkeypatch):
    body = 'var hq_str_sh600519="贵州茅台,0,1266.98,0,0,0' + ',0' * 20 + '";'
    monkeypatch.setattr(S.urllib.request, "urlopen",
                        lambda req, timeout=15: _QuoteResp(body))
    assert S.get_price("600519") == pytest.approx(1266.98)


def test_get_price_returns_zero_on_failure(monkeypatch):
    def boom(req, timeout=15):
        raise OSError("network down")

    monkeypatch.setattr(S.urllib.request, "urlopen", boom)
    assert S.get_price("600519") == 0.0       # fail-closed，绝不抛到调用方


def test_get_price_rejects_non_a_share():
    assert S.get_price("AAPL") == 0.0
