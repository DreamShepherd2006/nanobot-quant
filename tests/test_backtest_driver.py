"""方案 B Step 3：回测驱动（BacktestDriver）测试。

验证点（docs/quant-system.md §25.6）：回测驱动复用实盘策略决策代码——
本测试覆盖：驱动结构完整性、TD 信号触发成交（下跌序列 setup 9 → BUY）、
回测 hooks 注入生效（子账号 broker / 取价零网络）、台账隔离（不回写实盘
批次文件）、场景缺失 fail-closed。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from nanobot_quant.backtest.driver import BacktestDriver
from nanobot_quant.batches import BatchManager
from nanobot_quant.exec_params import DEFAULT_EXEC_PARAMS
from nanobot_quant.strategies.td_sequential_strategy import TdSequentialStrategy

# 测试用环境隔离（conftest autouse fixture 已隔离 exec_params/batches 路径）


class _FakeFetcher:
    """fetcher(pair, start_ts, end_ts, bar) → DataFrame（大写列 + UTC index）。"""

    def __init__(self, closes: list[float], bar: str = "15m", start=None):
        self.closes = closes
        self.bar = bar
        self.start = start or datetime(2026, 8, 1, tzinfo=timezone.utc)

    def __call__(self, pair: str, start_ts, end_ts, bar: str) -> pd.DataFrame:
        interval = {
            "1m": timedelta(minutes=1), "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15), "1H": timedelta(hours=1),
            "1D": timedelta(days=1),
        }[bar]
        idx = [self.start + interval * i for i in range(len(self.closes))]
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


def _params(**overrides) -> dict:
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
    p["td_bars"] = 40  # 测试缩小 TD 窗口（默认 120），加快回放
    p.update(overrides)
    return p


def _rising_closes(n: int = 300, base: float = 100.0, step: float = 0.1) -> list[float]:
    return [base + i * step for i in range(n)]


def _flat_then_falling(flat: int = 120, fall: int = 40,
                       base: float = 100.0, step: float = 0.5) -> list[float]:
    """120 根平盘（无 setup 计数）→ 40 根连续递减（setup_buy 数到 9+）。"""
    return [base] * flat + [base - i * step for i in range(1, fall + 1)]


# ── 结构 ─────────────────────────────────────────────────────────────

def test_driver_run_structure(tmp_path):
    """上涨序列（无 TD 买入信号）：结构完整、零成交、槽位全空。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(_rising_closes(100)),
        ledger_dir=tmp_path,
    )
    out = driver.run()

    assert out["scene"] == "mid"
    assert out["symbols"] == ["CRCLX"]
    assert out["timestep"] == "15m"
    assert out["batches"] == 2
    # 100 根 - (min_history 40 - 1) = 61 个有效 bar
    assert out["bars"] == 61
    assert len(out["net_values"]) == 61
    assert out["fills"] == 0
    assert all(s["open"] == [] for s in out["slots"].values())
    # 上涨行情 TD 不出买入（SELL 无持仓 → fail-closed SKIP，无成交）
    assert out["net_values"][-1]["net"] == pytest.approx(2000.0, abs=1e-3)


def test_driver_trend_down_triggers_buy(tmp_path):
    """下跌序列 setup_buy≥9 → 真实成交（BacktestBroker 撮合）。

    下跌持续 → 每批独立止损（A1 stop_loss 场景化）可能已平仓，open 不硬
    断言——验证点 = 成交发生 + 决策链（BUY→止损/SELL）走实盘同一代码。
    """
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(_flat_then_falling(60, 40)),
        ledger_dir=tmp_path,
    )
    out = driver.run()

    assert out["fills"] >= 1, out
    # 净值 = 现金 + 持仓×现价（每 slot 初始 1000U）；交易有摩擦 → 略低于 2000
    assert out["net_values"][-1]["net"] > 0
    assert out["net_values"][-1]["net"] < 2000.0


# ── hooks（回测注入，实盘零影响） ────────────────────────────────────

