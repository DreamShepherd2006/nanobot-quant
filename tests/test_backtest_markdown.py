"""回测结果 markdown 渲染测试（``nanobot_quant.backtest_markdown``）。

覆盖：两种结果形态（期权 / 现货）、不可渲染载荷（running / error / 空）、
notes 归档清单压缩、表格竖线转义，以及 ``get_backtest_result`` 的注入行为。
"""

from __future__ import annotations

import json

from nanobot_quant.backtest_markdown import render_markdown


# ── fixtures ──────────────────────────────────────────────────────────

def _opt_result() -> dict:
    return {
        "family": "SOL-USD_UM", "timestep": "15m", "bar": "15m",
        "ref_inst": "SOL-USDT",
        "start_ts": "2026-08-19T00:45:00+00:00",
        "end_ts": "2026-09-18T00:30:00+00:00",
        "initial_cash": 100.0, "slippage_pct": 0.5, "fee_rate": 0.0003,
        "td_bars": 120, "tp_pct": 50,
        "bars": {"fetched": 2999, "evaluated": 2880},
        "contracts": {"in_archive": 3824, "with_iv": 206},
        "kpi": {
            "final_net_usd": 101.3633, "roi_pct": 1.3633,
            "premium_income_usd": 1.2911, "payout_usd": 0.039,
            "open_mark_value_usd": 0.0507, "fills": 21, "wins": 9, "losses": 0,
        },
        "fills": [
            {"ts": "2026-08-23T13:15:00+00:00", "inst_id": "SOL-USD_UM-260828-88-P",
             "side": "sell_open", "sz": 1, "strike": 88.0, "avg_px": 1.4498,
             "fee_usd": 0.0026, "iv": 0.7942063020062438, "delta": -0.2620272030831562,
             "days": 5.0, "net_yield_pct": 1.5, "reason": "buy9(setup_buy=9)"},
            {"ts": "2026-09-16T16:00:00+00:00", "inst_id": "SOL-USD_UM-260916-97-P",
             "side": "settle_itm", "sz": 1, "strike": 97.0, "settle_px": 96.61,
             "payout_usd": 0.039, "premium_usd": 0.0628, "pnl_usd": 0.0428,
             "reason": "到期被行权"},
        ],
        "final_positions": [
            {"inst_id": "SOL-USD_UM-260918-94-P", "strike": 94.0, "sz": 1,
             "entry_px": 0.890335, "mark_px": 0.002683, "collateral_usd": 9.4,
             "mark_value_usd": 0.000268, "pnl_pct": 99.7},
        ],
        "skips": {"张数上限": 1851, "无 TD 衰竭信号": 975, "周期门控": 42},
        "notes": [
            "标的 K 线裁剪：3000 → 2999 根（区间外丢弃 1）",
            "归档共 31 天: 2026-08-17 ~ 2026-09-16",
            "[1/31] 2026-08-17 → alloption-trades-2026-08-17.zip (0.13MB)",
            "[2/31] 2026-08-18 → alloption-trades-2026-08-18.zip (0.12MB)",
        ],
        "elapsed_s": 517.5,
    }


def _spot_result() -> dict:
    return {
        "scene": "high", "symbols": ["CRCLX", "SOL"], "timestep": "1m",
        "bars": 2880, "fetched_bars": 2999,
        "start_ts": "2026-08-28T00:00:00+00:00",
        "end_ts": "2026-08-28T23:59:00+00:00",
        "initial_quote": 100.0, "initial_total": 1000.0,
        "final_net": 996.2, "roi": -0.0038, "batches": 10,
        "fills": 168,
        "fills_detail": [
            {"ts": "2026-08-28T01:02:00+00:00", "slot": 1, "symbol": "CRCLX",
             "side": "buy", "quantity": 0.05, "strategy_price": 71.78,
             "avg_price": 71.9091, "state": "LONG", "reason": "buy9(setup_buy=9)"},
            {"ts": "2026-08-28T03:15:00+00:00", "slot": 1, "symbol": "CRCLX",
             "side": "sell", "quantity": 0.05, "strategy_price": 72.4,
             "avg_price": 72.3276, "state": "EXIT", "reason": "setup_sell=9"},
        ],
        "skip_counts": {"batch_wait": 12, "sell_min_hold": 3},
        "capital_stats": {"turnover": 2.3, "turnover_two_side": 4.25,
                          "utilization": 0.224, "total_funds": 1000.0},
        "slots": {"open": [
            {"slot": 2, "symbol": "SOL", "qty": 0.0488, "entry_price": 81.83,
             "final_price": 79.5, "pnl_pct": -2.85},
        ]},
        "backtest_config": {
            "entry_setup": 9, "entry_countdown": 13, "exit_setup": 9,
            "exit_countdown": 13, "min_hold_bars": 10,
            "stop_loss_pct": 0.05, "take_profit_pct": 0.03,
            "slippage": 0.1, "fee_rate": 0.001,
        },
    }


