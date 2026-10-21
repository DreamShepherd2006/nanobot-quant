"""多周期回测（2026-10-21）的契约测试。

拍板口径（写死在这里，防回归）：
- 各档**独立回放**：同一区间、同一份参数（bar 计数语义）、各自独立资金/台账；
- `periods` 有 1 档 = 单周期覆盖，≥2 档 = 多周期，结果含 `compare` + `period_results`；
- 单档失败（数据不足）记 `insufficient` 并继续，不拖垮其它档；全失败才抛错；
- **两臂对照在多周期下不跑**（否则 2N 次回放），结果里显式注明；
- 跨周期 ROI 不得求平均/加总，报告里必须有这句。
"""
from __future__ import annotations

from nanobot_quant.tools.tools_backtest import _period_summary, _run_multi_period


class _FakeDriver:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def run(self):
        if self._error:
            raise RuntimeError(self._error)
        return self._payload


def _res(period: str, roi: float = 0.01, fills: int = 4):
    return {
        "scene": "high",
        "symbols": ["SOL"],
        "source": "gate_cex",
        "source_label": "Gate CEX（与执行同所）",
        "timestep": period,
        "bars": 100,
        "fetched_bars": 219,
        "initial_quote": 100.0,
        "initial_total": 400.0,
        "batches": 4,
        "roi": roi,
        "final_net": 400.0 * (1 + roi),
        "fills": fills,
        "skip_counts": {"a": 2, "b": 3},
        "ema_gate": {"pass": 7, "block": 11, "na": 0},
        "capital_stats": {"turnover": 0.5, "turnover_two_side": 1.0, "utilization": 0.2},
        "bars_rows": [{"ts": "2026-10-17T00:00:00", "symbol": "SOL"}],
    }


def _build_factory(per_period):
    calls = []

    def build(ov, progress_path, extra):
        calls.append((ov.get("sleeptime"), dict(extra)))
        item = per_period[ov.get("sleeptime")]
        if isinstance(item, Exception):
            return _FakeDriver(error=str(item))
        return _FakeDriver(payload=item)

    build.calls = calls
    return build


def test_multi_period_runs_each_period_independently():
    build = _build_factory({"1m": _res("1m"), "3m": _res("3m", roi=0.02), "5m": _res("5m")})
    out = _run_multi_period(["1m", "3m", "5m"], {"ema_gate_enabled": True}, build, None)

    assert out["multi_period"] is True
    assert out["periods"] == ["1m", "3m", "5m"]
    # 每档都用同一份参数、只替换 sleeptime（同参数 = 同根数）
    assert [c[0] for c in build.calls] == ["1m", "3m", "5m"]
    assert build.calls[1][1] == {"period": "3m", "period_index": 2, "period_total": 3}
    # 各档完整结果独立保存（互不覆盖）
    assert out["period_results"]["3m"]["roi"] == 0.02
    assert len(out["compare"]) == 3
    # 两臂在多周期下不跑，且显式注明
    assert "skipped" in out["arms"]


def test_insufficient_period_does_not_kill_others():
    build = _build_factory(
        {"1m": RuntimeError("数据不足（1m 深度 6.9 天）"), "3m": _res("3m"), "5m": _res("5m")}
    )
    out = _run_multi_period(["1m", "3m", "5m"], {}, build, None)

    assert out["period_results"]["1m"]["error"].startswith("数据不足")
    bad = out["compare"][0]
    assert bad == {
        "period": "1m",
        "status": "insufficient",
        "error": "数据不足（1m 深度 6.9 天）",
    }
    # 其余两档照常出结果，顶层字段取第一个成功的档
    assert [c["status"] for c in out["compare"][1:]] == ["ok", "ok"]
    assert out["timestep"] == "3m"


def test_all_periods_failing_raises():
    build = _build_factory({"1m": RuntimeError("x"), "3m": RuntimeError("y")})
    try:
        _run_multi_period(["1m", "3m"], {}, build, None)
    except RuntimeError as exc:
        assert "所有周期均失败" in str(exc)
        assert "1m: x" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当抛错")


def test_period_summary_uses_arm_summary_shape():
    s = _period_summary("1m", _res("1m", roi=0.0123, fills=6))
    assert s["period"] == "1m" and s["status"] == "ok"
    assert round(s["roi_pct"], 4) == 1.23
    assert s["fills"] == 6
    assert s["ema_pass"] == 7 and s["ema_block"] == 11 and s["ema_na"] == 0
    assert s["skip_total"] == 5
    assert s["turnover_two_side"] == 1.0
    assert s["fetched_bars"] == 219 and s["bars"] == 100
    # TD / EMA 分段（2026-10-21）：候选 = 通过 + 拦，通过率单独给
    assert s["candidates"] == 18
    assert round(s["ema_pass_rate"], 2) == round(7 / 18 * 100, 2)


def test_markdown_has_compare_table_and_period_sections():
    from nanobot_quant.backtest_markdown import render_markdown

    build = _build_factory({"1m": _res("1m"), "5m": _res("5m")})
    out = _run_multi_period(["1m", "5m"], {}, build, None)
    out["run_id"] = "bt-test-1"
    md = render_markdown(out)

    assert "多周期回测 bt-test-1" in md
    # 对比表已按 TD / EMA 分段（2026-10-21）：不再把两套指标合成一格
    for col in ("TD 候选", "成交", "EMA 通过", "EMA 拦", "通过率", "SKIP"):
        assert col in md, col
    assert "不得求平均或加总" in md          # 口径警告必须在
    assert "同根数，非同时间" in md
    assert md.count("### 周期 ") == 2       # 每档一节