def test_hooks_injected_backtest_broker():
    """_slot_broker_factory / _price_source_override 注入后：
    子账号 broker 与取价全部走回测内存实现（零网络）。"""
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters)
    s.logger = logging.getLogger("td-hook-test")

    fake_broker = object()
    s._slot_broker_factory = lambda slot: fake_broker
    assert s._cex_slot_broker({"slot": 1, "account_id": "x"}) is fake_broker

    s._price_source_override = lambda cur: 42.0
    assert s._cex_price_of("CRCLX") == 42.0
    assert s._cex_price_of("USDT") == 42.0


def test_hooks_default_none_keeps_live_path():
    """未注入时 hooks 为 None（默认）——实盘走真实 CexBroker 路径不变。"""
    s = TdSequentialStrategy()
    s.parameters = dict(TdSequentialStrategy.parameters)
    s.logger = logging.getLogger("td-hook-test")

    assert getattr(s, "_slot_broker_factory", None) is None
    assert getattr(s, "_price_source_override", None) is None


# ── 隔离 ─────────────────────────────────────────────────────────────

def test_ledger_isolation(tmp_path):
    """台账写入独立目录（回测干净重放），不碰实盘 batches 文件。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(_flat_then_falling(60, 40)),  # 成交 → 台账落盘
        ledger_dir=tmp_path,
    )
    out = driver.run()
    assert out["fills"] >= 1

    # 批次台账落在 tmp_path（驱动注入 ledger_dir），文件名含 backtest 通道
    files = list(tmp_path.glob("batches.*.json"))
    assert len(files) == 1
    assert files[0].name == "batches.backtest.mid.CRCLX.json"
    import json

    ledger = json.loads(files[0].read_text(encoding="utf-8"))
    assert ledger["symbol"] == "CRCLX"
    assert "slots" in ledger


def test_scene_missing_fail_closed():
    """不存在的场景 → ValueError（绝不静默回退到其他场景配置）。"""
    with pytest.raises(ValueError, match="场景"):
        BacktestDriver(scene="nope", params=_params())


def test_symbols_and_batches_override(tmp_path):
    """CLI 覆盖：symbols/batches 覆盖场景配置，不影响实盘文件。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        symbols=["RENDER"],
        batches=3,
        fetcher=_FakeFetcher(_rising_closes(200)),
        ledger_dir=tmp_path,
    )
    assert driver.symbols == ["RENDER"]
    assert driver.batches == 3
    out = driver.run()
    assert out["symbols"] == ["RENDER"]
    assert out["batches"] == 3
    assert list(out["slots"].keys()) == ["RENDER"]


def test_fixed_amount_override(tmp_path):
    """回测覆盖 TD 固定金额（quantity_mode=fixed_amount 时生效），不回写实盘。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        symbols=["RENDER"],
        fixed_amount=5.0,
        fetcher=_FakeFetcher(_rising_closes(200)),
        ledger_dir=tmp_path,
    )
    assert driver.fixed_amount == 5.0
    # 覆盖值合并进场景参数（_activate_scene 每 bar 从 rt.params 重读）
    assert driver._merged_params["td_fixed_amount"] == 5.0
    out = driver.run()
    assert out["symbols"] == ["RENDER"]
    assert out["fetched_bars"] > 0
    # 策略实际生效值（覆盖 > 场景）：修复前被 _activate_scene 覆盖回场景值
    assert driver.strategy.fixed_amount == 5.0
    # 结果带实际生效配置（诊断显示用）
    assert out["backtest_config"]["td_fixed_amount"] == 5.0
    # 2026-08-29：结果区先展示回测参数——策略实际生效值快照
    cfg = out["backtest_config"]
    assert cfg["min_hold_bars"] is not None
    assert cfg["entry_setup"] is not None
    assert cfg["exit_setup"] is not None
    assert cfg["stop_loss_pct"] is not None
    assert cfg["take_profit_pct"] is not None
    assert cfg["slippage"] is not None
    assert cfg["start_ts"] and cfg["end_ts"]


def test_insufficient_history_fail_closed(tmp_path):
    """数据不足 min_history（120）→ 明确报错（不静默空跑）。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(_rising_closes(30)),  # < min_history 40
        ledger_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="历史数据不足"):
        driver.run()