# ── 基本形态 ──────────────────────────────────────────────────────────

def test_options_result_renders_core_sections():
    md = render_markdown(_opt_result())
    assert md.startswith("## 🟤 期权回测")
    assert "**📋 回测参数（实际生效）**" in md
    assert "**📊 结果**" in md
    assert "**📒 成交明细（2）**" in md
    assert "**📌 期末未平仓（1）**" in md
    assert "**🚦 SKIP 统计**" in md
    # 关键数值
    assert "| 标的家族 | SOL-USD_UM |" in md
    assert "| ROI | 1.3633% |" in md
    assert "| 成交笔数 | 21（盈利 9 / 亏损 0） |" in md
    # 方向标签与原因
    assert "🟠 卖出开仓" in md and "⚠️ 被行权" in md
    assert "buy9(setup_buy=9)" in md


def test_skip_labels_are_translated_for_both_shapes():
    """现货英文键 → 中文；期权中文前缀原样保留。"""
    spot = render_markdown(_spot_result())
    assert "| 周期门控/波锁等待 | 12 |" in spot
    assert "| 高9 min_hold 拦 | 3 |" in spot
    opt = render_markdown(_opt_result())
    assert "| 周期门控 | 42 |" in opt
    assert "| 张数上限 | 1851 |" in opt


def test_spot_result_renders_core_sections():
    md = render_markdown(_spot_result())
    assert md.startswith("## 📈 现货回测")
    assert "| 场景 | high |" in md
    assert "| 标的 | CRCLX, SOL |" in md
    assert "| ROI | -0.3800% |" in md          # 小数比例 → 百分比
    assert "| 单边周转率 | 2.30 |" in md
    assert "| 平均资金利用率 | 22.40% |" in md
    assert "🟢 买入" in md and "🔴 卖出" in md
    assert "setup_sell=9" in md
    assert "| 最短持有期 | 10 |" in md


# ── 不可渲染载荷 ──────────────────────────────────────────────────────

def test_running_and_error_payloads_render_empty():
    assert render_markdown({"status": "running", "run_id": "x"}) == ""
    assert render_markdown({"error": "no backtest result"}) == ""
    assert render_markdown({"error": "boom", "kpi": {}}) == ""
    assert render_markdown({}) == ""
    assert render_markdown(None) == ""
    assert render_markdown("not a dict") == ""


# ── notes 压缩 ────────────────────────────────────────────────────────

def test_archive_notes_are_compressed_to_one_line():
    md = render_markdown(_opt_result())
    assert "归档 2 天：2026-08-17 ~ 2026-08-18（0.2 MB）" in md
    # 逐日下载清单必须被折叠掉，否则粘贴结果一半是日志
    assert "[1/31]" not in md and "alloption-trades" not in md
    # 非归档备注原样保留
    assert "标的 K 线裁剪：3000 → 2999 根（区间外丢弃 1）" in md


def test_notes_absent_is_not_an_error():
    res = _opt_result()
    res["notes"] = []
    md = render_markdown(res)
    assert "**📝 运行备注**" not in md
    assert "## 🟤 期权回测" in md


# ── 表格健壮性 ────────────────────────────────────────────────────────

def test_pipe_and_newline_in_cells_are_escaped():
    res = _opt_result()
    res["fills"][0]["reason"] = "a|b\nc"
    md = render_markdown(res)
    row = [ln for ln in md.splitlines() if "a" in ln and "c" in ln]
    assert row, "含竖线/换行的单元格应仍在一行内"
    assert "a\\|b c" in row[0]


