"""C125 回测指标汇总（backtest/metrics.py）单测。"""

from __future__ import annotations

import pytest

from nanobot_quant.backtest.metrics import compute_metrics, normalize_exit_reason


def _fill(side, qty, px, ts, symbol="SOL", reason=""):
    return {
        "ts": ts,
        "symbol": symbol,
        "side": side,
        "quantity": qty,
        "avg_price": px,
        "reason": reason,
    }


def test_flat_position_metrics():
    """全平场景：最终权益 = 期末现金，持仓相关全为 0。"""
    fills = [
        _fill("buy", 10, 10.0, "2026-11-01T00:00:00+00:00"),
        _fill("sell", 10, 12.0, "2026-11-01T02:00:00+00:00", reason="setup_sell=9"),
    ]
    cash_end = 1000 - 100 + 10 * 12 * (1 - 0.001)
    met = compute_metrics(
        fills_detail=fills,
        slots_summary={"SOL": {"open": [], "final_price": 12.0}},
        initial_total=1000.0,
        final_net=cash_end,
        cash_end=cash_end,
        positions_value_end=0.0,
        net_values=[{"ts": "2026-11-01T00:00:00+00:00", "net": 1000.0},
                    {"ts": "2026-11-01T02:00:00+00:00", "net": cash_end}],
        skip_counts={"cycle_wait": 3},
        fee_rate=0.001,
    )
    assert met["entries"] == 1
    assert met["exits"] == 1
    assert met["open_lots"] == 0
    assert met["positions_cost"] == 0.0
    assert met["positions_market_value"] == 0.0
    assert met["cash_end"] == pytest.approx(cash_end)
    assert met["final_equity"] == pytest.approx(cash_end)
    assert met["realized_pnl"] == pytest.approx(cash_end - 1000.0)
    assert met["unrealized_pnl"] == pytest.approx(0.0)
    assert met["roi_pct"] == pytest.approx((cash_end / 1000.0 - 1) * 100)
    assert met["missed"] == 3
    assert met["missed_rate_pct"] == pytest.approx(3 / 4 * 100)
    assert met["consistency"]["ok"] is True
    # 出场结构按卖出笔统计，与 exits 对得上（不能用配对段数冒充）
    assert sum(v["count"] for v in met["exit_reason"].values()) == met["exits"] == 1


def test_open_position_split():
    """期末有仓：现金 + 持仓市值 = 净值；实现/未实现分开。"""
    fills = [_fill("buy", 10, 10.0, "2026-11-01T00:00:00+00:00")]
    cash_end = 900.0
    mv = 110.0  # 10 × 11
    met = compute_metrics(
        fills_detail=fills,
        slots_summary={"SOL": {"open": [{"slot": 1, "qty": 10, "entry_price": 10.0}],
                               "final_price": 11.0}},
        initial_total=1000.0,
        final_net=cash_end + mv,
        cash_end=cash_end,
        positions_value_end=mv,
        net_values=[{"ts": "2026-11-01T00:00:00+00:00", "net": 1010.0}],
        skip_counts={},
        fee_rate=0.001,
    )
    assert met["open_lots"] == 1
    assert met["positions_cost"] == pytest.approx(100.0)
    assert met["positions_market_value"] == pytest.approx(110.0)
    assert met["unrealized_pnl"] == pytest.approx(10.0)
    assert met["realized_pnl"] == pytest.approx(0.0)
    assert met["open_qty_by_symbol"]["SOL"] == pytest.approx(10.0)
    assert met["consistency"]["ok"] is True


def test_max_drawdown_from_net_values():
    net_values = [
        {"ts": "t1", "net": 100.0},
        {"ts": "t2", "net": 120.0},
        {"ts": "t3", "net": 90.0},
        {"ts": "t4", "net": 110.0},
    ]
    met = compute_metrics(
        fills_detail=[],
        slots_summary={},
        initial_total=100.0,
        final_net=110.0,
        cash_end=110.0,
        positions_value_end=0.0,
        net_values=net_values,
        fee_rate=0.001,
    )
    assert met["max_drawdown_usd"] == pytest.approx(30.0)
    assert met["max_drawdown_pct"] == pytest.approx(25.0)
    assert met["max_drawdown_peak_ts"] == "t2"
    assert met["max_drawdown_trough_ts"] == "t3"
    assert met["calmar"] == pytest.approx(10.0 / 25.0)