# ── 结构化结果（WebUI 回测页消费） ────────────────────────────────

def test_driver_result_roi_and_fills_detail(tmp_path):
    """结构化结果：initial_total / final_net / roi / fills_detail。"""
    driver = BacktestDriver(
        scene="mid",
        params=_params(),
        fetcher=_FakeFetcher(_flat_then_falling(60, 40)),
        ledger_dir=tmp_path,
    )
    out = driver.run()

    assert out["initial_total"] == pytest.approx(2000.0)
    assert out["fetched_bars"] == 100
    assert out["bars"] == out["fetched_bars"] - (driver.min_history - 1)
    assert out["final_net"] == pytest.approx(out["net_values"][-1]["net"])
    assert out["roi"] == pytest.approx(out["final_net"] / out["initial_total"] - 1.0, abs=1e-5)
    assert isinstance(out["fills_detail"], list)
    assert len(out["fills_detail"]) == out["fills"]
    for f in out["fills_detail"]:
        assert {"ts", "slot", "scene", "symbol", "side", "quantity", "strategy_price", "avg_price", "reason", "state"} <= set(f)
        assert f["side"] in ("buy", "sell")
        assert f["state"] in ("LONG", "EXIT")
        assert f["scene"] == "mid"


def _two_cycles_closes() -> list[float]:
    """40 根平盘 → 下跌 15（setup 9 → BUY slot1）→ 平盘 10（setup 重置）→
    下跌 15（setup 9 → BUY slot2）→ 平盘 10 → 下跌 15（两 slot 均 open → SKIP）。

    整体单调下行（每段从上一平台价继续下探），避免衔接处 close 跳升
    触发 setup_sell≥9 误出 SELL 或触发 SL/TP 价格出场。"""
    flat = [100.0] * 40
    d1 = [100.0 - i * 0.5 for i in range(1, 16)]  # 99.5 → 92.5
    r1 = [92.5] * 10
    d2 = [92.0 - i * 0.5 for i in range(1, 16)]  # 91.5 → 84.5
    r2 = [84.5] * 10
    d3 = [84.0 - i * 0.5 for i in range(1, 16)]  # 83.5 → 76.5
    return flat + d1 + r1 + d2 + r2 + d3 + [76.5] * 15


def test_driver_multi_slot_fills_detail(tmp_path):
    """回归：跨 slot oid 冲突（两 broker 各自从 bt0 起）不再丢 fills_detail。"""
    params = _params()
    # 关闭价格出场（SL/TP），只保留 TD 信号驱动，保证两个信号周期各成交 1 笔
    params["scenes"]["mid"].update({"stop_loss_pct": 0.0, "take_profit_pct": 0.0})
    driver = BacktestDriver(
        scene="mid",
        params=params,
        fetcher=_FakeFetcher(_two_cycles_closes()),
        ledger_dir=tmp_path,
    )
    out = driver.run()

    # 两个独立信号周期 → 两个 slot 各成交 1 笔
    assert out["fills"] == 2, out["fills_detail"]
    assert len(out["fills_detail"]) == out["fills"], out["fills_detail"]
    assert [f["side"] for f in out["fills_detail"]] == ["buy", "buy"]
    assert {f["slot"] for f in out["fills_detail"]} == {1, 2}
    # 每笔 detail 都带完整字段
    for f in out["fills_detail"]:
        assert {"ts", "slot", "symbol", "side", "quantity", "avg_price"} <= set(f)

# ── 时间戳归一化（回归：datetime 对象传 ReplayDataSource 报 int() 错） ──