def test_missing_fields_degrade_to_dash():
    res = {"family": "SOL-USD_UM", "kpi": {}, "fills": [
        {"inst_id": "X", "side": "sell_open", "sz": 1}]}
    md = render_markdown(res)
    assert "| — |" in md or "| X |" in md   # 不抛异常即可，缺字段显示 —
    assert "## 🟤 期权回测" in md


def test_time_is_rendered_in_shanghai_timezone():
    md = render_markdown(_opt_result())
    # 2026-08-23T13:15Z → 北京 21:15
    assert "2026/8/23 21:15:00" in md


def test_iv_shown_as_percent_and_strike_without_trailing_zero():
    """IV 内部是小数（0.794）、展示要 ×100；strike 88.0 不该带 .0 尾巴。"""
    md = render_markdown(_opt_result())
    assert "| 79.4% |" in md          # IV 0.7942… → 79.4%
    assert "| 88 |" in md            # strike 88.0 → 88
    assert "| 88.0 |" not in md
    assert "| 0.8% |" not in md      # 回归：曾经把 0.794 当成 0.8%


def test_missing_numbers_render_dash_not_dash_percent():
    """缺值应是 ``—``，不能出现 ``—%`` 这种拼接残留。"""
    res = _opt_result()
    res["tp_pct"] = None
    res["fills"][1]["iv"] = None
    md = render_markdown(res)
    assert "| 止盈线 | — |" in md
    assert "—%" not in md


def test_spread_model_row_present_only_for_options():
    """「价差模型」是期权回测专有行（现货链没有这个模型）——页面有、markdown 也必须有，
    否则「复制 Markdown = 页面所见」这条一致性要求会被静默破坏。"""
    res = _opt_result()
    res["spread_model"] = "family_dsigma+tick"
    res["spread_model_note"] = "家族 Δσ + tick 地板（SOL-USD_UM：Δσ=12.2 IV 点，tick=0.01）；额外滑点 0.00%"
    md = render_markdown(res)
    assert "| 价差模型 |" in md
    assert "Δσ=12.2 IV 点" in md
    assert "| 价差模型 |" not in render_markdown(_spot_result())


def test_premium_income_split_settled_and_open():
    """权利金拆两栏：已了结（premium_usd 累计）+ 未平仓（在 cash 里但不在 fills 里）。

    原先只给一栏「权利金收入」= 已了结，而净值含未平仓那张 ⇒ 两栏不闭合，
    看上去像算错（2026-09-25 实测：0.358 vs 净值 100.4126）。
    """
    res = _opt_result()
    res["kpi"]["open_premium_usd"] = 0.089
    md = render_markdown(res)
    assert "| 权利金收入（已了结） | $1.2911 |" in md
    assert "| 未平仓权利金 | $0.0890 |" in md

    # 旧记录没有该字段 → 必须是「—」而不是 None/NaN 拼接残留
    res["kpi"].pop("open_premium_usd")
    md_old = render_markdown(res)
    assert "| 未平仓权利金 | $— |" in md_old
    assert "None" not in md_old


def test_archive_summary_line_is_not_duplicated():
    """原始 notes 自带的「归档共 N 天」应与我们生成的摘要合并成一条。"""
    md = render_markdown(_opt_result())
    assert md.count("归档") == 1
    assert "归档 2 天：2026-08-17 ~ 2026-08-18（0.2 MB）" in md


def test_spot_price_shows_level_and_per_fill_price():
    """只写标的代码看不出价格水平 —— 区间与每笔现货价都要给。"""
    res = _opt_result()
    res["spot_range"] = {"first": 100.79, "last": 106.53,
                         "high": 107.2, "low": 98.4}
    res["fills"][0]["spot"] = 100.79
    md = render_markdown(res)
    assert "SOL-USDT（$100.79 → $106.53）" in md
    assert "最高 $107.2 / 最低 $98.4" in md
    assert "| 现货价 |" in md
    assert "| 100.79 |" in md

