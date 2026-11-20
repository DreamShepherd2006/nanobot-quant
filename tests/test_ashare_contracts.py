"""A 股期权合约清单：名称解析 / 到期日推算 / 过滤（纯函数，不碰网络）。"""

from __future__ import annotations

from datetime import date

import pytest

from nanobot_quant.ashare import contracts as ac


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
    """东财多入口：首个域名 TLS 超时时必须换下一个（2026-11-20 实测有此事）。"""
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