def test_to_ts_normalizes_datetime():
    from nanobot_quant.backtest.replay_data_source import _to_ts

    dt = datetime(2026, 8, 22, 0, 0, 0)
    assert _to_ts(dt) == int(dt.replace(tzinfo=timezone.utc).timestamp())
    assert _to_ts(None) is None
    assert _to_ts(1780000000) == 1780000000
    assert _to_ts("2026-08-22") == _to_ts(datetime(2026, 8, 22))
    assert _to_ts(3.5) == 3


def test_replay_data_source_accepts_datetime():
    from nanobot_quant.backtest.replay_data_source import ReplayDataSource, _to_ts

    src = ReplayDataSource(
        symbols=["SOL"],
        timestep="1m",
        start_ts=datetime(2026, 8, 22),
        end_ts=datetime(2026, 8, 23),
        length=120,
        fetcher=lambda pair, s, e, bar: None,
    )
    assert src._user_start_ts == _to_ts(datetime(2026, 8, 22))
    assert src._prefetch_start_ts == _to_ts(datetime(2026, 8, 22)) - 119 * 60  # 预热窗口
    assert src._end_ts == _to_ts(datetime(2026, 8, 23))

# ── bar 映射（回归：driver 传 Gate 风格 "15m" 曾落到默认 "1D"） ──


# ── bar 映射（回归：driver 传 Gate 风格 "15m" 曾落到默认 "1D"） ──

def test_bar_map_accepts_both_styles():
    from nanobot_quant.backtest.replay_data_source import (
        ReplayDataSource,
        _BAR_MAP,
        _bar_map,
        _resolve_bar,
    )

    # driver._timestep_for 输出（Gate 风格统一周期名，经 _bar_map() 合并 spec 声明）
    m = _bar_map()
    assert m["15m"] == "15m"
    assert m["1m"] == "1m"
    assert m["1H"] == "1H"
    assert m["1D"] == "1D"
    # lumibot 风格（td_live 场景 timestep，模块级 _BAR_MAP 保留）
    assert _BAR_MAP["15min"] == "15m"
    assert _BAR_MAP["hour"] == "1H"
    assert _BAR_MAP["day"] == "1D"

    src = ReplayDataSource(symbols=["SOL"], timestep="15m", length=120)
    assert src._bar == "15m"


def test_driver_timestep_for_new_periods():
    """2026-08-24 方案 C：driver 支持 16 个周期（含新周期），fail-closed。"""
    from nanobot_quant.backtest.driver import _timestep_for

    assert _timestep_for("1m") == "1m"
    assert _timestep_for("15m") == "15m"
    assert _timestep_for("1H") == "1H"
    assert _timestep_for("4H") == "4H"
    assert _timestep_for("1D") == "1D"
    # 新周期
    assert _timestep_for("3m") == "3m"
    assert _timestep_for("2H") == "2H"
    assert _timestep_for("6H") == "6H"
    assert _timestep_for("8H") == "8H"
    assert _timestep_for("12H") == "12H"
    assert _timestep_for("3D") == "3D"
    assert _timestep_for("7D") == "7D"
    assert _timestep_for("30D") == "30D"
    assert _timestep_for("1W") == "1W"
    # fail-closed
    with pytest.raises(ValueError, match="2m"):
        _timestep_for("2m")
    with pytest.raises(ValueError, match="1s"):
        _timestep_for("1s")


def test_bar_map_new_periods():
    """2026-08-24 方案 C：16 个周期全支持（含新周期），fail-closed。"""
    from nanobot_quant.backtest.replay_data_source import _resolve_bar

    # 新周期 Gate 风格（统一名 / 小写归一）
    assert _resolve_bar("2H") == "2H"
    assert _resolve_bar("2h") == "2H"   # .lower() 归一
    assert _resolve_bar("6H") == "6H"
    assert _resolve_bar("8H") == "8H"
    assert _resolve_bar("12H") == "12H"
    assert _resolve_bar("3m") == "3m"
    assert _resolve_bar("3D") == "3D"
    assert _resolve_bar("3d") == "3D"
    assert _resolve_bar("7D") == "7D"
    assert _resolve_bar("30D") == "30D"
    assert _resolve_bar("1W") == "1W"
    # bar: 前缀直拉
    assert _resolve_bar("bar:2H") == "2H"
    # fail-closed：未知周期抛 ValueError，不静默回退日线
    with pytest.raises(ValueError, match="2m"):
        _resolve_bar("2m")
    with pytest.raises(ValueError, match="bogus"):
        _resolve_bar("bogus")
    # 空 timestep = 未指定，用默认 1D
    assert _resolve_bar(None) == "1D"


