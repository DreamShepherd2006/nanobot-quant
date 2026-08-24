"""Unit tests for gate_cex_data.py (Gate spot candlesticks → DataFrame)."""

import json
import urllib.error

import pandas as pd
import pytest

from nanobot_quant.gate_cex_data import (
    _map_bar,
    fetch_gate_kline,
    fetch_gate_ticker,
    rows_to_df,
)


def _rows(n=5, closed=True):
    # [ts, quote_volume, close, high, low, open, base_volume, closed]
    out = []
    for i in range(n):
        ts = 1786752000 + i * 86400
        out.append([str(ts), "1000.0", f"{100+i}.0", f"{102+i}.0",
                    f"{99+i}.0", f"{101+i}.0", "10.5", str(closed).lower()])
    return out


def test_rows_to_df_column_mapping():
    df = rows_to_df(_rows(3))
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.iloc[0].to_dict() == {"Open": 101.0, "High": 102.0,
                                    "Low": 99.0, "Close": 100.0, "Volume": 10.5}
    # UTC index, ascending
    assert df.index[0].tz is not None
    assert df.index.is_monotonic_increasing


def test_rows_to_df_drops_in_progress_bar():
    rows = _rows(3) + _rows(1, closed=False)  # last row in-progress
    df = rows_to_df(rows)
    assert len(df) == 3


def test_rows_to_df_malformed_rows_skipped():
    rows = [["1786752000", "1000.0"], None, "junk", ["x" * 20]]
    df = rows_to_df(rows)
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_bar_map():
    assert _map_bar("1D") == "1d"
    assert _map_bar("1H") == "1h"
    assert _map_bar("4H") == "4h"
    assert _map_bar("1W") == "1w"      # spec 语义：1W = 自然周线（不再用 7d）
    assert _map_bar("7D") == "7d"      # 7D = 7 天，独立粒度
    assert _map_bar("5m") == "5m"
    # 新周期（2026-08-24 方案 C：16 个周期全支持）
    assert _map_bar("3m") == "3m"
    assert _map_bar("2H") == "2h"
    assert _map_bar("6H") == "6h"
    assert _map_bar("8H") == "8h"
    assert _map_bar("12H") == "12h"
    assert _map_bar("3D") == "3d"
    assert _map_bar("30D") == "30d"
    # fail-closed：不支持的周期抛 KeyError，不静默回退日线
    with pytest.raises(KeyError):
        _map_bar("bogus")
    with pytest.raises(KeyError):
        _map_bar("2m")   # 非交易所粒度
    with pytest.raises(KeyError):
        _map_bar("1s")   # 秒级未纳入（用户拍板）


def test_fetch_gate_kline_live(monkeypatch):
    calls = {}

    def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
        calls.update(pair=pair, interval=interval, limit=limit,
                     from_ts=from_ts, to_ts=to_ts)
        return _rows(4)

    import nanobot_quant.gate_cex_data as m
    monkeypatch.setattr(m, "_request", fake_request)
    df = fetch_gate_kline("CRCLX_USDT", bar="1D", limit=60)
    # Gate limit 语义=返回 limit 根含进行中最后一根；fetch_gate_kline 请求
    # limit+1，过滤 closed=false 后正好返回 limit 根已收盘（A 修复第三部分）。
    assert calls == {"pair": "CRCLX_USDT", "interval": "1d", "limit": 61,
                     "from_ts": None, "to_ts": None}
    assert len(df) == 4


def test_fetch_gate_ticker_list_shape(monkeypatch):
    import nanobot_quant.gate_cex_data as m

    monkeypatch.setattr(urllib_request(), "urlopen", lambda req, timeout=20: _FakeResp(
        '[{"currency_pair":"CRCLX_USDT","last":"73.31"}]'))
    t = fetch_gate_ticker("CRCLX_USDT")
    assert t == {"currency_pair": "CRCLX_USDT", "last": "73.31"}


class _FakeResp:
    def __init__(self, body):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urllib_request():
    import urllib.request
    return urllib.request


# ── 黑名单：Gate 无交易对/已下架币停止查询 ──────────────────────────

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, label):
        self.code = 400
        self._label = label

    def read(self):
        return json.dumps({"label": self._label, "message": "x"}).encode()


def _clean_blacklist():
    import nanobot_quant.gate_cex_data as m
    m.clear_blacklist()


def test_kline_400_blacklists_symbol(monkeypatch):
    import nanobot_quant.gate_cex_data as m
    _clean_blacklist()

    def boom(req, timeout=20):
        raise _FakeHTTPError("INVALID_CURRENCY_PAIR")

    monkeypatch.setattr(urllib_request(), "urlopen", boom)
    import pytest
    with pytest.raises(urllib.error.HTTPError):
        fetch_gate_kline("MU_USDT", bar="1m", limit=120)
    assert m.blacklist_reason("MU") and "INVALID_CURRENCY_PAIR" in m.blacklist_reason("MU")
    _clean_blacklist()


def test_kline_blacklisted_short_circuits(monkeypatch):
    import nanobot_quant.gate_cex_data as m
    _clean_blacklist()
    m.mark_blacklisted("MU", "Gate 无此交易对/已下架 (INVALID_CURRENCY_PAIR)")
    calls = []

    def spy(req, timeout=20):
        calls.append(1)
        return _FakeResp("[]")

    monkeypatch.setattr(urllib_request(), "urlopen", spy)
    import pytest
    with pytest.raises(RuntimeError, match="已停止查询"):
        fetch_gate_kline("MU_USDT", bar="1m", limit=120)
    assert calls == []  # 不再发请求
    _clean_blacklist()


def test_ticker_400_blacklists_symbol(monkeypatch):
    import nanobot_quant.gate_cex_data as m
    _clean_blacklist()

    def boom(req, timeout=20):
        raise _FakeHTTPError("currency VSC is delisted")

    monkeypatch.setattr(urllib_request(), "urlopen", boom)
    assert fetch_gate_ticker("VSC_USDT") is None
    assert m.blacklist_reason("VSC") and "delisted" in m.blacklist_reason("VSC")
    _clean_blacklist()


def test_ticker_blacklisted_short_circuits(monkeypatch):
    import nanobot_quant.gate_cex_data as m
    _clean_blacklist()
    m.mark_blacklisted("VSC", "Gate 已下架/无行情 (delisted)")
    calls = []

    def spy(req, timeout=20):
        calls.append(1)
        return _FakeResp("[]")

    monkeypatch.setattr(urllib_request(), "urlopen", spy)
    assert fetch_gate_ticker("VSC_USDT") is None
    assert calls == []  # 不再发请求
    _clean_blacklist()


def test_clear_blacklist_reenables(monkeypatch):
    import nanobot_quant.gate_cex_data as m
    _clean_blacklist()
    m.mark_blacklisted("MU", "test")
    assert m.blacklist_reason("MU")
    m.clear_blacklist()
    assert m.blacklist_reason("MU") is None
    # 清空后重新探测（真实 _request 正常发请求）
    calls = []

    def fake(req, timeout=20):
        calls.append(1)
        return _FakeResp(json.dumps(_rows(4)))

    monkeypatch.setattr(urllib_request(), "urlopen", fake)
    df = fetch_gate_kline("MU_USDT", bar="1m", limit=120)
    assert calls == [1] and len(df) == 4
    _clean_blacklist()
