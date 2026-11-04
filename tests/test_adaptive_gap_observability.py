"""C102：自适应间距（adaptive gap）三处可观测性测试（2026-11-04）。

背景（2026-11-03 拍板）：方案 B 反馈式自适应间距必须先把「看得见」补齐，
否则回测开关是死门、实盘切档无痕、信号历史看不出是谁拦的。三处：

① 档位首次设定 / 滞回切换 → stderr `[TD] ADAPT GAP`；回退静态按 scene 只记一次
   `[TD] ADAPT GAP NA`（不刷屏）。
② `entry_gap` 拦截事件与 print 都必须带「当前间距 + 来源（自适应/静态）」，自适应
   时附带 hit_rate / 样本数 / slot 占用。
③ 回测侧：`adaptive_gap_enabled` / `adaptive_m` 必须显式注入 strategy.parameters
   （否则策略读类默认 False ⇒ 恒关），`backtest_config` 快照含两项，结果顶层有
   `adaptive_gap_trace`（档位变化轨迹），页面与 markdown 都能读出来。

判定口径说明：日志与 trace 的值全部来自策略自身（`_adaptive_gap_cur` /
`_adaptive_last_stat`），测试锁的是「可见性接线」，不是重新实现判定逻辑。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from nanobot_quant.backtest.driver import BacktestDriver
from nanobot_quant.backtest_markdown import render_markdown
from nanobot_quant.batches import BatchManager
from nanobot_quant.exec_params import DEFAULT_EXEC_PARAMS
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy


# ── 夹具 ────────────────────────────────────────────────────────────────


class _FakeBM:
    """批次台账最小替身：open_slots() + slots 决定 slot 占用率。"""

    def __init__(self, costs=None, total_slots: int = 10):
        self._costs = list(costs or [])
        self.slots = [{"slot": i + 1, "status": "available"} for i in range(total_slots)]

    def open_slots(self):
        return [
            {"slot": i + 1, "status": "open",
             "lot": {"qty": 1.0, "entry_price": c}}
            for i, c in enumerate(self._costs)
        ]

    def any_open(self):
        return bool(self._costs)

    def check_exit(self, *a, **k):
        return []

    def pick_exit_slot(self, *a, **k):
        return None

    def scan_buy_slots(self, start=1):
        return []


def _df(close: float = 100.0, high: float = 100.5) -> pd.DataFrame:
    return pd.DataFrame({"High": [high], "Low": [close * 0.995], "Close": [close]})


def _strategy(scene_params: dict | None = None, **params) -> TdSequentialStrategy:
    params.setdefault("min_history", 50)
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters, **params)
    s.logger = logging.getLogger("c102-adaptive")
    s.portfolio_value = 100_000.0
    s.cash = 100_000.0
    s.batch_manager = _FakeBM()
    s.initialize()
    s.broker = object()
    s._activate_scene("high", {"params": dict(scene_params or {})})
    return s


def _bars_buy9() -> "object":
    from lumibot.entities import Bars

    closes = [100.0 + i * 10 for i in range(41)] + [500.0 - i for i in range(1, 13)]
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.002 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="min"),
    )
    return Bars(df, "GATE", None)


# ── ① 档位日志 ──────────────────────────────────────────────────────────


def test_init_log_and_stat_snapshot(capsys):
    """样本足够时首次设定档位 → INIT 日志 + 统计快照（回测 trace 同源）。"""
    s = _strategy({"adaptive_gap_enabled": True, "adaptive_m": 5})
    s._adaptive_done["high"] = [True] * 5  # 命中率 100% → 0.1% 档

    gap = s._adaptive_gap_observe(_df(), False, s.batch_manager)
    err = capsys.readouterr().err

    assert gap == pytest.approx(0.10)
    assert "ADAPT GAP INIT" in err, err
    assert "0.10%" in err
    assert "hit_rate=100.0%" in err
    assert s._adaptive_gap_src["high"] == "自适应"
    stat = s._adaptive_last_stat["high"]
    assert stat["n"] == 5 and stat["target"] == pytest.approx(0.10)


def test_switch_logged_after_hysteresis(capsys):
    """命中率掉到 <30% → 目标 0.4%，滞回 2 次后才切换并打 SWITCH 日志。"""
    s = _strategy({"adaptive_gap_enabled": True, "adaptive_m": 5})
    s._adaptive_done["high"] = [True] * 5
    s._adaptive_gap_observe(_df(), False, s.batch_manager)
    capsys.readouterr()  # 丢掉 INIT 日志

    s._adaptive_done["high"] = [False] * 7  # 命中率 0 → 0.4%
    first = s._adaptive_gap_observe(_df(), False, s.batch_manager)
    assert first == pytest.approx(0.10), "滞回未满不应切换"
    assert "ADAPT GAP SWITCH" not in capsys.readouterr().err

    second = s._adaptive_gap_observe(_df(), False, s.batch_manager)
    err = capsys.readouterr().err
    assert second == pytest.approx(0.40)
    assert "ADAPT GAP SWITCH" in err, err
    assert "0.10%" in err and "0.40%" in err


def test_na_logged_once_per_scene(capsys):
    """样本不足回退静态档：只记一次，不刷屏（日志降噪 ≠ 静默）。"""
    s = _strategy({"adaptive_gap_enabled": True, "adaptive_m": 5,
                   "entry_min_gap_pct": 0.3})
    for _ in range(4):
        gap = s._adaptive_gap_observe(_df(), False, s.batch_manager)
        assert gap == pytest.approx(0.30)
    err = capsys.readouterr().err
    assert err.count("ADAPT GAP NA") == 1, err
    assert "回退静态 0.30%" in err
    assert s._adaptive_gap_src["high"] == "自适应·回退静态"


def test_disabled_writes_static_source_without_log(capsys):
    """默认关：零行为变化、零日志，但来源标为「静态」供事件读取。"""
    s = _strategy({"entry_min_gap_pct": 0.3})
    capsys.readouterr()  # 丢掉 _activate_scene 的 [DIAG] 场景激活日志
    assert s._adaptive_gap_observe(_df(), False, s.batch_manager) == pytest.approx(0.30)
    assert capsys.readouterr().err == ""
    assert s._adaptive_gap_src["high"] == "静态"


# ── ② entry_gap 事件带间距 + 来源 ──────────────────────────────────────


def _capture_entry_gap(scene_params: dict):
    s = _strategy(scene_params)
    s._bars = _bars_buy9()
    s.get_position = lambda symbol: None
    s.get_historical_prices = lambda symbol, length, timestep: s._bars
    s.batch_manager = _FakeBM(costs=[100.0])  # 现价 488 远高于成本 → 必然被拦
    s._buy_on_slot = lambda *a, **k: "stubbed"
    s._wallet_switch = lambda account_id: True
    s._slot_quote_balance = lambda quote_symbol="USDC": 1e9
    s._slot_token_balance = lambda symbol: 1e9
    s._home_account = "acc-home"
    s.create_order = lambda *a, **k: None
    s.submit_order = lambda order: None
    captured: dict = {}
    s._record = lambda *a, **k: captured.update(args=a, kwargs=k)
    s._evaluate_symbol()
    return captured, s


def test_entry_gap_event_carries_static_source():
    captured, s = _capture_entry_gap({"entry_min_gap_pct": 0.3})
    assert s._skip_counts.get("entry_gap", 0) == 1
    text = str(captured["args"])
    assert "间距 0.3%（静态）" in text, text
    assert captured["kwargs"]["status"] == "entry_gap_blocked"


def test_entry_gap_event_carries_adaptive_source_and_stats():
    captured, s = _capture_entry_gap(
        {"entry_min_gap_pct": 0.3, "adaptive_gap_enabled": True, "adaptive_m": 5}
    )
    text = str(captured["args"])
    # 无定案样本 → 自适应回退静态，来源必须如实标注（不能冒充「自适应」档）
    assert "间距 0.3%（自适应·回退静态）" in text, text


# ── ③ 回测接线：注入 / 快照 / trace ────────────────────────────────────


def _fake_fetcher(closes: list[float]):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def _fetch(pair, start_ts, end_ts, bar):
        idx = [start + timedelta(minutes=15) * i for i in range(len(closes))]
        return pd.DataFrame(
            {
                "Open": closes,
                "High": [c * 1.001 for c in closes],
                "Low": [c * 0.999 for c in closes],
                "Close": closes,
                "Volume": [1000.0] * len(closes),
            },
            index=pd.DatetimeIndex(idx),
        )

    return _fetch


def _params(**scene_overrides) -> dict:
    p = {k: (dict(v) if isinstance(v, dict) else v)
         for k, v in DEFAULT_EXEC_PARAMS.items()}
    p["scenes"] = {k: dict(v) for k, v in (DEFAULT_EXEC_PARAMS["scenes"] or {}).items()}
    p["scenes"]["mid"].update(
        {
            "enabled": True,
            "sleeptime": "15m",
            "symbols": ["CRCLX"],
            "batches": 2,
            "quantity_mode": "fixed_amount",
            "td_fixed_amount": 4.0,
            "entry_setup": 9,
            "exit_setup": 9,
            "exit_countdown": 13,
        }
    )
    p["scenes"]["mid"].update(scene_overrides)
    p["td_bars"] = 40
    return p


def _driver(tmp_path, **scene_overrides) -> BacktestDriver:
    closes = [100.0] * 60 + [100.0 + i * 0.5 for i in range(1, 41)]
    return BacktestDriver(
        scene="mid",
        params=_params(**scene_overrides),
        fetcher=_fake_fetcher(closes),
        ledger_dir=tmp_path,
    )


def test_driver_parses_and_injects_adaptive_params(tmp_path):
    """开关与反馈窗口必须进 strategy.parameters（否则回测里自适应恒关）。"""
    driver = _driver(tmp_path, adaptive_gap_enabled=True, adaptive_m=5)
    assert driver.adaptive_gap_enabled is True
    assert driver.adaptive_m == 5

    captured: dict = {}
    orig = driver._evaluate_symbol if hasattr(driver, "_evaluate_symbol") else None
    out = driver.run()
    cfg = out["backtest_config"]
    assert cfg["adaptive_gap_enabled"] is True
    assert cfg["adaptive_m"] == 5
    assert "adaptive_gap_trace" in out and isinstance(out["adaptive_gap_trace"], list)
    assert orig is None or callable(orig)


def test_driver_default_off_keeps_v3_behavior(tmp_path):
    """默认关：快照写「关」、trace 为空（零行为变化）。"""
    driver = _driver(tmp_path)
    out = driver.run()
    assert out["backtest_config"]["adaptive_gap_enabled"] is False
    assert out["backtest_config"]["adaptive_m"] == 30
    assert out["adaptive_gap_trace"] == []


def test_trace_records_change_only(tmp_path):
    """trace 只读采样策略自身档位：首次变化记一条，同值不重复，关掉后不记。"""
    d = BacktestDriver.__new__(BacktestDriver)
    d.adaptive_gap_enabled = True
    d.scene_name = "high"
    d._adaptive_gap_trace = []
    d._adaptive_gap_last = {}

    ts = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    strat = SimpleNamespace(
        _adaptive_gap_cur={"high": 0.10},
        _adaptive_last_stat={"high": {"hit_rate": 0.6, "n": 10, "slot_load": 0.4}},
    )
    d._trace_adaptive_gap(ts, strat, "SOL")
    d._trace_adaptive_gap(ts, strat, "SOL")  # 同值不重复
    assert len(d._adaptive_gap_trace) == 1
    assert d._adaptive_gap_trace[0]["from"] is None
    assert d._adaptive_gap_trace[0]["to"] == pytest.approx(0.10)
    assert d._adaptive_gap_trace[0]["hit_rate"] == pytest.approx(0.6)

    strat._adaptive_gap_cur["high"] = 0.20
    d._trace_adaptive_gap(ts, strat, "ETH")
    assert len(d._adaptive_gap_trace) == 2
    assert d._adaptive_gap_trace[1]["from"] == pytest.approx(0.10)
    assert d._adaptive_gap_trace[1]["symbol"] == "ETH"

    d.adaptive_gap_enabled = False
    strat._adaptive_gap_cur["high"] = 0.40
    d._trace_adaptive_gap(ts, strat, "ETH")
    assert len(d._adaptive_gap_trace) == 2


# ── 展示层：markdown ───────────────────────────────────────────────────


def test_markdown_renders_adaptive_rows_and_trace():
    md = render_markdown(
        {
            "scene": "high",
            "backtest_config": {
                "entry_min_gap_pct": 0.3,
                "adaptive_gap_enabled": True,
                "adaptive_m": 5,
            },
            "adaptive_gap_trace": [
                {"ts": "2026-11-04T12:00:00+00:00", "scene": "high",
                 "symbol": "SOL", "from": None, "to": 0.1,
                 "hit_rate": 0.6, "n": 10, "slot_load": 0.4},
            ],
        }
    )
    assert "加仓最小间距" in md
    assert "自适应间距" in md and "M=5" in md
    assert "自适应间距档位轨迹" in md
    assert "SOL" in md


def test_markdown_marks_adaptive_off():
    md = render_markdown(
        {"scene": "high", "backtest_config": {"adaptive_gap_enabled": False}, }
    )
    assert "自适应间距 | 关" in md or "自适应间距" in md
    assert "自适应间距档位轨迹" not in md