def test_replay_data_source_accepts_datetime():
    from nanobot_quant.backtest.replay_data_source import ReplayDataSource, _to_ts

    src = ReplayDataSource(
        symbols=["SOL"],
        timestep="1m",
        start_ts=datetime(2026, 8, 22),
        end_ts=datetime(2026, 8, 23),
        length=120,
        fetcher=lambda pair, s, e, bar: None,
    )
    assert src._user_start_ts == _to_ts(datetime(2026, 8, 22))
    assert src._prefetch_start_ts == _to_ts(datetime(2026, 8, 22)) - 119 * 60  # 预热窗口
    assert src._end_ts == _to_ts(datetime(2026, 8, 23))

# ── 黑名单误伤（回归：1m 深度上限 400 曾把 CRCLX 标成「无交易对」） ──

def test_request_depth_limit_does_not_blacklist(monkeypatch):
    import urllib.error
    import urllib.request

    from nanobot_quant import gate_cex_data as g

    class FakeResp:
        def read(self):
            return b'{"label":"INVALID_PARAM_VALUE"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_open(req, timeout=20):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, FakeResp()
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    g.clear_blacklist()
    with pytest.raises(urllib.error.HTTPError):
        g._request("CRCLX_USDT", "1m", 100)
    assert g.blacklist_reason("CRCLX") is None


def test_request_missing_pair_blacklists(monkeypatch):
    import urllib.error
    import urllib.request

    from nanobot_quant import gate_cex_data as g

    class FakeResp:
        def read(self):
            return b'{"label":"INVALID_CURRENCY_PAIR"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_open(req, timeout=20):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, FakeResp()
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    g.clear_blacklist()
    with pytest.raises(urllib.error.HTTPError):
        g._request("MU_USDT", "1m", 100)
    assert g.blacklist_reason("MU") is not None


def test_get_datetime_contract():
    from datetime import datetime, timezone

    from nanobot_quant.backtest.replay_data_source import ReplayDataSource

    src = ReplayDataSource(
        symbols=["SOL"],
        timestep="15m",
        length=120,
        fetcher=lambda pair, s, e, bar: None,
    )
    assert src.get_datetime().tzinfo is not None  # 未拉数 → 当前 UTC
    # lumibot v4.5.78 以带 adjust_for_delay 关键字调用
    assert src.get_datetime(adjust_for_delay=True).tzinfo is not None
    src.seek(datetime(2026, 8, 22, tzinfo=timezone.utc))
    assert src.get_datetime() == datetime(2026, 8, 22, tzinfo=timezone.utc)
    assert src.get_datetime(adjust_for_delay=False) == datetime(
        2026, 8, 22, tzinfo=timezone.utc
    )


def test_driver_cd13_loss_holds(tmp_path):
    """driver 级复现：BUY 后下跌 → cd_sell 13 触发 → 保本门应拦（浮亏不卖）。

    序列：120 平盘 + 30 下跌（buy setup 9 → BUY 建仓）+ 60 继续跌
    （sell setup 9 → sell countdown 13）→ cd_sell 13 触发时浮亏 → 死扛。
    """
    closes = [100.0] * 120 + [100 - 0.5 * i for i in range(1, 31)] + \
             [85 - 0.2 * i for i in range(1, 61)]
    params = _params()
    params["scenes"]["mid"].update({
        "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
        "cd_exit_min_profit": 0.0, "cd_exit_all": True,
        "sell_only_profit": 0.003, "td_sell_all": True,
    })
    driver = BacktestDriver(
        scene="mid", params=params,
        fetcher=_FakeFetcher(closes, bar="1m"),
        ledger_dir=tmp_path,
    )
    out = driver.run()
    sells = [f for f in out["fills_detail"] if f["side"] == "sell"]
    assert out["fills"] >= 1, "应有 BUY 成交"
    assert not sells, f"浮亏应死扛，但出现卖出: {sells}"