def test_spot_fields_degrade_when_absent():
    """缺现货数据时列仍在、值为 —（不报错、不整段消失）。"""
    md = render_markdown(_opt_result())
    assert "| 现货区间 | — |" in md
    assert "现货价" in md
    assert "| SOL-USDT（" not in md     # 无 spot_range 时不拼价格


# ── get_backtest_result 注入 ──────────────────────────────────────────

def test_get_backtest_result_attaches_markdown_to_inner_result(tmp_path, monkeypatch):
    """后台任务的落盘形态是 {status, run_id, result}，markdown 必须挂在内层。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.tools import tools_backtest

    monkeypatch.setattr(onchainos_cli, "backtests_dir", lambda: tmp_path)
    payload = {"status": "done", "run_id": "opt-x", "result": _opt_result()}
    (tmp_path / "opt-x.json").write_text(json.dumps(payload), encoding="utf-8")

    out = tools_backtest.get_backtest_result("opt-x")
    assert out["status"] == "done"
    # 前端把 out["result"] 直接喂给渲染器 —— markdown 就在这一层
    assert out["result"]["markdown"].startswith("## 🟤 期权回测")
    assert out["result"]["kpi"]["roi_pct"] == 1.3633   # 原字段不被改写
    assert "markdown" not in out                       # 外层不加，避免冗余


def test_get_backtest_result_handles_bare_result(tmp_path, monkeypatch):
    """裸 result 形态（旧记录 / 其他调用方）也要能渲染。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.tools import tools_backtest

    monkeypatch.setattr(onchainos_cli, "backtests_dir", lambda: tmp_path)
    (tmp_path / "bare.json").write_text(json.dumps(_spot_result()), encoding="utf-8")

    out = tools_backtest.get_backtest_result("bare")
    assert out["run_id"] == "bare"
    assert out["markdown"].startswith("## 📈 现货回测")


def test_get_backtest_result_skips_markdown_when_running(tmp_path, monkeypatch):
    from nanobot_quant import onchainos_cli
    from nanobot_quant.tools import tools_backtest

    monkeypatch.setattr(onchainos_cli, "backtests_dir", lambda: tmp_path)
    (tmp_path / "run1.json").write_text(
        json.dumps({"status": "running", "run_id": "run1",
                    "progress": {"pct": 10.0}}), encoding="utf-8")

    out = tools_backtest.get_backtest_result("run1")
    assert "markdown" not in out


def test_get_backtest_result_survives_render_failure(tmp_path, monkeypatch):
    """markdown 只是 UX 增强 —— 渲染炸了也不能影响结果返回。"""
    from nanobot_quant import onchainos_cli
    from nanobot_quant.tools import tools_backtest
    import nanobot_quant.backtest_markdown as bm

    monkeypatch.setattr(onchainos_cli, "backtests_dir", lambda: tmp_path)
    monkeypatch.setattr(bm, "render_markdown",
                        lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
    (tmp_path / "opt-y.json").write_text(
        json.dumps({"status": "done", "run_id": "opt-y", "result": _opt_result()}),
        encoding="utf-8")

    out = tools_backtest.get_backtest_result("opt-y")
    assert "markdown" not in out["result"]
    assert out["result"]["kpi"]["fills"] == 21         # 结果本身完好


# ── 生效张数上限（回测以页面参数为准）─────────────────────────────

def test_max_contracts_shows_effective_and_both_inputs():
    """「页面填 10 却只开 3」这类闷棍 —— 生效值与两个原始值都要写出来。"""
    res = _opt_result()
    res["max_contracts"] = {"effective": 3, "per_family": 10, "total": 3}
    md = render_markdown(res)
    assert "张数上限" in md
    assert "3 张（单家族 10 / 全局 3，取小值）" in md


def test_max_contracts_degrades_when_absent():
    """旧记录没有该字段 —— 降级为 —，不能报错。"""
    res = _opt_result()
    res.pop("max_contracts", None)
    md = render_markdown(res)
    assert "张数上限" in md
    assert "张数上限 | —" in md


def test_max_contracts_tolerates_partial_values():
    """只有生效值、原始值缺一个时仍能渲染。"""
    res = _opt_result()
    res["max_contracts"] = {"effective": 5, "per_family": None, "total": 3}
    md = render_markdown(res)
    assert "5 张（单家族 — / 全局 3，取小值）" in md
