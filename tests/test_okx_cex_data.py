"""OKX CEX K 线取数测试（mock session，无网络）。

覆盖 2026-10-20 修复的两处语义：
1. ``fetch_kline_range`` 改用公用反向分页器后，**首批整页晚于窗口**仍能翻到
   窗口数据（原实现「游标取窗口内最早行 ⇒ 首批无窗口行 ⇒ 立即返回空」）；
2. ``confirm`` 字段过滤进行中 bar（``fetch_kline`` / ``fetch_kline_range``）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from nanobot_quant import okx_cex_data as m

BASE = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
STEP_MS = 60_000  # 1m


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"code": "0", "msg": "", "data": self._data}


class _FakeSession:
    """按 OKX semantics 模拟 history-candles：after = 返回严格早于该 ts 的 300 根。"""

    def __init__(self, floor_ms: int, top_ms: int, confirm_last: bool = True):
        self.floor_ms = floor_ms      # 最早可返回的 ts
        self.top_ms = top_ms          # 最新 ts
        self.confirm_last = confirm_last
        self.calls = []

    def get(self, url, params=None, timeout=None):
        after = int(params["after"])
        limit = int(params.get("limit", 300))
        self.calls.append(after)
        rows = []
        # 真实交易所的 bar 对齐到周期网格；after 为不含端点（严格早于）
        ts = min(((after - 1) // STEP_MS) * STEP_MS, self.top_ms)
        while ts >= self.floor_ms and len(rows) < limit:
            confirmed = "1" if (self.confirm_last or ts != self.top_ms) else "0"
            rows.append([str(ts), "100", "101", "99", "100.5", "10", "10", "1005", confirmed])
            ts -= STEP_MS
        return _Resp(rows)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def test_range_first_page_newer_than_window_still_pages():
    """窗口在 4 天前、每页只覆盖 5 小时：首批整页晚于 end 仍必须继续翻页。"""
    sess = _FakeSession(floor_ms=_ms(BASE - timedelta(days=30)), top_ms=_ms(BASE))
    start = (BASE - timedelta(days=4)).strftime("%Y-%m-%d")
    end = (BASE - timedelta(days=3)).strftime("%Y-%m-%d")

    df = m.fetch_kline_range("SOL", start, end, bar="1m", session=sess)

    assert len(df) == 1441, f"expected full day of 1m bars, got {len(df)}"
    assert len(sess.calls) > 1, "must paginate more than once"
    assert df.index[0] == datetime(2026, 10, 16, tzinfo=timezone.utc)
    assert df.index[-1] == datetime(2026, 10, 17, tzinfo=timezone.utc)


def test_range_empty_window_in_future_returns_empty():
    sess = _FakeSession(floor_ms=_ms(BASE - timedelta(days=30)), top_ms=_ms(BASE))
    df = m.fetch_kline_range("SOL", "2027-01-01", "2027-01-02", bar="1m", session=sess)
    assert df.empty
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_range_stops_when_no_more_data():
    """历史只到 floor：翻到底返回空页即停，不挂死、不报错。"""
    sess = _FakeSession(floor_ms=_ms(BASE - timedelta(hours=1)), top_ms=_ms(BASE))
    start = (BASE - timedelta(days=10)).strftime("%Y-%m-%d")
    end = BASE.strftime("%Y-%m-%d")
    df = m.fetch_kline_range("SOL", start, end, bar="1m", session=sess)
    assert len(df) > 0
    assert len(sess.calls) < 10


def test_range_drops_unconfirmed_bar():
    """窗口截止到「上一分钟」：最新那根进行中 bar（confirm=0）不得进入结果。"""
    sess = _FakeSession(floor_ms=_ms(BASE - timedelta(days=2)), top_ms=_ms(BASE))
    start = (BASE - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    end = (BASE - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    df = m.fetch_kline_range("SOL", start, end, bar="1m", session=sess)
    assert not df.empty
    assert df.index.max() <= pd.Timestamp(BASE) - pd.Timedelta(minutes=1)


def test_fetch_kline_drops_unconfirmed():
    """快照路径同样只保留已收盘 bar。"""
    class _SnapSession:
        def get(self, url, params=None, timeout=None):
            rows = [
                [str(_ms(BASE)), "1", "1", "1", "1", "1", "1", "1", "0"],   # 进行中
                [str(_ms(BASE - timedelta(minutes=1))), "1", "1", "1", "1", "1", "1", "1", "1"],
            ]
            return _Resp(rows)

    df = m.fetch_kline("SOL", bar="1m", limit=2, session=_SnapSession())
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp(BASE - timedelta(minutes=1))