def test_exit_reason_normalization():
    assert normalize_exit_reason("setup_sell=9") == "high9"
    assert normalize_exit_reason("cd_sell=13") == "cd13"
    assert normalize_exit_reason("take_profit: pnl_net=2.1%") == "take_profit"
    assert normalize_exit_reason("stop_loss: pnl_net=-5.0%") == "stop_loss"
    assert normalize_exit_reason("eod_close:2026-11-04") == "eod"
    assert normalize_exit_reason("") == "unknown"


def test_fifo_pairing_and_hold_hours():
    """两批买入被一笔卖出吃掉：FIFO 顺序配对、持有小时按各自入场时间。"""
    fills = [
        _fill("buy", 5, 10.0, "2026-11-01T00:00:00+00:00"),
        _fill("buy", 5, 20.0, "2026-11-01T01:00:00+00:00"),
        _fill("sell", 10, 25.0, "2026-11-01T03:00:00+00:00", reason="setup_sell=9"),
    ]
    met = compute_metrics(
        fills_detail=fills,
        slots_summary={"SOL": {"open": [], "final_price": 25.0}},
        initial_total=1000.0,
        final_net=1000.0,
        cash_end=1000.0,
        positions_value_end=0.0,
        net_values=[{"ts": "2026-11-01T00:00:00+00:00", "net": 1000.0}],
        fee_rate=0.001,
    )
    reason = met["exit_reason"]["high9"]
    # 一笔卖出跨两个买入批：count = 卖出笔数(1)、pairs = 配对段数(2)
    assert reason["count"] == 1
    assert reason["pairs"] == 2
    # 5×(25×0.999−10)=74.875，5×(25×0.999−20)=24.875
    assert reason["pnl"] == pytest.approx(99.75)
    # 持有时间按数量加权：(5×3h + 5×2h)/10 = 2.5h
    assert met["hold_hours_max"] == pytest.approx(2.5)
    assert met["hold_hours_mean"] == pytest.approx(2.5)
    assert sum(v["count"] for v in met["exit_reason"].values()) == met["exits"]
    assert met["win_rate_pct"] == pytest.approx(100.0)
    assert met["profit_factor"] is None


def test_consistency_flags_mismatch():
    """现金+市值 与 净值 不一致时必须显式标记（不静默）。"""
    met = compute_metrics(
        fills_detail=[],
        slots_summary={},
        initial_total=1000.0,
        final_net=1100.0,
        cash_end=900.0,
        positions_value_end=0.0,
        net_values=[{"ts": "t", "net": 1100.0}],
        fee_rate=0.001,
    )
    assert met["consistency"]["ok"] is False
    assert met["consistency"]["diff"] == pytest.approx(-200.0)


def test_page_renders_metrics_box():
    """页面必须渲染指标汇总块，且与 markdown 同来源（res.metrics）。"""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "src/nanobot_quant/backtest_page.html").read_text(encoding="utf-8")
    assert "function metricsBox" in html
    assert "metricsBox(res.metrics)" in html
    for label in (
        "最终权益（市值）",
        "期末现金",
        "期末持仓占用资金",
        "期末持仓手数",
        "总开仓数",
        "错过次数 / 错过率",
        "最大回撤",
        "手续费合计",
    ):
        assert label in html, label


def test_empty_inputs_do_not_raise():
    met = compute_metrics(
        fills_detail=[],
        slots_summary={},
        initial_total=0.0,
        final_net=0.0,
        cash_end=0.0,
        positions_value_end=0.0,
        net_values=[],
        skip_counts=None,
        capital_stats=None,
        fee_rate=0.0,
    )
    assert met["roi_pct"] == 0.0
    assert met["entries"] == 0
    assert met["missed_rate_pct"] == 0.0
    assert met["exit_reason"] == {}
