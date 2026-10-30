"""OKX 区间取数必须保留时分秒（2026-10-30 数据冻结 3.7 小时的根因）。

故障现场：4 个标的的「最后 bar」全部停在 `2026-10-30 00:00:00 UTC`，页面
一切正常（信号照旧 HOLD、无报错），实际 K 线缓存已 3.7 小时没推进。

根因链：
1. `KlineCache._incremental` 按 [缓存尾, now] 传**秒级**边界；
2. `data_sources/okx_cex.py:fetch_kline` 却用 `strftime("%Y-%m-%d")` 把它
   截断成日期 ⇒ 区间被压成「当天 00:00 → 当天 00:00」；
3. `fetch_kline_range` 只返回当天第一根（或空）⇒ 缓存 append 0 根后
   保留旧数据、**静默**返回（"无新 bar" 不是错误分支）。

Gate（`int(start.timestamp())`）没这个问题，所以只在 okx_cex 通道暴露。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from nanobot_quant.data_sources import okx_cex as ds


def test_range_fetch_keeps_time_of_day(monkeypatch):
    """区间边界必须带时分秒传到 fetch_kline_range。"""
    captured = {}

    def _fake_range(inst, *, start, end, bar=None, **_kw):
        captured.update(inst=inst, start=start, end=end, bar=bar)
        return pd.DataFrame()

    monkeypatch.setattr(ds, "okx_ticker", lambda symbol, tokens=None: "BTC-USDT")
    monkeypatch.setattr(ds, "_fetch_kline_range_okx", _fake_range)

    start = datetime(2026, 10, 30, 3, 41, tzinfo=timezone.utc)
    end = datetime(2026, 10, 30, 3, 42, tzinfo=timezone.utc)
    ds.fetch_kline("BTC", bar="1m", start=start, end=end)

    # 关键断言：不能再是 "2026-10-30"（那样区间长度 = 0）
    assert captured["start"] != "2026-10-30"
    assert "03:41" in captured["start"], captured["start"]
    assert "03:42" in captured["end"], captured["end"]
    assert datetime.fromisoformat(captured["start"]) == start
    assert datetime.fromisoformat(captured["end"]) == end


def test_okx_range_parser_accepts_full_iso(monkeypatch):
    """okx_cex_data.fetch_kline_range 必须能把带时分秒的 ISO 解析成精确 ms。"""
    from nanobot_quant import okx_cex_data

    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"code": "0", "data": []}

    class _Sess:
        def get(self, url, params=None, timeout=None):
            seen["after"] = int(params["after"])
            return _Resp()

    start = datetime(2026, 10, 30, 3, 41, tzinfo=timezone.utc)
    end = datetime(2026, 10, 30, 3, 42, tzinfo=timezone.utc)
    monkeypatch.setattr(okx_cex_data, "inst_id_for", lambda t: "BTC-USDT")
    monkeypatch.setattr(okx_cex_data, "_RATE_DELAY", 0)
    okx_cex_data.fetch_kline_range("BTC", start.isoformat(), end.isoformat(),
                                   bar="1m", session=_Sess())

    # 首游标 = end + 1ms（秒级精度体现在毫秒差上；截断成日期则差 24h）
    assert seen["after"] == int(end.timestamp() * 1000) + 1
