"""sse_options（上交所云行情期权链）单测 —— 全离线，网络调用由 fake 替换。

实证背景见 docs/quant-system.md §33.38.10：云行情可用（实时全链），
官网披露接口 403、深交所 reset；字段按 select 位置返回，只暴露已确认字段。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.data_sources import sse_options

PAYLOAD = {
    "date": 20260923,
    "time": 115743,
    "total": 5,
    "begin": 0,
    "end": 5,
    "list": [
        ["10010971", "510050C2609M02850", "50ETF购9月2850", 0.1534, 0.1649, 0.1665, 0.1532, 636, 1009533],
        ["10010972", "510050P2609M02850", "50ETF沽9月2850", 0.1180, 0.1100, 0.1200, 0.1090, 500, 600000],
        ["10010980", "510050C2612M03000", "50ETF购12月3000", 0.0500, 0.0500, 0.0500, 0.0500, 1, 500],
        ["10010999", "510500C2612A07750", "500ETF购12月7750A", 0.2000, 0.2000, 0.2000, 0.2000, 10, 200],
        ["10010888", "NOT-A-CONTRACT", "脏数据", 1, 1, 1, 1, 1, 1],
    ],
}


def _fake_get_json(payload):
    def _inner(url, timeout=20):
        assert "tstyle" in url
        return payload
    return _inner


# ── 合约码解析 ────────────────────────────────────────────────────────

def test_parse_contract_standard():
    meta = sse_options.parse_contract("510050C2609M02850")
    assert meta == {"underlying": "510050", "right": "C", "expiry": "2609",
                    "flag": "M", "strike": 2.85}


def test_parse_contract_put_and_adjusted_flag():
    put = sse_options.parse_contract("510050P2703M02750")
    assert put["right"] == "P" and put["expiry"] == "2703" and put["strike"] == 2.75
    adj = sse_options.parse_contract("510500C2612A07750")
    assert adj["flag"] == "A" and adj["strike"] == 7.75


@pytest.mark.parametrize("bad", ["", "510050X2609M02850", "NOT-A-CONTRACT", "510050C2609M0285"])
def test_parse_contract_rejects_garbage(bad):
    assert sse_options.parse_contract(bad) is None


# ── fetch_chain ───────────────────────────────────────────────────────

def test_fetch_chain_columns_sorted_and_numeric(monkeypatch):
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json(PAYLOAD))
    df = sse_options.fetch_chain("510050")
    assert list(df.columns) == list(sse_options._COLUMNS)
    assert len(df) == 4                      # 脏行被跳过、其余保留
    assert isinstance(df["last"].iloc[0], float) or pd.api.types.is_numeric_dtype(df["last"])
    # 排序：先到期月、再行权价、最后认购/认沽
    assert list(df["expiry"]) == ["2609", "2609", "2612", "2612"]
    assert list(df["strike"]) == sorted(df["strike"])
    assert set(df["right"]) == {"C", "P"}


def test_fetch_chain_fail_closed_on_empty_list(monkeypatch):
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json({"list": []}))
    with pytest.raises(RuntimeError, match="0 个合约"):
        sse_options.fetch_chain("159915")     # 深市标的：不该静默返回空表


def test_fetch_chain_fail_closed_on_missing_list(monkeypatch):
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json({"error": "boom"}))
    with pytest.raises(RuntimeError, match="缺 list"):
        sse_options.fetch_chain("510050")


def test_fetch_chain_propagates_http_error(monkeypatch):
    def _boom(url, timeout=20):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(sse_options, "_get_json", _boom)
    with pytest.raises(RuntimeError, match="403"):
        sse_options.fetch_chain("510050")


# ── 汇总 / P-C 比 ─────────────────────────────────────────────────────

def test_chain_summary(monkeypatch):
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json(PAYLOAD))
    s = sse_options.chain_summary("510050")
    assert s["total"] == 4 and s["calls"] == 3 and s["puts"] == 1
    assert s["expiries"] == {"2609": 2, "2612": 2}
    assert s["strike_min"] == 2.85 and s["strike_max"] == 7.75


def test_put_call_ratio(monkeypatch):
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json(PAYLOAD))
    r = sse_options.put_call_ratio("510050")
    assert r["call_volume"] == 647 and r["put_volume"] == 500
    assert r["pcr"] == pytest.approx(500 / 647)
    assert r["basis"] == "volume"           # 口径显式标注，不可当持仓量口径用


def test_put_call_ratio_without_calls_is_none(monkeypatch):
    payload = {"list": [["1", "510050P2609M03000", "50ETF沽9月3000", 0.1, 0.1, 0.1, 0.1, 7, 70]]}
    monkeypatch.setattr(sse_options, "_get_json", _fake_get_json(payload))
    r = sse_options.put_call_ratio("510050")
    assert r["call_volume"] == 0 and r["pcr"] is None   # 除零不猜


def test_available_underlyings_records_failures(monkeypatch):
    def _fake_fetch(code, timeout=20):
        if code == "510300":
            raise RuntimeError("HTTP 500")
        return pd.DataFrame({"x": range(3)})
    monkeypatch.setattr(sse_options, "fetch_chain", _fake_fetch)
    out = sse_options.available_underlyings()
    assert out["510300"] == -1 and out["510050"] == 3
    assert set(out) == set(sse_options.SSE_UNDERLYINGS)


def test_underlyings_cover_all_five_etf():
    assert sse_options.SSE_UNDERLYINGS == ("510050", "510300", "510500", "588000", "588080")