def test_replay_prefetch_warmup_and_eval_start():
    """引擎增强：prefetch 提前 length-1 根（TD 计数覆盖更早重置点），
    评估从用户 start 起——与实盘 KlineCache（从启动起累积）对齐。"""
    from nanobot_quant.backtest.replay_data_source import ReplayDataSource

    calls = []

    def fetcher(pair, start, end, bar):
        calls.append((pair, start, end, bar))
        idx = pd.date_range(
            pd.Timestamp(start, unit="s", tz="UTC"), periods=200, freq="1min"
        )
        return pd.DataFrame(
            {"close": [100.0] * 200, "open": [99.0] * 200,
             "high": [101.0] * 200, "low": [98.0] * 200,
             "volume": [1.0] * 200},
            index=idx,
        )

    user_start = int(pd.Timestamp("2026-08-28 00:00", tz="UTC").timestamp())
    ds = ReplayDataSource(
        symbols=["SOL"], timestep="1m", start_ts=user_start,
        length=90, fetcher=fetcher,
    )
    ds.prefetch()

    # prefetch 起点 = 用户 start − (length−1)×60s（预热窗口）
    assert calls[0][1] == user_start - 89 * 60
    # 评估起点 = 第一个 ≥ 用户 start 的索引（预热段不评估）
    assert ds.start_idx == 89
    # 预热段数据存在（TD 计数用它）
    assert ds.bar_times[0].timestamp() == user_start - 89 * 60
    # 无 start（拉满）时退回旧行为：length−1 预热
    ds2 = ReplayDataSource(
        symbols=["SOL"], timestep="1m", start_ts=None, length=90,
        fetcher=fetcher,
    )
    ds2.prefetch()
    assert ds2.start_idx == 89


def test_driver_min_hold_follows_exec_params(tmp_path):
    """min_hold_bars 跟随 exec_params 全局（回测表单无该键）；表单可覆盖。

    2026-08-29 修复：此前 _build_strategy 只读回测表单的 min_hold_bars、
    缺省恒 10，/config/exec 改全局/场景值对回测无效（回测与实盘不一致）。
    """
    # 场景 A：表单无 min_hold_bars 键 → 回退 exec_params 全局（隔离路径无文件 → 默认 10）
    p = _params()
    p.pop("min_hold_bars", None)
    driver = BacktestDriver(
        scene="mid", params=p, fetcher=_FakeFetcher(_rising_closes(100)),
        ledger_dir=tmp_path,
    )
    assert driver._min_hold_bars_value() == 10

    # 场景 B（用户实测 bug）：exec_params 全局改 0 → 回测生效 0
    import json
    raw = _params()
    raw["min_hold_bars"] = 0
    (tmp_path / "exec_params.json").write_text(json.dumps(raw), encoding="utf-8")
    p2 = _params()
    p2.pop("min_hold_bars", None)  # 模拟回测表单无该键
    driver2 = BacktestDriver(
        scene="mid", params=p2, fetcher=_FakeFetcher(_rising_closes(100)),
        ledger_dir=tmp_path,
    )
    assert driver2._min_hold_bars_value() == 0

    # 场景 C：表单显式覆盖优先（回测页未来加字段时）
    p3 = _params(min_hold_bars=25)
    driver3 = BacktestDriver(
        scene="mid", params=p3, fetcher=_FakeFetcher(_rising_closes(100)),
        ledger_dir=tmp_path,
    )
    assert driver3._min_hold_bars_value() == 25
