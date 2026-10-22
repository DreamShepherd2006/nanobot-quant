"""回测数据源（Gate CEX / OKX CEX）测试。

2026-10-20 拍板：**跨所回测放行 + 强制标注**——OKX 数据只作参数粗筛，
不作参数裁决依据（1m TD 九连事件跨所一致率 0.23–0.59，事件级收益归属
不成立）。本组测试锁定：

1. 源注册表：默认 Gate、未知源 fail-closed；
2. 符号解析：Gate 用 ``gate_pair``、OKX 用 ``okx_ticker``；
3. OKX 适配层：unix 秒 → ISO 字符串；「拉满」（start<=0）按单次上限截窗；
4. 单次上限：OKX 有闸（10,000 根）、Gate 不设本地闸；
5. 启动前探测：OKX 源取不到行情的标的不静默跳过；
6. 驱动：未知源 ValueError、结果/参数快照带数据源字段；
7. 展示层：页面下拉 + 跨所提示条 + markdown「数据源」行。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import nanobot_quant
from nanobot_quant.backtest import driver as drv
from nanobot_quant.backtest import fetchers as F
from nanobot_quant.backtest.driver import BacktestDriver
from nanobot_quant.backtest.fetchers import (
    CROSS_VENUE_NOTE,
    DEFAULT_SOURCE,
    SOURCE_LABELS,
    is_cross_venue,
    make_fetcher,
    max_bars_for_source,
    normalize_source,
    resolve_symbol,
    source_label,
    unavailable_symbols,
)
from nanobot_quant.backtest_markdown import render_markdown
from nanobot_quant.exec_params import DEFAULT_EXEC_PARAMS
from nanobot_quant.kline_paging import KlineRangeTooLarge, max_bars_for


# ── 注册表 ───────────────────────────────────────────────────────────


def test_default_source_is_gate_and_labels():
    assert DEFAULT_SOURCE == "gate_cex"
    assert normalize_source(None) == "gate_cex"
    assert normalize_source("") == "gate_cex"
    assert normalize_source(" OKX_CEX ") == "okx_cex"
    assert source_label("okx_cex") == "OKX CEX"
    assert set(SOURCE_LABELS) == {"gate_cex", "okx_cex"}


def test_unknown_source_fail_closed():
    """未知源必须报错，不许静默回退到默认源。"""
    with pytest.raises(ValueError):
        normalize_source("binance")
    with pytest.raises(ValueError):
        normalize_source("onchainos")


def test_cross_venue_flag_and_note():
    assert is_cross_venue("gate_cex") is False
    assert is_cross_venue("okx_cex") is True
    # 强制标注：必须写明「不作参数裁决依据」
    assert "不作参数裁决依据" in CROSS_VENUE_NOTE


def test_resolve_symbol_per_source():
    tokens = [{"symbol": "CRCLX", "gate_symbol": "CRCLXUSDT", "okx_symbol": "XCRCL"}]
    assert resolve_symbol("CRCLX", "gate_cex", tokens) == "CRCLX_USDT"
    assert resolve_symbol("CRCLX", "okx_cex", tokens) == "XCRCL"


def test_make_fetcher_selects_per_source():
    from nanobot_quant.gate_cex_data import fetch_gate_kline_range_paged

    # 2026-10-22：fetcher 多包了一层合成周期支持，改断言“底层取数路径”而非函数身份
    assert make_fetcher("gate_cex").__wrapped_fetch__ is fetch_gate_kline_range_paged
    assert make_fetcher("okx_cex").__wrapped_fetch__ is F._okx_fetch


# ── OKX 适配层 ───────────────────────────────────────────────────────


def test_okx_fetcher_converts_ts_to_iso(monkeypatch):
    calls: dict = {}

    def fake(ticker, start, end, bar="1D"):
        calls.update(ticker=ticker, start=start, end=end, bar=bar)
        return pd.DataFrame()

    monkeypatch.setattr(F, "fetch_kline_range", fake)
    start = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
    F._okx_fetch("SOL", start, end, "1m")
    assert calls["ticker"] == "SOL"
    assert calls["start"].startswith("2026-10-01T00:00:00")
    assert calls["end"].startswith("2026-10-02T00:00:00")
    assert calls["bar"] == "1m"


def test_okx_fetcher_clamps_open_start_to_single_request_cap(monkeypatch):
    """start<=0（页面「拉满」）→ 按单次上限截最近窗口（并在结果里注明）。"""
    calls: dict = {}

    def fake(ticker, start, end, bar="1D"):
        calls.update(start=start, end=end, bar=bar)
        return pd.DataFrame()

    monkeypatch.setattr(F, "fetch_kline_range", fake)
    end = int(datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())
    F._okx_fetch("SOL", 0, end, "1m")
    span = (
        datetime.fromisoformat(calls["end"]) - datetime.fromisoformat(calls["start"])
    ).total_seconds()
    assert abs(span - max_bars_for("1m") * 60) <= 60


def test_max_bars_for_source():
    # Gate 由服务端 400 深度闸门兜底 —— 本地不设闸（零行为变化）
    assert max_bars_for_source("gate_cex", "1m") is None
    assert max_bars_for_source("okx_cex", "1m") == max_bars_for("1m") == 10_000


# ── 启动前探测 ───────────────────────────────────────────────────────


def test_unavailable_symbols_gate_short_circuits():
    assert unavailable_symbols(["CRCLX"], "gate_cex", []) == []


def test_unavailable_symbols_okx_reports_reason(monkeypatch):
    def boom(ticker):
        raise RuntimeError("51001 Instrument ID does not exist")

    monkeypatch.setattr(F, "fetch_ticker", boom)
    bad = unavailable_symbols(
        ["CRCLX", "SOL"], "okx_cex", [{"symbol": "CRCLX", "okx_symbol": "XCRCL"}]
    )
    assert [b[0] for b in bad] == ["CRCLX", "SOL"]
    assert "51001" in bad[0][1]


def test_unavailable_symbols_okx_empty_ticker_is_unavailable(monkeypatch):
    """OKX 对不存在的交易对不报错、返回空 dict —— 不能只看异常。"""
    monkeypatch.setattr(F, "fetch_ticker", lambda ticker: {})
    bad = unavailable_symbols(["NOTACOIN"], "okx_cex", [])
    assert bad and bad[0][0] == "NOTACOIN"
    assert "无行情" in bad[0][1]


def test_unavailable_symbols_okx_all_good(monkeypatch):
    monkeypatch.setattr(F, "fetch_ticker", lambda ticker: {"last": "1"})
    assert unavailable_symbols(["SOL"], "okx_cex", []) == []


# ── 驱动层 ───────────────────────────────────────────────────────────


def _params(**overrides) -> dict:
    p = {
        k: (dict(v) if isinstance(v, dict) else v)
        for k, v in DEFAULT_EXEC_PARAMS.items()
    }
    p["scenes"] = {
        k: dict(v) for k, v in (DEFAULT_EXEC_PARAMS["scenes"] or {}).items()
    }
    p["scenes"]["mid"].update(
        {
            "enabled": True,
            "sleeptime": "15m",
            "symbols": ["CRCLX"],
            "batches": 2,
            "quantity_mode": "fixed_amount",
            "td_fixed_amount": 4.0,
        }
    )
    p["td_bars"] = 40
    p.update(overrides)
    return p


class _FakeFetcher:
    def __init__(self, closes: list[float]):
        self.closes = closes

    def __call__(self, pair: str, start_ts, end_ts, bar: str) -> pd.DataFrame:
        idx = [
            datetime(2026, 8, 1, tzinfo=timezone.utc) + pd.Timedelta(minutes=15) * i
            for i in range(len(self.closes))
        ]
        return pd.DataFrame(
            {
                "Open": self.closes,
                "High": [c * 1.001 for c in self.closes],
                "Low": [c * 0.999 for c in self.closes],
                "Close": self.closes,
                "Volume": [1000.0] * len(self.closes),
            },
            index=pd.DatetimeIndex(idx),
        )


def test_driver_rejects_unknown_source():
    with pytest.raises(ValueError):
        BacktestDriver(
            scene="mid",
            params=_params(),
            fetcher=_FakeFetcher([100.0] * 60),
            source="binance",
        )


def test_driver_records_source_fields(tmp_path, monkeypatch):
    """默认 Gate：结果带 source/source_label/cross_venue，均非跨所。"""
    d = BacktestDriver(
        scene="mid",
        params=_params(),
        symbols=["CRCLX"],
        start_ts=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end_ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
        fetcher=_FakeFetcher([100.0 + i * 0.01 for i in range(300)]),
        ledger_dir=tmp_path,
        progress_path=tmp_path / "p.json",
    )
    res = d.run()
    assert res["source"] == "gate_cex"
    assert res["source_label"] == "Gate CEX"
    assert res["cross_venue"] is False
    assert res["cross_venue_note"] is None
    assert res["backtest_config"]["source"] == "gate_cex"


def test_driver_okx_source_marks_cross_venue(tmp_path, monkeypatch):
    """OKX 源：结果必须带跨所警告 + source_note（留空 = 取上限窗口）。"""
    monkeypatch.setattr(drv, "unavailable_symbols", lambda *a, **k: [])
    d = BacktestDriver(
        scene="mid",
        params=_params(),
        symbols=["CRCLX"],
        start_ts=None,
        end_ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
        fetcher=_FakeFetcher([100.0 + i * 0.01 for i in range(300)]),
        source="okx_cex",
        ledger_dir=tmp_path,
        progress_path=tmp_path / "p.json",
    )
    res = d.run()
    assert res["source"] == "okx_cex"
    assert res["cross_venue"] is True
    assert "不作参数裁决依据" in res["cross_venue_note"]
    assert res["source_note"] and "单次上限" in res["source_note"]


def test_driver_okx_unavailable_symbols_fail_closed(tmp_path, monkeypatch):
    """有标的取不到行情 → 启动即报错并列出原因（不静默少标的）。"""
    monkeypatch.setattr(
        drv, "unavailable_symbols", lambda *a, **k: [("CRCLX", "XCRCL: 51001")]
    )
    d = BacktestDriver(
        scene="mid",
        params=_params(),
        symbols=["CRCLX"],
        fetcher=_FakeFetcher([100.0] * 60),
        source="okx_cex",
        ledger_dir=tmp_path,
        progress_path=tmp_path / "p.json",
    )
    with pytest.raises(RuntimeError) as ei:
        d.run()
    assert "取不到以下标的" in str(ei.value) and "CRCLX" in str(ei.value)


# ── 展示层 ───────────────────────────────────────────────────────────


def test_page_has_source_selector_and_cross_venue_warning():
    html = (Path(nanobot_quant.__file__).parent / "backtest_page.html").read_text(
        encoding="utf-8"
    )
    assert 'id="source"' in html
    assert "OKX CEX（跨所参考）" in html
    assert "cross-venue-banner" in html
    assert "不作参数裁决依据" in html
    assert "source: $(\"source\")" in html  # 请求体带上源
    assert "onSourceChange" in html


def test_markdown_marks_cross_venue():
    res = {
        "run_id": "t1",
        "scene": "mid",
        "symbols": ["SOL"],
        "timestep": "15m",
        "source": "okx_cex",
        "source_label": "OKX CEX",
        "cross_venue": True,
        "cross_venue_note": CROSS_VENUE_NOTE,
        "source_note": "未指定起始时间 → 按 OKX CEX 单次上限取最近窗口",
        "bars": 10,
        "fetched_bars": 12,
        "initial_quote": 100.0,
        "batches": 1,
        "backtest_config": {},
    }
    md = render_markdown(res)
    assert "数据源" in md and "跨所参考" in md
    assert "不作参数裁决依据" in md
    assert "单次上限" in md
