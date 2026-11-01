"""OKX 公开行情端点限流：跨线程全局节流 + 429 指数退避重试（C95 第三层）。

2026-11-01 实盘实证：EMA 折价门开启后首轮 prefetch 每标的 8 页（2137 根
÷ 300），5 标的并发 = 40 个请求；原来的 ``time.sleep(_RATE_DELAY)`` 是
**每线程独立**的，峰值 ~33 req/s 远超 OKX 的 ~10 req/s ⇒ 四个标的全部

    [TD] DATA ERROR | symbol=SOL HTTPError: 429 Too Many Requests
    /api/v5/market/history-candles?...

修复：① 全局节流（模块级锁 + 上次请求时刻，跨线程串行化）；
     ② 429 指数退避重试（1s / 2s / 4s，共 4 次），仍失败由调用方 fail-closed。
"""

from __future__ import annotations

import threading
import time

import pytest
import requests

from nanobot_quant import okx_cex_data


class _Resp:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload or {"code": "0", "data": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._payload


class _Sess:
    """按预设序列返回响应；记录调用次数。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        idx = min(self.calls - 1, len(self._responses) - 1)
        return self._responses[idx]


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """重试/节流都不真睡，测试保持毫秒级。"""
    slept: list[float] = []
    monkeypatch.setattr(okx_cex_data.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(okx_cex_data, "_last_req_monotonic", 0.0)
    return slept


def test_429_is_retried_then_succeeds(_no_real_sleep):
    sess = _Sess([_Resp(429), _Resp(429), _Resp(200, {"code": "0", "data": [[1]]})])

    resp = okx_cex_data._get_with_retry(sess, "http://x", {})

    assert resp.status_code == 200
    assert sess.calls == 3
    # 退避值与节流等待共用 time.sleep：只挑退避档（≥ 1s）核对
    assert [s for s in _no_real_sleep if s >= 1.0] == [1.0, 2.0]


def test_429_exhausted_raises(_no_real_sleep):
    sess = _Sess([_Resp(429)])

    with pytest.raises(requests.HTTPError):
        okx_cex_data._get_with_retry(sess, "http://x", {})

    assert sess.calls == okx_cex_data._RETRY_ATTEMPTS


def test_non_429_error_not_retried(_no_real_sleep):
    sess = _Sess([_Resp(500)])

    with pytest.raises(requests.HTTPError):
        okx_cex_data._get_with_retry(sess, "http://x", {})

    assert sess.calls == 1  # 不重试非限流错误


def test_throttle_is_global_across_threads(monkeypatch):
    """多线程并发请求必须串行化到 _RATE_DELAY 间隔（不能各线程各睡）。"""
    stamps: list[float] = []
    real_sleep = time.sleep

    def _record_sleep(s):
        stamps.append(s)
        real_sleep(min(s, 0.002))  # 只睡 2ms 模拟，避免测试变慢

    monkeypatch.setattr(okx_cex_data.time, "sleep", _record_sleep)
    monkeypatch.setattr(okx_cex_data, "_last_req_monotonic", 0.0)

    def worker():
        for _ in range(3):
            okx_cex_data._throttle()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 12 次调用里除首次外都该撞到节流等待 ⇒ 至少 11 次 sleep 记录
    assert len(stamps) >= 11
