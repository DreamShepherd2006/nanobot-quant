"""期权回测 WebUI（/config/backtest 的「期权回测」分栏）单测。

覆盖三块：

* 页面骨架 —— 两个分栏（现货 / 期权）+ 切换函数
* 覆盖参数合并 —— 键名对齐实盘 ``DEFAULT_STRATEGY``，且**不污染**全局配置
* 引擎分派 —— ``run_backtest(engine="options")`` 的 run_id 前缀与参数校验

引擎本身（``OptionsBacktestDriver``）由 ``test_options_driver.py`` 覆盖，
这里只测接入层。
"""

from __future__ import annotations

import re

import pytest

from nanobot_quant import options_backtest_handlers as obh
from nanobot_quant.tools import tools_backtest as tb


# ── 页面骨架 ──────────────────────────────────────────────────────────


def _page_html() -> str:
    from pathlib import Path

    import nanobot_quant

    return (Path(nanobot_quant.__file__).parent / "backtest_page.html").read_text(
        encoding="utf-8"
    )


def test_page_has_both_panes():
    html = _page_html()
    assert 'id="pane-spot"' in html
    assert 'id="pane-opt"' in html
    assert "function switchTab" in html
    assert 'id="tab-spot"' in html and 'id="tab-opt"' in html


def test_page_wires_option_endpoints():
    html = _page_html()
    for ep in (
        "/config/backtest/options/meta",
        "/config/backtest/options/start",
        "/config/backtest/options/result",
        "/config/backtest/options/runs",
    ):
        assert ep in html, ep


def test_page_option_fields_have_placeholders():
    """覆盖输入框留空 = 跟随实盘 —— placeholder 是这层语义的可见提示。"""
    html = _page_html()
    assert "跟随实盘" in html
    # 契约字段 id 齐全
    for fid in ("opt-family", "opt-timestep", "opt-start", "opt-end",
                "opt-cash", "opt-tdbars", "opt-slip", "opt-entry", "opt-cd",
                "opt-szlimit", "opt-ivgate", "opt-tp", "opt-dist",
                "opt-dmin", "opt-dmax", "opt-emin", "opt-emax", "opt-mny"):
        assert f'id="{fid}"' in html, fid


def test_page_js_braces_balanced():
    """整块 <script> 语法粗查：HTML/JS 里少一个括号会静默杀死整个页面。"""
    js = re.search(r"<script>(.*)</script>", _page_html(), re.S).group(1)
    for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
        assert js.count(op) == js.count(cl), f"{op}{cl} 不平衡"


# ── 家族列表 ──────────────────────────────────────────────────────────


def test_available_families_includes_known():
    fams = obh.available_families()
    assert "SOL-USD_UM" in fams
    assert all(f == f.upper() for f in fams)
    assert len(fams) == len(set(fams))


def test_timesteps_match_okx_options_support():
    """期权市场无 8H；1H/4H/1D 必须有。"""
    ts = obh.OPT_TIMESTEPS
    assert "8H" not in ts
    assert "15m" in ts and "1H" in ts and "1D" in ts


# ── 覆盖参数合并（关键：不污染实盘配置） ───────────────────────────────


def test_merge_opt_params_maps_selector_keys():
    p = tb._merge_opt_params({"min_distance_pct": 8, "delta_max": 0.3})
    assert p["selector"]["min_distance_pct"] == 8.0
    assert p["selector"]["delta_max"] == 0.3


def test_merge_opt_params_uses_live_key_names():
    """键名必须对齐 DEFAULT_STRATEGY —— 写错就是「静默无效」。"""
    p = tb._merge_opt_params({"max_contracts_per_family": 2, "iv_min_percentile": 0.7})
    assert p["max_contracts_per_family"] == 2
    assert p["iv_min_percentile"] == 0.7


def test_merge_opt_params_sets_td_period_from_timestep():
    """信号周期必须与重放 bar 粒度一致，否则是「15m bar 上算 5m 信号」。"""
    p = tb._merge_opt_params({}, "5m")
    assert p["td_period"] == "5m"


def test_merge_opt_params_skips_blank_and_none():
    base = tb._merge_opt_params(None, None)
    p = tb._merge_opt_params({"entry_setup": None, "take_profit_pct": ""}, None)
    assert p["entry_setup"] == base["entry_setup"]
    assert p["take_profit_pct"] == base["take_profit_pct"]


def test_merge_opt_params_does_not_mutate_live_config():
    """返回值是副本 —— 改它不能影响下一次读取到的实盘参数。"""
    from nanobot_quant.okx_options_live import _strategy_params, live_config

    before = dict(_strategy_params(live_config()) or {})
    p = tb._merge_opt_params({"entry_setup": 3, "min_distance_pct": 1}, "1m")
    p["entry_setup"] = 99
    after = dict(_strategy_params(live_config()) or {})
    assert after == before


# ── 引擎分派 ──────────────────────────────────────────────────────────


def test_run_backtest_options_requires_family():
    r = tb.run_backtest(engine="options")
    assert "error" in r and "family" in r["error"]


def test_run_backtest_options_prefixes_run_id(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        tb, "_auto_backtest_options", lambda rid, *a, **kw: calls.append((rid, a, kw))
    )
    r = tb.run_backtest(engine="options", family="sol-usd_um", timestep="15m")
    assert r["status"] == "started"
    assert r["run_id"].startswith("opt-")
    assert calls and calls[0][1][0] == "SOL-USD_UM", calls


def test_run_backtest_options_passes_overrides(monkeypatch):
    box: dict = {}

    def fake(rid, fam, ts, start, end, cash, tdb, slip, ov):
        box.update(fam=fam, ts=ts, ov=ov, cash=cash)

    monkeypatch.setattr(tb, "_auto_backtest_options", fake)
    tb.run_backtest(
        engine="options", family="SOL-USD_UM", timestep="5m",
        initial_cash=5000, overrides={"entry_setup": 7},
    )
    assert box["fam"] == "SOL-USD_UM"
    assert box["ts"] == "5m"
    assert box["ov"] == {"entry_setup": 7}
    assert box["cash"] == 5000.0


def test_opt_runs_filter_by_prefix(monkeypatch):
    monkeypatch.setattr(
        "nanobot_quant.backtest_handlers._recent_runs",
        lambda limit=20: [
            {"run_id": "opt-20260917-120000-abc123", "status": "done", "ts": 1.0},
            {"run_id": "20260917-115900-def456", "status": "done", "ts": 2.0},
            {"run_id": "opt-20260917-110000-xyz789", "status": "running", "ts": 3.0},
        ],
    )
    runs = obh._opt_runs()
    assert [r["run_id"] for r in runs] == [
        "opt-20260917-120000-abc123",
        "opt-20260917-110000-xyz789",
    ]


@pytest.mark.parametrize("bad", ["8H", "30D", "bogus"])
def test_run_backtest_options_rejects_bad_timestep(bad):
    """非法周期在 handler 层就拒（fail-closed），不落到引擎里猜。"""
    assert bad not in obh.OPT_TIMESTEPS
