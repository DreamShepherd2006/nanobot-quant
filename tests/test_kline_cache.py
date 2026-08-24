"""KlineCache (S2 incremental kline) unit + data-source integration tests.

设计：docs/quant-system.md §22.6 —— 数据源层增量 K 线缓存：
首轮全量预取 → 每轮只拉最近 2 根 → 按 ts 合并（忽略旧 / 覆盖尾行 /
恰 +1 周期 append / 跳跃缺口 → 全量重拉）。契约不变：Gate 缓存已收盘
序列（策略不 drop）；OnchainOS 缓存含进行中 bar（策略 drop 最后 1 根）。
"""

import pandas as pd
import pytest

from nanobot_quant.data.kline_cache import KlineCache


# ── helpers ─────────────────────────────────────────────────────

def _df(n=130, start=None, period_s=60, tz="UTC", col=None):
    """Synthetic OHLCV frame: tz-aware UTC DatetimeIndex, ascending.

    end 默认 = now（自适应增量 limit 依赖缓存尾与 wall clock 的
    时间差；固定历史日期会算出巨大 elapsed → limit 封顶 1000）。
    """
    if start is None:
        idx = pd.date_range(
            end=pd.Timestamp.now(tz="UTC"), periods=n,
            freq=f"{period_s}s", tz=tz,
        )
    else:
        idx = pd.date_range(
            start, periods=n, freq=f"{period_s}s", tz=tz,
        )
    base = pd.DataFrame(
        {
            "open": [10.0 + i * 0.1 for i in range(n)],
            "high": [11.0 + i * 0.1 for i in range(n)],
            "low": [9.0 + i * 0.1 for i in range(n)],
            "close": [10.5 + i * 0.1 for i in range(n)],
            "volume": [100.0] * n,
        },
        index=idx,
    )
    return base if col is None else base.rename(columns=str.upper)


class _FakeFetch:
    """fetch(symbol, bar, limit) stub backed by a growing frame."""

    def __init__(self, df):
        self.df = df.copy()
        self.calls = []  # (symbol, bar, limit) 记录

    def __call__(self, symbol, bar, limit):
        self.calls.append((symbol, bar, limit))
        return self.df.iloc[-limit:]

    def grow(self, rows=1, period_s=60):
        """Append ``rows`` new closed bars (next timestamps)."""
        last = self.df.index[-1]
        new_idx = pd.date_range(
            last + pd.Timedelta(seconds=period_s), periods=rows,
            freq=f"{period_s}s", tz=self.df.index.tz)
        tail = self.df.iloc[-1]
        for ts in new_idx:
            self.df.loc[ts] = [v + 0.1 for v in tail.values]
        return self


# ── KlineCache unit tests ────────────────────────────────────────

class TestRebuild:
    def test_first_get_prefetches_full_window(self):
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        out = cache.get("SOL", "1m", 120)
        assert len(out) == 120
        assert src.calls == [("SOL", "1m", 120)]
        pd.testing.assert_frame_equal(out, src.df.iloc[-120:])

    def test_bar_change_rebuilds(self):
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        cache.get("SOL", "1D", 120)
        assert src.calls[-1] == ("SOL", "1D", 120)
        assert len(cache) == 1  # 同一 symbol 条目被重建复用

    def test_length_grow_rebuilds(self):
        src = _FakeFetch(_df(300))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        cache.get("SOL", "1m", 200)
        assert src.calls[-1] == ("SOL", "1m", 200)  # 全量重建（长度不足）
        assert len(cache.get("SOL", "1m", 200)) == 200

    def test_fetch_failure_raises_on_rebuild(self):
        src = _FakeFetch(_df(130))

        def boom(symbol, bar, limit):
            raise RuntimeError("CLI down")
        cache = KlineCache(boom)
        with pytest.raises(RuntimeError, match="CLI down"):
            cache.get("SOL", "1m", 120)


