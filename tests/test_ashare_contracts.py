"""A 股期权合约清单：名称解析 / 到期日推算 / 过滤（纯函数，不碰网络）。"""

from __future__ import annotations

import json
import os
import time
from datetime import date

import pytest

from nanobot_quant.ashare import contracts as ac

@pytest.fixture(autouse=True)
def _reset_contracts_state(monkeypatch):
    """退避状态与 LAST_SOURCE 是模块级的，必须每个用例重置（否则先后顺序污染）。"""
    monkeypatch.setattr(ac, "LAST_SOURCE", "")
    monkeypatch.setattr(ac, "_BACKOFF", {"until": 0.0, "msg": ""})
    yield


def _sina_spec_line(code: str = "10011425", strike: str = "2.8000",
                    undl: str = "510050", right: str = "C",
                    expire: str = "2026-12-23", name: str = "50ETF购12月2800") -> str:
    """构造与新浪 CON_OP_ 同形的 51 字段行（字段索引见 contracts._F_*）。"""
    f = ["0"] * 51
    f[7], f[36], f[37], f[45], f[46] = strike, undl, name, right, expire
    return f'var hq_str_CON_OP_{code}="' + ",".join(f) + '";'


def _expired_cache(tmp_path, items=None) -> None:
    """写一份已过 TTL 的缓存，逼 contracts() 走网络。"""
    p = ac.cache_path(tmp_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": int(time.time()) - ac.CACHE_TTL_S - 10,
                   "items": items or [{"code": "9", "undl": "510050.SH",
                                      "right": "C", "strike": 2.8,
                                      "expire": "20261125"}]}, fh)


def test_expiry_is_fourth_wednesday_known_dates():
    # 已与 QMT 导出的 202611/202612/202703 到期日核对一致
    assert ac.expiry_of(2026, 11) == date(2026, 11, 25)
    assert ac.expiry_of(2026, 12) == date(2026, 12, 23)
    assert ac.expiry_of(2027, 3) == date(2027, 3, 24)


def test_expiry_always_wednesday_between_22_and_28():
    for year in (2025, 2026, 2027):
        for month in range(1, 13):
            d = ac.expiry_of(year, month)
            assert d.weekday() == 2, (year, month, d)
            assert 22 <= d.day <= 28, (year, month, d)
            assert d.month == month


def test_resolve_expiry_rolls_to_next_year():
    today = date(2026, 11, 20)
    assert ac.resolve_expiry(11, today) == date(2026, 11, 25)
    assert ac.resolve_expiry(6, today) == date(2027, 6, 23)
    # 当月已过第四个星期三 → 顺延到明年
    assert ac.resolve_expiry(11, date(2026, 11, 26)) == date(2027, 11, 24)


def test_parse_name_put_and_call():
    today = date(2026, 11, 20)
    c = ac.parse_name("50ETF购11月2800", today)
    assert c == {"undl": "510050.SH", "right": "C", "month": 11,
                 "strike": 2.8, "expire": "20261125"}

    p = ac.parse_name("300ETF沽6月3600", today)
    assert p == {"undl": "510300.SH", "right": "P", "month": 6,
                 "strike": 3.6, "expire": "20270623"}


def test_parse_name_decimal_strike():
    r = ac.parse_name("500ETF沽11月6250", date(2026, 11, 20))
    assert r["strike"] == 6.25


@pytest.mark.parametrize("name", [
    "", "创业板ETF购6月3000",   # 池外标的
    "50ETF购X月2800",           # 月份非法
    "50ETF购11月",              # 缺行权价
    "随便一个名字",
])
def test_parse_name_rejects(name):
    assert ac.parse_name(name, date(2026, 11, 20)) is None


def test_filter_active_drops_expired_and_other_underlyings():
    items = [
        {"code": "1", "undl": "510050.SH", "expire": "20261125"},
        {"code": "2", "undl": "510050.SH", "expire": "20261119"},   # 已到期
        {"code": "3", "undl": "588080.SH", "expire": "20261125"},   # 池外
    ]
    out = ac.filter_active(items, date(2026, 11, 20), ["510050.SH"])
    assert [x["code"] for x in out] == ["1"]


def test_contracts_falls_back_to_cache_when_fetch_fails(tmp_path, monkeypatch):
    ac.save_cached(tmp_path, [{"code": "9", "undl": "510050.SH", "expire": "20261125"}])

    def _boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ac, "fetch_contracts", _boom)
    r = ac.contracts(tmp_path, force=True)      # force → 必须走网络，失败落回缓存
    assert r["source"] == "cache"
    assert r["items"][0]["code"] == "9"
    assert "network down" in r["error"]


def test_contracts_uses_cache_within_ttl(tmp_path, monkeypatch):
    ac.save_cached(tmp_path, [{"code": "7", "undl": "510300.SH", "expire": "20261223"}])
    monkeypatch.setattr(ac, "fetch_contracts",
                        lambda *a, **k: pytest.fail("不应走网络"))
    r = ac.contracts(tmp_path)
    assert r["source"] == "cache" and r["items"][0]["code"] == "7"


