"""多标的 TD 看板：引擎分派（futu / base）、代码解析、cd 块与页面选项一致性。

588000 只认 cd13（buy9 是下跌中继），所以看板必须能选它并用
``td_sequential._DeMarkEngine`` 算 countdown —— 这里锁死引擎分派与展示字段。
"""

from __future__ import annotations

import pandas as pd
import pytest

from nanobot_quant.ashare import td_panel as tp


def _mk_bars(n_up: int = 12, n_down: int = 30, start: str = "2026-11-25 09:30") -> pd.DataFrame:
    """先涨后跌的单日分钟线。

    ``_DeMarkEngine`` 的 setup 带 flip 前置（``close[i-1] >= close[i-5]``），
    纯单调下跌永远数不出 9 —— 必须先有上涨段才能起计数。
    """
    dts = pd.date_range(start, periods=n_up + n_down, freq="1min")
    up = [2.880 + i * 0.001 for i in range(n_up)]
    down = [up[-1] - i * 0.002 for i in range(1, n_down + 1)]
    closes = up + down
    opens = [c + 0.0005 for c in closes]
    df = pd.DataFrame({
        "dt": dts,
        "open": opens,
        "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.0005 for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [1.0] * len(closes),
    })
    df["date"] = df["dt"].dt.strftime("%Y%m%d")
    return df


def test_engine_base_adds_countdown_columns():
    out = tp.setup_by_session(_mk_bars(), engine="base")
    for col in ("buy_setup_count", "sell_setup_count",
                "buy_countdown_count", "sell_countdown_count"):
        assert col in out.columns
    assert int(out["buy_setup_count"].max()) >= 9      # 先涨后跌 ⇒ flip 成立后数到低9
    assert int(out["buy_countdown_count"].max()) >= 13  # 跌够长 ⇒ cd 走满 13
    assert out["buy_countdown_count"].notna().all()


def test_engine_futu_has_no_countdown_columns():
    out = tp.setup_by_session(_mk_bars(), engine="futu")
    assert "buy_setup_count" in out.columns
    assert "buy_countdown_count" not in out.columns
    assert "sell_countdown_count" not in out.columns


def test_describe_period_cd_block_only_with_countdown():
    with_cd = tp.describe_period(tp.setup_by_session(_mk_bars(), engine="base"), "1m")
    assert with_cd["ok"] is True
    assert set(with_cd["buy_cd"]) == {"count", "need", "state"}
    assert with_cd["buy_cd"]["count"] + with_cd["buy_cd"]["need"] == tp.CD_N

    without = tp.describe_period(tp.setup_by_session(_mk_bars(), engine="futu"), "1m")
    assert "buy_cd" not in without and "sell_cd" not in without


@pytest.mark.parametrize("raw,expect", [
    ("588000.SH", "588000.SH"),
    ("sh588000", "588000.SH"),
    ("588000", "588000.SH"),
    ("510500.SH", "510500.SH"),
    (None, tp.DEFAULT_CODE),
])
def test_resolve_code(raw, expect):
    code, spec = tp._resolve_code(raw)
    assert code == expect
    assert spec["tx"] == tp.tx_code(code)


def test_resolve_code_rejects_unknown():
    with pytest.raises(ValueError) as ei:
        tp._resolve_code("159915.SZ")
    assert "159915.SZ" in str(ei.value)
    assert "可选" in str(ei.value)


def test_panel_uses_symbol_engine(monkeypatch):
    """588000 走 base 引擎 ⇒ payload 带 cd 块与 name/engine。"""
    bars = _mk_bars(40)
    monkeypatch.setattr(tp, "fetch_klines", lambda bar="m1", n=640, symbol="": bars)
    monkeypatch.setattr(tp, "drop_unclosed", lambda df, span, now=None: df)
    monkeypatch.setattr(tp, "_in_session", lambda now=None: True)
    tp.clear_cache()

    p = tp.panel("588000.SH", force=True)
    assert p["code"] == "588000.SH"
    assert p["engine"] == "base"
    assert p["name"] == "科创50ETF"
    assert "countdown" in p["note"]
    assert p["periods"]["1m"].get("buy_cd") is not None

    tp.clear_cache()
    p2 = tp.panel("510050.SH", force=True)
    assert p2["engine"] == "futu"
    assert "buy_cd" not in p2["periods"]["1m"]
    tp.clear_cache()


def test_page_options_cover_all_symbols():
    """页面写死的标的选项必须覆盖注册表（防两处漂移）。"""
    from pathlib import Path

    html = (Path(tp.__file__).resolve().parent.parent / "ashare_page.html").read_text("utf-8")
    for code in tp.SYMBOLS:
        assert f'value="{code}"' in html
    assert "td-symbol" in html and "td-name" in html