class TestIncremental:
    def test_gate_style_incremental_matches_full(self):
        """Gate 型（fetch 返回已收盘序列）：逐根增量 vs 全量 → 尾部一致。"""
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        # 推进 9 轮，每轮 1 根新收盘 bar
        for _ in range(9):
            src.grow(1)
            out = cache.get("SOL", "1m", 120)
            assert len(out) == 120
            pd.testing.assert_frame_equal(out, src.df.iloc[-120:])
        # 增量路径只拉 2 根（每轮），非全量
        assert all(c[2] == 2 for c in src.calls[1:10])

    def test_onchainos_style_in_progress_overwrite(self):
        """OnchainOS 型（fetch 返回含进行中 bar）：
        尾行 ts 相同 → 覆盖（进行中值刷新/收盘定格）；新周期 → append。"""
        closed = _df(20)  # 20 根已收盘
        cur_ts = closed.index[-1] + pd.Timedelta(seconds=60)  # 进行中 bar ts
        state = {"cur": pd.Series(
            [30.0, 31.0, 29.0, 30.5, 500.0], index=closed.columns, name=cur_ts)}

        def fetch(symbol, bar, limit):
            # 最近 limit 根 = limit-1 根已收盘 + 1 根进行中
            tail = closed.iloc[-(limit - 1):]
            return pd.concat([tail, state["cur"].to_frame().T])

        cache = KlineCache(fetch)
        out1 = cache.get("SOL", "1m", 10)
        assert len(out1) == 10
        assert out1.index[-1] == cur_ts
        # 进行中 bar 值更新（同一 ts）→ 覆盖尾行
        state["cur"] = pd.Series(
            [30.0, 31.0, 29.0, 31.2, 500.0], index=closed.columns, name=cur_ts)
        out2 = cache.get("SOL", "1m", 10)
        assert out2.index[-1] == cur_ts
        assert out2.iloc[-1]["close"] == 31.2
        # 进行中收盘定格 + 新进行中 bar 出现 → 覆盖 + append
        closed = pd.concat([closed, state["cur"].to_frame().T])
        new_ts = cur_ts + pd.Timedelta(seconds=60)
        state["cur"] = pd.Series(
            [31.0, 32.0, 30.0, 31.5, 600.0], index=closed.columns, name=new_ts)
        out3 = cache.get("SOL", "1m", 10)
        assert len(out3) == 10
        assert out3.index[-1] == new_ts
        assert out3.iloc[-2]["close"] == 31.2  # 上一根已定格为收盘值

    def test_no_new_bar_skips(self):
        """无新 bar（fetch 返回全 ≤ 缓存尾）→ 缓存不增长、返回一致。"""
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        n = len(cache._entries["SOL"].df)
        for _ in range(3):
            out = cache.get("SOL", "1m", 120)
            assert len(out) == 120
        assert len(cache._entries["SOL"].df) == n  # 未增长

    def test_3m_period_incremental_no_gap(self):
        """3m 周期（Step 4 新增）：bar 间隔 180s，增量 +1 不得误判 gap。

        回归：旧 _BAR_SECONDS 缺 3m → fallback 60s → diff=180s≠60s 判 gap
        → 每轮全量重拉（2026-08-24 HF Space 实测，S2 增量失效）。
        """
        src = _FakeFetch(_df(130, period_s=180))
        cache = KlineCache(src)
        cache.get("SOL", "3m", 120)
        src.grow(1, period_s=180)
        before = len(src.calls)
        out = cache.get("SOL", "3m", 120)
        assert len(out) == 120
        # 增量路径：仅 limit=2 拉取（非全量 120）
        assert all(c[2] == 2 for c in src.calls[before:])
        pd.testing.assert_frame_equal(out, src.df.iloc[-120:])

    def test_unknown_period_raises_keyerror(self):
        """未知周期（不在注册表）→ 增量路径显式 KeyError，不静默回退 60s。"""
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "7Q", 120)  # 重建成功（fake fetch 不校验 bar）
        with pytest.raises(KeyError):
            cache.get("SOL", "7Q", 120)  # 第二次同 bar → 增量路径 → KeyError

    def test_gap_triggers_full_refetch(self):
        """新 bar ts 跳跃 > 1 周期 → 缺口 → 全量重拉重建。"""
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        # 模拟缺根：下一根直接跳 2 个周期（+120s）
        last = src.df.index[-1]
        jump_ts = last + pd.Timedelta(seconds=120)
        src.df.loc[jump_ts] = [v + 0.1 for v in src.df.iloc[-1].values]
        out = cache.get("SOL", "1m", 120)
        assert len(out) == 120
        assert src.calls[-1] == ("SOL", "1m", 120)  # 全量重拉
        pd.testing.assert_frame_equal(out, src.df.iloc[-120:])

    def test_multi_new_bars_appended_without_false_gap(self):
        """跳过一轮后 limit=2 返回 2 根新收盘 bar → 顺序逐根 append，不误判 gap。

        S3a 场景调度下心跳边界抖动可能跳过一轮（不拉数据），下一轮增量
        limit=2 会拿到 2 根新收盘 bar；旧实现逐根相对原始尾判定，第二根
        diff=2×period 误判缺口触发全量重拉。修复：append 后更新尾 ts。
        """
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        src.grow(2)  # 跳过一轮：一次长 2 根
        before = len(src.calls)
        out = cache.get("SOL", "1m", 120)
        assert len(out) == 120
        # 增量路径：仅 limit=2 拉取，无全量重拉
        assert all(c[2] == 2 for c in src.calls[before:])
        pd.testing.assert_frame_equal(out, src.df.iloc[-120:])

    def test_skip_rounds_adaptive_fetch_limit(self, monkeypatch):
        """S3a 多场景：调用方跳过多轮才访问（如 mid=5m 每 5 轮访问 1m
        缓存，期间产生 5 根新 bar）→ 增量 limit 按距缓存尾的 elapsed
        自适应拉取，不误判 gap 全量重拉。"""
        src = _FakeFetch(_df(125))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)   # 首轮 prefetch，缓存尾 = now
        src.grow(5)                     # 世界推进 5 分钟：src 尾 = now + 5min
        real_now = pd.Timestamp.now(tz="UTC")
        # 模拟调用方 5 分钟未访问（wall clock 推进）
        monkeypatch.setattr(
            "nanobot_quant.data.kline_cache._now_for",
            lambda ts: real_now + pd.Timedelta(minutes=5),
        )
        before = len(src.calls)
        out = cache.get("SOL", "1m", 120)
        assert len(out) == 120
        # 自适应 limit：elapsed≈5 → need=7（5 根缺口 + 2 容差），非全量 120
        assert src.calls[-1][2] == 7
        assert src.calls[-1][2] != 120
        # 数据一致：out 是 src 的连续子序列（时间戳对齐、值相同）
        pd.testing.assert_frame_equal(
            out, src.df.loc[out.index], check_freq=False,
        )

    def test_incremental_fetch_failure_keeps_cache(self):
        """增量拉取失败 → 保留缓存返回（下一轮再试），不中断循环。"""
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        n = len(cache._entries["SOL"].df)

        def broken(symbol, bar, limit):
            raise RuntimeError("rate limited")
        cache._fetch = broken
        out = cache.get("SOL", "1m", 120)  # 不抛异常
        assert len(out) == 120
        assert len(cache._entries["SOL"].df) == n


class TestTrimAndCopy:
    def test_trim_keeps_window_bounded(self):
        src = _FakeFetch(_df(130))
        cache = KlineCache(src, keep_extra=8)
        cache.get("SOL", "1m", 120)
        for _ in range(20):
            src.grow(1)
            cache.get("SOL", "1m", 120)
        assert len(cache._entries["SOL"].df) <= 128  # 120 + keep_extra

    def test_length_shrink_trims_without_refetch(self):
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        cache.get("SOL", "1m", 120)
        calls = len(src.calls)
        out = cache.get("SOL", "1m", 60)
        assert len(out) == 60
        # 无全量重拉（仅常规 limit=2 增量），缓存裁剪到 60+keep_extra
        assert all(c[2] == 2 for c in src.calls[calls:])
        assert len(cache._entries["SOL"].df) <= 68

    def test_get_returns_copy(self):
        src = _FakeFetch(_df(130))
        cache = KlineCache(src)
        out1 = cache.get("SOL", "1m", 120)
        out1.loc[out1.index[-1], "close"] = 999.0  # 修改返回值
        out2 = cache.get("SOL", "1m", 120)
        assert out2.iloc[-1]["close"] != 999.0
