"""A股 日内按交易日切分（2026-09-21 用户拍板）。

背景：A股 日内 bar 在午休/隔夜处并非连续，而 TD 引擎按「相邻行」比较
``close[i-4]``，不切分时 09:35 那根会与上一交易日尾盘 bar 比较，产生跨时段
计数（线上实测：09-18 15:00 Sell Setup 2 → 09-21 09:35 Sell Setup 3）。

拍板结论：A股 + 日内周期按交易日切分、每天从 0 重数（setup 与 countdown 同归零）；
午休不切（同一交易日）；日线/周线不切；美股（字母代码）本次不做。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.td_table_handlers import (
    _engine_run,
    _engine_run_session,
    _is_a_share,
    _session_labels,
    _session_split_enabled,
    _split_by_session_run,
    _stock_src_label,
)

PARAMS = {"setup_period": 9, "compare_length": 4}
COLS = ("buy_setup_count", "sell_setup_count", "buy_countdown_count", "sell_countdown_count")


def _df(times: list[pd.Timestamp], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": closes, "High": [c + 0.5 for c in closes],
         "Low": [c - 0.5 for c in closes], "Close": closes,
         "Volume": [1_000_000] * len(closes)},
        index=pd.DatetimeIndex(times),
    )


def _two_day_df(day1: list[float], day2: list[float],
                t1: str = "2026-09-18", t2: str = "2026-09-21") -> pd.DataFrame:
    """两个交易日的 5m 序列（09:30 起每 5 分钟一根）。"""
    times = list(pd.Timestamp(f"{t1} 09:30") + pd.to_timedelta([5 * i for i in range(len(day1))], unit="m")) \
        + list(pd.Timestamp(f"{t2} 09:30") + pd.to_timedelta([5 * i for i in range(len(day2))], unit="m"))
    return _df(times, list(day1) + list(day2))


def _sell(df: pd.DataFrame) -> list[int]:
    return [int(v) for v in df["sell_setup_count"].tolist()]


# ── ① 生效范围门控 ────────────────────────────────────────────────

@pytest.mark.parametrize("source,ticker,bar,expected", [
    ("stock", "510050", "5m", True),      # A股 ETF 日内 → 切
    ("stock", "601127", "15m", True),     # A股 日内 → 切
    ("stock", "588000", "30m", True),
    ("stock", "510050", "1D", False),     # 一根 bar 一天 → 不切
    ("stock", "510050", "1W", False),
    ("stock", "AAPL", "5m", False),       # 美股（字母代码）→ 本次不做
    ("cex", "SOL", "5m", False),          # 加密 24/7 → 不切
    ("okx_cex", "BTC", "15m", False),
    ("onchainos", "RENDER", "5m", False),
])
def test_session_split_enabled(source, ticker, bar, expected):
    assert _session_split_enabled(source, ticker, bar) is expected


def test_is_a_share():
    assert _is_a_share("510050") and _is_a_share(" 601127 ")
    assert not _is_a_share("AAPL") and not _is_a_share("SOL")
    assert not _is_a_share("51005") and not _is_a_share("5100501")


# ── ② 交易日标签 ─────────────────────────────────────────────────

def test_session_labels_naive_index_is_beijing():
    """naive 索引按北京时间解释（A股 数据源口径）。"""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-09-18 15:00"), pd.Timestamp("2026-09-21 09:35")])
    assert _session_labels(idx) == ["2026-09-18", "2026-09-21"]


def test_session_labels_tz_aware_index():
    idx = pd.DatetimeIndex(["2026-09-21 14:40"]).tz_localize("Asia/Shanghai").tz_convert("UTC")
    assert _session_labels(idx) == ["2026-09-21"]


# ── ③ 切分行为 ───────────────────────────────────────────────────

def test_split_resets_counts_at_day_boundary():
    """日末形成的 Sell Setup 不得跨日延续（线上 2 → 3 的伪计数即此）。"""
    df = _two_day_df([100, 100, 100, 100, 100, 101, 102, 103],
                     [104, 105, 106, 107, 108, 109])
    whole = _engine_run(df, "td_sequential", PARAMS)
    split = _split_by_session_run(df, "td_sequential", PARAMS, _session_labels(df.index))

    assert _sell(whole) == [0, 0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert _sell(split) == [0, 0, 0, 0, 0, 1, 2, 3, 0, 0, 0, 0, 0, 0]
    # 首日两段一致：切分不改变日内计数
    assert _sell(split)[:8] == _sell(whole)[:8]
    # 长度与索引必须与输入对齐（渲染层按下标取数）
    assert len(split) == len(df)
    assert list(split.index) == list(df.index)
    assert all(c in split.columns for c in COLS)


def test_split_segment_without_intraday_flip_cannot_restart_count():
    """引擎要求「价格翻转」才启动计数；次日若日内无翻转（单边行情），该日计数保持 0。

    这是「每天一个独立序列」的应有代价——跨日边界处的翻转不可见；加密 24/7 不受影响。
    """
    df = _two_day_df([103, 102, 101, 100, 99, 98], [99, 100, 101, 102, 103, 104])
    whole = _engine_run(df, "td_sequential", PARAMS)
    split = _split_by_session_run(df, "td_sequential", PARAMS, _session_labels(df.index))

    assert max(_sell(whole)[6:]) > 0
    assert max(_sell(split)[6:]) == 0


def test_split_does_not_break_lunch_break_within_one_day():
    """午休（11:30 → 13:05）属同一交易日 → 只有一个分段，行为等同不切分。"""
    times = [pd.Timestamp("2026-09-21 11:15"), pd.Timestamp("2026-09-21 11:20"),
             pd.Timestamp("2026-09-21 11:25"), pd.Timestamp("2026-09-21 11:30"),
             pd.Timestamp("2026-09-21 13:05"), pd.Timestamp("2026-09-21 13:10"),
             pd.Timestamp("2026-09-21 13:15"), pd.Timestamp("2026-09-21 13:20")]
    df = _df(times, [100, 100, 100, 100, 100, 101, 102, 103])
    whole = _engine_run(df, "td_sequential", PARAMS)
    split = _split_by_session_run(df, "td_sequential", PARAMS, _session_labels(df.index))

    assert set(_session_labels(df.index)) == {"2026-09-21"}
    assert _split_by_session_run(df, "td_sequential", PARAMS, _session_labels(df.index)) is not None
    assert _sell(split) == _sell(whole)
    assert all(c in split.columns for c in COLS)


def test_engine_run_session_routes_to_plain_engine_when_gate_misses():
    df = _two_day_df([100, 100, 100, 100, 100, 101, 102, 103],
                     [104, 105, 106, 107, 108, 109])
    plain = _engine_run(df, "td_sequential", PARAMS)
    for args in (("stock", "510050", "1D"), ("stock", "AAPL", "5m"), ("cex", "SOL", "5m")):
        out = _engine_run_session(df, "td_sequential", PARAMS, *args)
        assert _sell(out) == _sell(plain), args


def test_engine_run_session_splits_for_a_share_intraday():
    df = _two_day_df([100, 100, 100, 100, 100, 101, 102, 103],
                     [104, 105, 106, 107, 108, 109])
    out = _engine_run_session(df, "td_sequential", PARAMS, "stock", "510050", "5m")
    assert _sell(out)[8:] == [0] * 6


# ── ④ 标注可见 ───────────────────────────────────────────────────

def test_stock_src_label_marks_split():
    assert _stock_src_label("510050", "5m").endswith("按交易日切分")
    assert not _stock_src_label("510050", "1D").endswith("按交易日切分")
    assert not _stock_src_label("AAPL", "5m").endswith("按交易日切分")
