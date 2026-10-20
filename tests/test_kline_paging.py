"""公用反向分页器 ``paginate_backwards`` 的单元测试（2026-10-20，方案 C Step 1）。"""

from __future__ import annotations

import logging

from nanobot_quant.kline_paging import paginate_backwards

MIN = 60_000


def _rows(start_ms: int, count: int, step_ms: int = MIN) -> list:
    return [[start_ms + i * step_ms] for i in range(count)]


def test_collects_pages_until_short_page():
    """整页 → 整页 → 短页：收集全部，短页处停。"""
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            return _rows(cursor - 999 * MIN, 1000)
        if len(calls) == 2:
            return _rows(cursor - 999 * MIN, 1000)
        return _rows(cursor - 299 * MIN, 300)

    start = 0
    pages = paginate_backwards(
        fetch, initial_cursor=10 * 1000 * MIN, start_ms=start,
        page_limit=1000, next_cursor=lambda oldest: oldest - MIN, label="t",
    )
    assert len(pages) == 3
    assert len(calls) == 3
    # 游标单调回退
    assert calls == sorted(calls, reverse=True)


def test_first_page_outside_window_still_pages():
    """首批整页都晚于窗口（rows 不在 [start,end] 内）也必须继续翻页。

    这是 OKX 原实现「游标取过滤后的行 ⇒ 首批无窗口行 ⇒ 立即返回空」的回归点。
    """
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        if len(calls) == 1:                     # 整页都比窗口新
            return _rows(cursor - 999 * MIN, 1000)
        return _rows(cursor - 499 * MIN, 500)   # 覆盖窗口

    pages = paginate_backwards(
        fetch, initial_cursor=100 * 1000 * MIN, start_ms=0,
        page_limit=1000, next_cursor=lambda oldest: oldest - MIN, label="t",
    )
    assert len(pages) == 2
    assert len(calls) == 2


def test_empty_page_stops():
    pages = paginate_backwards(
        lambda c: [], initial_cursor=1000 * MIN, start_ms=0,
        page_limit=1000, next_cursor=lambda o: o - MIN, label="t",
    )
    assert pages == []


def test_reaching_start_stops():
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return _rows(cursor - 999 * MIN, 1000)  # 整页，但最早 ts < start

    pages = paginate_backwards(
        fetch, initial_cursor=500 * MIN, start_ms=0,
        page_limit=1000, next_cursor=lambda o: o - MIN, label="t",
    )
    assert len(pages) == 1
    assert len(calls) == 1


def test_initial_cursor_not_after_start_returns_empty():
    called = []
    pages = paginate_backwards(
        lambda c: called.append(c) or _rows(c, 10), initial_cursor=100,
        start_ms=100, page_limit=1000, next_cursor=lambda o: o - MIN, label="t",
    )
    assert pages == []
    assert called == []


def test_next_cursor_none_stops():
    """adapter 判定无法继续（如深度上限）→ 保留已拉页并停。"""
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        return _rows(cursor - 999 * MIN, 1000)

    pages = paginate_backwards(
        fetch, initial_cursor=10 * 1000 * MIN, start_ms=0,
        page_limit=1000, next_cursor=lambda o: None, label="t",
    )
    assert len(pages) == 1
    assert len(calls) == 1


def test_max_pages_guard_logs(caplog):
    """游标始终前进但窗口极大 → 到 max_pages 停止并 warning。"""
    with caplog.at_level(logging.WARNING, logger="nanobot_quant.kline_paging"):
        pages = paginate_backwards(
            lambda c: _rows(c - 999 * MIN, 1000),
            initial_cursor=10_000 * 1000 * MIN, start_ms=0,
            page_limit=1000, next_cursor=lambda o: o - MIN,
            max_pages=3, label="guard",
        )
    assert len(pages) == 3
    assert any("max_pages" in r.message for r in caplog.records)


def test_ts_of_seconds_rows():
    """Gate 行 ts 是秒：必须传 ts_of 换算。不传会被当成毫秒 ⇒ oldest <= start_ms
    恒真 ⇒ 只拉一页（2026-10-20 重构实测踩过）。"""
    def fetch(cursor_ms):
        return [[(cursor_ms - i * MIN) // 1000] for i in range(1000)]

    pages = paginate_backwards(
        fetch, initial_cursor=5000 * MIN, start_ms=0, page_limit=1000,
        next_cursor=lambda o: o - MIN, label="sec",
        ts_of=lambda r: int(r[0]) * 1000,
    )
    assert len(pages) >= 2


def test_malformed_batch_stops(caplog):
    with caplog.at_level(logging.WARNING, logger="nanobot_quant.kline_paging"):
        pages = paginate_backwards(
            lambda c: [["not-a-ts"]], initial_cursor=1000,
            start_ms=0, page_limit=1000, next_cursor=lambda o: o - MIN, label="bad",
        )
    assert len(pages) == 1                      # 坏页被收下但停止翻页
    assert any("malformed" in r.message for r in caplog.records)
