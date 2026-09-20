"""Tests for the EastMoney data source's transport hardening.

东财对裸脚本流量会直接断连（``RemoteDisconnected``）——2026-09-20 在 HF Space
与 Nightly 容器上双双复现，与 limit / 周期 / 标的无关。因此取数层现在发完整
浏览器头，并对多个 CDN 入口做回退。这里用一个假 urlopen 验证：

① 浏览器头确实带上了（UA + Referer）；
② 第一个入口失败时会换下一个，而不是直接放弃；
③ 全部失败时报错包含每一个入口的失败原因 —— 绝不静默退化成「无数据」。
"""

import json
import urllib.error

import pytest

from nanobot_quant.data_sources import eastmoney as EM


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_headers_carry_browser_signals():
    assert "Mozilla" in EM._EM_HEADERS["User-Agent"]
    assert EM._EM_HEADERS["Referer"].startswith("http")
    assert len(EM._EM_HOSTS) >= 2


def test_fetch_json_falls_back_to_next_host(monkeypatch):
    calls: list[str] = []

    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError("RemoteDisconnected: boom")
        return _Resp({"data": {"klines": []}})

    monkeypatch.setattr(EM.urllib.request, "urlopen", fake_open)
    out = EM._fetch_json("secid=1.588000")
    assert out == {"data": {"klines": []}}
    assert len(calls) == 2
    assert EM._EM_HOSTS[0] in calls[0]
    assert EM._EM_HOSTS[1] in calls[1]


def test_fetch_json_sends_browser_headers(monkeypatch):
    seen: dict = {}

    def fake_open(req, timeout=None):
        seen.update(req.headers)
        return _Resp({"data": {"klines": []}})

    monkeypatch.setattr(EM.urllib.request, "urlopen", fake_open)
    EM._fetch_json("secid=1.588000")
    # urllib 会把首字母大写化：User-agent / Referer
    assert "Mozilla" in seen.get("User-agent", "")
    assert "eastmoney.com" in seen.get("Referer", "")


def test_fetch_json_raises_with_every_attempt(monkeypatch):
    def fake_open(req, timeout=None):
        raise urllib.error.URLError("RemoteDisconnected: nope")

    monkeypatch.setattr(EM.urllib.request, "urlopen", fake_open)
    with pytest.raises(RuntimeError) as ei:
        EM._fetch_json("secid=1.588000")
    msg = str(ei.value)
    for host in EM._EM_HOSTS:
        assert host in msg                 # 每个入口都被报到，不静默
    assert "RemoteDisconnected" in msg    # 原始错误保留