def test_fetch_page_falls_back_across_hosts(monkeypatch):
    from urllib.error import URLError

    calls: list[str] = []

    class _Resp:
        def read(self):
            return ('{"data":{"total":1,"diff":[{"f12":"1001","f13":10,'
                    '"f14":"50ETF购11月2800"}]}}').encode()

    def _urlopen(req, timeout=0):
        calls.append(req.full_url)
        if req.full_url.startswith(ac.LIST_HOSTS[0]):   # 只让首个入口失败
            raise URLError("tls handshake timeout")
        return _Resp()

    monkeypatch.setattr(ac, "_HOST_OK", "", raising=False)
    monkeypatch.setattr(ac.urllib.request, "urlopen", _urlopen)
    data = ac._fetch_page(1)
    assert data["data"]["total"] == 1
    assert len(calls) == 2, calls
    assert ac._HOST_OK == ac.LIST_HOSTS[1]     # 记住可用入口

    # 第二次请求应直接用记住的入口（不再重试坏域名）
    calls.clear()
    ac._fetch_page(2)
    assert len(calls) == 1 and ac.LIST_HOSTS[1] in calls[0]


# ── 新浪主源（2026-11-23 新增） ─────────────────────────────

def test_month_seq_rolls_over_year():
    assert ac.month_seq(date(2026, 11, 23), 4) == ["2611", "2612", "2701", "2702"]


def test_parse_sina_codes_reads_op_up_line():
    txt = ('var hq_str_OP_UP_5100502611="CON_OP_10011425,CON_OP_10011426";\n'
           'var hq_str_OP_DOWN_5100502611="";\n')
    assert ac.parse_sina_codes(txt) == ["10011425", "10011426"]


def test_parse_sina_spec_real_field_positions():
    spec = ac.parse_sina_spec(_sina_spec_line())
    assert spec == {"code": "10011425", "name": "50ETF购12月2800",
                    "undl": "510050.SH", "right": "C",
                    "strike": 2.8, "expire": "20261223"}


@pytest.mark.parametrize("line", [
    "", "随便一行", 'var hq_str_CON_OP_1001=""',
    _sina_spec_line(strike="0"),          # 行权价非正
    _sina_spec_line(undl="999999"),      # 池外短码
    _sina_spec_line(right="X"),          # 方向非法
    _sina_spec_line(expire="2026/12/23"),  # 到期日格式异常
])
def test_parse_sina_spec_rejects(line):
    assert ac.parse_sina_spec(line) is None


def test_fetch_contracts_prefers_sina(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(ac, "fetch_contracts_sina", lambda t, to: (
        calls.append("sina") or [{"code": "1", "undl": "510050.SH", "expire": "20261223"}]))
    monkeypatch.setattr(ac, "fetch_contracts_eastmoney",
                        lambda t, to: calls.append("em") or [])
    items = ac.fetch_contracts(date(2026, 11, 23))
    assert items and calls == ["sina"] and ac.LAST_SOURCE == "sina"


def test_fetch_contracts_falls_back_to_eastmoney(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("sina down")

    monkeypatch.setattr(ac, "fetch_contracts_sina", _boom)
    monkeypatch.setattr(ac, "fetch_contracts_eastmoney",
                        lambda t, to: [{"code": "2", "undl": "510300.SH",
                                        "expire": "20261223"}])
    items = ac.fetch_contracts(date(2026, 11, 23))
    assert items[0]["code"] == "2" and ac.LAST_SOURCE == "eastmoney"


def test_fetch_contracts_reports_every_source(monkeypatch):
    def _boom(name):
        def _f(*_a, **_k):
            raise RuntimeError(f"{name} down")
        return _f

    monkeypatch.setattr(ac, "fetch_contracts_sina", _boom("sina"))
    monkeypatch.setattr(ac, "fetch_contracts_eastmoney", _boom("eastmoney"))
    with pytest.raises(RuntimeError) as ei:
        ac.fetch_contracts(date(2026, 11, 23))
    assert "sina" in str(ei.value) and "eastmoney" in str(ei.value)


# ── 失败退避（避免缓存过期后每轮采样都打外网） ─────────

def test_contracts_backoff_skips_network_within_window(tmp_path, monkeypatch):
    _expired_cache(tmp_path)
    n = {"calls": 0}

    def _boom(*_a, **_k):
        n["calls"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(ac, "fetch_contracts", _boom)
    first = ac.contracts(tmp_path)
    assert first["source"] == "cache" and "network down" in first["error"]
    assert first["retry_in_s"] == ac.FAIL_BACKOFF_S
    assert first["age_s"] is not None and first["age_s"] >= ac.CACHE_TTL_S

    second = ac.contracts(tmp_path)           # 退避窗口内：不再打网络
    assert n["calls"] == 1
    assert "退避中" in second["error"]


def test_contracts_force_bypasses_backoff(tmp_path, monkeypatch):
    _expired_cache(tmp_path)
    n = {"calls": 0}

    def _boom(*_a, **_k):
        n["calls"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(ac, "fetch_contracts", _boom)
    ac.contracts(tmp_path)                    # 第一次失败 → 进入退避
    ac.contracts(tmp_path, force=True)        # 页面手动刷新要真去试
    assert n["calls"] == 2


def test_contracts_backoff_resets_after_success(tmp_path, monkeypatch):
    _expired_cache(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(ac, "fetch_contracts", _boom)
    ac.contracts(tmp_path)
    assert ac._BACKOFF["until"] > 0

    monkeypatch.setattr(ac, "fetch_contracts",
                        lambda t, to=8.0: [{"code": "5", "undl": "510050.SH",
                                            "expire": "20261223"}])
    r = ac.contracts(tmp_path, force=True)
    assert r["source"].startswith("live") and ac._BACKOFF["until"] == 0.0
