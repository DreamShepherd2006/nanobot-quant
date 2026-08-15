"""Unit tests for gate_cex_data.py (Gate spot candlesticks → DataFrame)."""

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
    assert _map_bar("1W") == "7d"
    assert _map_bar("5m") == "5m"
    assert _map_bar("bogus") == "1d"


def test_fetch_gate_kline_live(monkeypatch):
    calls = {}

    def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
        calls.update(pair=pair, interval=interval, limit=limit,
                     from_ts=from_ts, to_ts=to_ts)
        return _rows(4)

    import nanobot_quant.gate_cex_data as m
    monkeypatch.setattr(m, "_request", fake_request)
    df = fetch_gate_kline("CRCLX_USDT", bar="1D", limit=60)
    assert calls == {"pair": "CRCLX_USDT", "interval": "1d", "limit": 60,
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
