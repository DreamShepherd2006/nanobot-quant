"""Step 2 (方案 B 回测): 历史推进数据源 + gate 翻页拉全量单测（无网络）。

Covered:
- fetch_gate_kline_range_paged：多批合并/去重/裁剪、1m 深度 400 截断
- ReplayDataSource：prefetch / seek 窗口尾对齐 / 长度 / 小写列 / Bars 构造
- price_of：当前重放时间收盘价、空数据 0.0（fail-closed）
- 多标的：窗口尾对齐同一 ts；缺数据短窗口
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from nanobot_quant.backtest.replay_data_source import ReplayDataSource
from nanobot_quant.gate_cex_data import fetch_gate_kline_range_paged

TOKENS = [
    {"symbol": "CRCLX", "chain": "solana",
     "address": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
     "gate_symbol": "CRCLX", "okx_symbol": "XCRCL"},
]


def _row(ts: int, close: float, open_=None, high=None, low=None):
    """Gate candlestick row: [ts, qvol, close, high, low, open, bvol, closed]."""
    return [str(ts), "0", str(close),
            str(high if high is not None else close),
            str(low if low is not None else close),
            str(open_ if open_ is not None else close),
            "1", "true"]


def _ts(offset_min: int, base=None) -> int:
    base = base or datetime(2026, 8, 20, tzinfo=timezone.utc)
    return int((base + timedelta(minutes=offset_min)).timestamp())


class TestPagedFetch:
    def test_single_batch_under_1000(self, monkeypatch):
        rows = [_row(_ts(i), float(i)) for i in range(10)]
        seen = {}

        def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
            seen["args"] = (pair, interval, limit, from_ts, to_ts)
            return rows

        monkeypatch.setattr("nanobot_quant.gate_cex_data._request", fake_request)
        df = fetch_gate_kline_range_paged("CRCLX_USDT", _ts(0), _ts(9), bar="15m")
        assert len(df) == 10
        assert df["Close"].iloc[0] == 0.0
        assert df["Close"].iloc[-1] == 9.0
        # to-only 请求（不传 from）：翻页语义需要服务端返回「to 之前最近 limit 根」
        assert seen["args"][2] == 1000
        assert seen["args"][3] is None

    def test_multi_batch_paging_merge_dedup(self, monkeypatch):
        """第一批 1000 根升序（to=end），第二批接续——合并去重裁剪。"""
        base = datetime(2026, 8, 20, tzinfo=timezone.utc)
        batch1 = [_row(_ts(i, base), float(i)) for i in range(1000)]     # 升序 0..999
        batch2 = [_row(_ts(i, base), float(i)) for i in range(-200, 0)]  # 更早 -200..-1
        calls = []

        def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
            calls.append({"from_ts": from_ts, "to_ts": to_ts})
            if len(calls) == 1:  # 第一轮（to=end）
                return batch1
            return batch2  # 第二轮（to = 上一批最早−step）

        monkeypatch.setattr("nanobot_quant.gate_cex_data._request", fake_request)
        # start 截在 -150（只留 1150 根：-150..999）
        df = fetch_gate_kline_range_paged("CRCLX_USDT", _ts(-150, base), _ts(999, base),
                                          bar="15m")
        assert len(calls) == 2
        assert len(df) == 1150
        assert df["Close"].iloc[0] == -150.0
        assert df["Close"].iloc[-1] == 999.0
        assert df.index.is_monotonic_increasing

    def test_depth_limit_400_truncates(self, monkeypatch):
        """第 10 批触发 "Maximum 10000 points ago" 400 → 截断保留已拉批次。"""
        import io
        import urllib.error

        # 批1 = [base−999min, base]，批2 = [base−1999min, base−1000min]（升序 1m）
        batch1 = [_row(_ts(i), float(i)) for i in range(-999, 1)]
        batch2 = [_row(_ts(i), float(i)) for i in range(-1999, -999)]
        calls = {"n": 0}

        def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise urllib.error.HTTPError(
                    "https://api.gateio.ws", 400,
                    "INVALID_PARAM_VALUE", None,
                    io.BytesIO(b'{"label":"INVALID_PARAM_VALUE",'
                               b'"message":"Candlestick too long ago. '
                               b'Maximum 10000 points ago are allowed"}'),
                )
            return batch1 if calls["n"] == 1 else batch2

        monkeypatch.setattr("nanobot_quant.gate_cex_data._request", fake_request)
        df = fetch_gate_kline_range_paged("CRCLX_USDT", _ts(-5000), _ts(0), bar="1m")
        assert calls["n"] == 3          # 第三批被 400 截断
        assert len(df) == 2000          # 前两批保留
        assert df.index[0] == pd.Timestamp(_ts(-1999), unit="s", tz="UTC")
        assert df.index[-1] == pd.Timestamp(_ts(0), unit="s", tz="UTC")

    def test_depth_limit_not_blacklisted(self, monkeypatch):
        """too-long-ago 400 不把 symbol 拉黑（深度限制非交易对问题）。"""
        import io
        import urllib.error

        def fake_request(pair, interval, limit, from_ts=None, to_ts=None):
            raise urllib.error.HTTPError(
                "x", 400, "INVALID_PARAM_VALUE", None,
                io.BytesIO(b'{"label":"INVALID_PARAM_VALUE","message":'
                           b'"Candlestick too long ago. Maximum 10000 points"}'),
            )

        monkeypatch.setattr("nanobot_quant.gate_cex_data._request", fake_request)
        from nanobot_quant.gate_cex_data import blacklist_reason, clear_blacklist
        clear_blacklist()
        df = fetch_gate_kline_range_paged("CRCLX_USDT", _ts(-5000), _ts(0), bar="1m")
        assert df.empty
        assert blacklist_reason("CRCLX") is None  # 未被黑名单


class _FakeFetcher:
    """合成历史：CRCLX 完整 300 根，SPCX 只到 -100（数据不足场景）。"""

    def __init__(self, bars=300, short_bars=100):
        self.base = datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.bars = bars
        self.short_bars = short_bars

    def _df(self, n, start_price=100.0):
        idx = [self.base + timedelta(minutes=15 * i) for i in range(n)]
        closes = [start_price + 0.1 * i for i in range(n)]
        return pd.DataFrame(
            {"Open": closes, "High": [c + 0.5 for c in closes],
             "Low": [c - 0.5 for c in closes], "Close": closes,
             "Volume": [1000.0] * n},
            index=idx,
        )

    def __call__(self, pair, start_ts, end_ts, bar):
        if pair.startswith("CRCLX"):
            return self._df(self.bars)
        return self._df(self.short_bars, start_price=50.0)


class TestReplayDataSource:
    def _ds(self, symbols=None, **kw):
        kw.setdefault("tokens_json", TOKENS)
        kw.setdefault("timestep", "15min")
        kw.setdefault("fetcher", _FakeFetcher())
        ds = ReplayDataSource(symbols or ["CRCLX", "SPCX"], **kw)
        ds.prefetch()
        return ds

    def test_prefetch_and_bar_times(self):
        ds = self._ds()
        assert len(ds.bar_times) == 300
        assert ds.start_idx == 119
        # 小写列（Bars 契约）
        assert list(ds._frames["CRCLX"].columns) == ["open", "high", "low", "close", "volume"]
        assert ds._frames["CRCLX"].index.is_monotonic_increasing

    def test_seek_window_tail_aligned_and_length(self):
        ds = self._ds(length=120)
        ts = ds.bar_times[200]  # 第 201 根
        ds.seek(ts)
        from lumibot.entities import Asset

        bars = ds.get_historical_prices(Asset(symbol="CRCLX", asset_type="crypto"),
                                        length=120)
        assert len(bars.df) == 120
        assert bars.df.index[-1] == ts          # 窗口尾 = 当前重放时间
        assert bars.df.index[0] == ds.bar_times[200 - 119]
        assert "close" in bars.df.columns        # 小写
        # 窗口按时间切：seek 早于数据起点 → 短窗口（策略 SKIP）
        ds.seek(ds.bar_times[10])
        short = ds.get_historical_prices(
            Asset(symbol="CRCLX", asset_type="crypto"), length=120)
        assert len(short.df) == 11

    def test_price_of_current_bar(self):
        ds = self._ds()
        ds.seek(ds.bar_times[150])
        # CRCLX 收盘 = 100 + 0.1×150
        assert ds.price_of("CRCLX") == pytest.approx(100.0 + 0.1 * 150)

    def test_price_of_no_data_fail_closed(self):
        ds = self._ds()
        assert ds.price_of("NOPE") == 0.0
        ds.seek(ds.bar_times[0])
        ds.seek(ds.bar_times[0] - timedelta(minutes=15))
        assert ds.price_of("CRCLX") == 0.0  # 早于数据起点

    def test_multi_symbol_window_aligned_same_ts(self):
        ds = self._ds(length=120)
        ts = ds.bar_times[250]  # SPCX 只有 100 根（到 bar_times[99]）——窗口不足
        ds.seek(ts)
        from lumibot.entities import Asset

        crclx = ds.get_historical_prices(
            Asset(symbol="CRCLX", asset_type="crypto"), length=120)
        spcx = ds.get_historical_prices(
            Asset(symbol="SPCX", asset_type="crypto"), length=120)
        assert len(crclx.df) == 120
        assert len(spcx.df) == 100  # 数据不足 → 短窗口（策略 min_history SKIP）
        # 短窗口尾不越界；价格 = 该 symbol 最后收盘（无新 bar 时与实盘一致返回旧价）
        assert spcx.df.index[-1] <= ts
        assert ds.price_of("SPCX") == pytest.approx(50.0 + 0.1 * 99)

    def test_net_value(self):
        ds = self._ds()
        ds.seek(ds.bar_times[100])
        # cash 200 + 持仓 0.5 CRCLX × 价(110)
        nv = ds.net_value({"USDT": 200.0}, {"CRCLX": 0.5})
        assert nv == pytest.approx(200.0 + 0.5 * 110.0)
