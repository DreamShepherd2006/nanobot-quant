"""``options_driver`` 单测 —— 注入假 fetcher，零网络。

覆盖三条记账路径（卖出开仓 / 止盈买回 / 到期结算）与两条 fail-closed
（现金担保不足、无 mark 的持仓不动），以及 instId 到期反解。

**为什么不打网络**：驱动只负责「重放 + 记账」，决策全部来自实盘的
``evaluate_entry`` / ``evaluate_exits``（各自已有单测）。这里要锁的是
*数据流与记账* 的正确性，用确定性假数据即可；真实拉数由探针与 HF 实测覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from nanobot_quant.backtest.options_driver import (
    OptionsBacktestDriver, SimPosition, _count_skips, _exp_ms_of, _to_ms,
)
from nanobot_quant.bs_pricing import bs_price, years_to_expiry

# ── 假数据：标的单调下跌 → TD 买9（下跌衰竭） ─────────────────────────

_N = 80
_IDX = pd.date_range("2026-09-10 00:00", periods=_N, freq="1h", tz="UTC")

# 信号到位的样子（TD 触发细节由引擎自身单测覆盖，驱动测试关心数据流）
_SIG = {"setup_buy": 9, "setup_sell": 0, "cd_buy": 0, "cd_sell": 0,
        "score": 5.0, "price": 88.0}


def _closes() -> list[float]:
    """前段上涨造出**翻转点**，后段连续下跌触发买9 衰竭。

    注意：``td_sequential`` / ``cycle`` 变体带 DeMark 标准的「翻转确认」——
    必须前一根是反向的才启动计数。全程单调下跌的数据永远数不到 9
    （只有无翻转确认的 ``futu`` 变体会累加）。真实行情里就是先涨后跌。
    """
    up = [100.0 + i * 0.5 for i in range(20)]          # 上涨 20 根
    down = [up[-1] - (i + 1) * 0.6 for i in range(_N - 20)]   # 下跌 60 根
    return up + down


def _spot_at(i: int) -> float:
    return _closes()[min(i, _N - 1)]


def _kline() -> pd.DataFrame:
    c = _closes()
    return pd.DataFrame(
        {"open": c, "high": [x + 0.05 for x in c],
         "low": [x - 0.05 for x in c], "close": c, "volume": 1000.0},
        index=_IDX)


def _exp_ms(inst_id: str) -> int:
    return int(datetime.strptime("20" + inst_id.split("-")[2], "%Y%m%d")
               .replace(hour=8, tzinfo=timezone.utc).timestamp() * 1000)


# 固定一组合约（新路径的合约来自成交，不再靠 instId 推算）
_CONTRACTS = [f"SOL-USD_UM-260918-{k}-P" for k in (80, 85, 88, 90, 95, 100)]


def _archive(start_ms: int, end_ms: int) -> list[dict]:
    """按 BS 造成交：现价随 bar 下跌，因此 put 权利金单调**上涨**（天然不触发止盈）。

    新数据源从**成交**反解 IV（归档里没有 mark），所以假数据也必须是成交价。
    """
    out: list[dict] = []
    for inst_id in _CONTRACTS:
        strike = float(inst_id.split("-")[3])
        exp = _exp_ms(inst_id)
        for i, t in enumerate(_IDX):
            t_ms = int(t.timestamp() * 1000)
            if not (start_ms <= t_ms <= end_ms):
                continue
            tv = years_to_expiry(t_ms, exp)
            if tv <= 0:
                continue
            px = bs_price(_spot_at(i), strike, tv, 0.6, 0.0, "P")
            if not px or px <= 0:
                continue
            out.append({"instrument_name": inst_id, "created_time": str(t_ms),
                        "price": str(px), "size": "1", "side": "sell"})
    return out


def _fake_chain(ts=None, slippage=0.0) -> dict:
    """一条最小可用链 —— 直接喂给驱动，不经过枚举/prefetch。

    数据源自己的合约装载与 IV 曲面由 ``test_options_chain_dict.py`` 覆盖；
    这里测的是**驱动把链接给实盘选档、再按选档结果记账**这条链路。
    """
    strike, bid = 80.0, 2.0
    return {
        "ts": ts, "spot": 88.0, "lot_coin": 0.1,
        "groups": [{
            "days": 4.5,
            "rows": [{"strike": strike, "P": {
                "inst_id": f"SOL-USD_UM-260918-{int(strike)}-P",
                "bid": bid, "mark_px": bid, "iv": 0.55, "delta": -0.25,
                "entry_reason": "buy9(setup_buy=9)"}}],
        }],
        "stats": {"total": 1, "kept": 1, "expired": 0,
                  "no_spot": 0, "no_iv": 0},
    }


def _driver(**kw) -> OptionsBacktestDriver:
    d = OptionsBacktestDriver(
        "SOL-USD_UM", timestep="1H",
        start_ts=int(_IDX[0].timestamp()), end_ts=int(_IDX[-1].timestamp()),
        td_bars=30, td_params={}, opt_params={"entry_setup": 9, "entry_countdown": 13},
        **kw)
    d.data = _build_data_with_fakes(d)
    return d


def _build_data_with_fakes(d: OptionsBacktestDriver):
    from nanobot_quant.backtest.options_replay_data_source import OptionsReplayDataSource

    ds = OptionsReplayDataSource(
        family="SOL-USD_UM", timestep="1H",
        start_ts=d.start_ts, end_ts=d.end_ts, length=d.td_bars,
        fetcher=lambda inst, bar, s, e: _kline(),
        archive_fetcher=lambda s, e: _archive(s, e))
    ds.prefetch()
    return ds


# ── instId 到期反解（右侧取段，_UM 后缀会破坏左侧假设）────────────────

# ── 张数上限：回测以页面参数为准 ─────────────────────────────

def _driver_with_opt_params(params: dict) -> OptionsBacktestDriver:
    """构造 driver 后替换 opt_params —— ``_driver()`` 已固定传入一份，不能重复传。"""
    d = _driver()
    d.opt_params = params
    d._opt_cache = None      # 丢掉构造阶段可能已缓存的旧值
    return d


def test_opt_params_lifts_total_to_family_cap():
    """页面填单家族 10、实盘全局 3 → 全局抬到 10。

    否则 min(per_family, total) 会把用户设的值吃成 3（实盘默认值），
    页面上「填了 10 却只开 3」无法解释。
    """
    d = _driver_with_opt_params({"entry_setup": 9, "max_contracts_per_family": 10,
                                 "max_contracts_total": 3})
    p = d._opt_params()
    assert p["max_contracts_total"] == 10
    assert p["max_contracts_per_family"] == 10
    assert any("张数上限" in n for n in d.notes)


def test_opt_params_keeps_total_when_not_smaller():
    """单家族 1（跟随实盘）、全局 3 → 全局不动，实盘语义原样保留。"""
    d = _driver_with_opt_params({"entry_setup": 9, "max_contracts_per_family": 1,
                                 "max_contracts_total": 3})
    p = d._opt_params()
    assert p["max_contracts_total"] == 3
    assert not any("张数上限" in n for n in d.notes)


def test_opt_params_cached_no_duplicate_notes():
    """多处调用只读一次配置 —— 否则 notes 会重复刷屏。"""
    d = _driver_with_opt_params({"max_contracts_per_family": 10, "max_contracts_total": 3})
    a, b = d._opt_params(), d._opt_params()
    assert a is b
    assert len([n for n in d.notes if "张数上限" in n]) == 1


@pytest.mark.parametrize("inst,expect", [
    ("SOL-USD_UM-260918-100-P", "2026-09-18 08:00 UTC"),
    ("SOL-USD_UM-260918-100.5-P", "2026-09-18 08:00 UTC"),
    ("BTC-USD_UM-261231-56000-C", "2026-12-31 08:00 UTC"),
])
def test_exp_ms_of_parses_from_right(inst, expect):
    ms = _exp_ms_of(inst, _IDX[0])
    got = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    assert got == expect


def test_exp_ms_of_falls_back_on_garbage():
    """畸形 instId 不抛异常 —— 回退到 ts + 1 天（fail-soft，不炸整轮回测）。"""
    assert _exp_ms_of("garbage", _IDX[0]) == _to_ms(_IDX[0]) + 86400_000


def test_count_skips_buckets_by_prefix():
    s = _count_skips(["无 TD 衰竭信号（setup_buy=3/9 cd_buy=0/13）",
                      "无 TD 衰竭信号（setup_buy=5/9 cd_buy=0/13）",
                      "担保不足：需 100.00 + 已占 0.00 > 可用 50.00"])
    assert s["无 TD 衰竭信号"] == 2
    assert s["担保不足"] == 1


# ── 记账层 ────────────────────────────────────────────────────────

def test_settle_otm_collects_premium_and_closes():
    d = _driver()
    pos = [SimPosition("SOL-USD_UM-260911-50-P", "SOL-USD_UM", 50.0,
                       _exp_ms("SOL-USD_UM-260911-50-P"), 1, 0.5,
                       _IDX[0], "buy9(setup_buy=9)", 0.1)]
    fills: list[dict] = []
    d.data.seek(_IDX[40])                  # 结算价 = 该时刻标的价格
    cash = d._settle_expired(_IDX[40], pos, fills, 1000.0)
    assert pos == []                      # 已平
    assert cash == 1000.0                 # OTM 零赔付，权利金在开仓时已收
    assert fills[0]["side"] == "settle_otm"
    assert fills[0]["pnl_usd"] == pytest.approx(0.5 * 0.1 * 1, rel=1e-9)


def test_settle_itm_pays_intrinsic():
    d = _driver()
    inst = "SOL-USD_UM-260911-200-P"      # strike 200 >> 现价 → 深度 ITM
    pos = [SimPosition(inst, "SOL-USD_UM", 200.0, _exp_ms(inst), 2, 1.0,
                       _IDX[0], "buy9", 0.1)]
    fills: list[dict] = []
    d.data.seek(_IDX[40])
    settle = d.data.price_of()
    assert 0 < settle < 200               # 前置校验：确实是 ITM 场景
    cash = d._settle_expired(_IDX[40], pos, fills, 1000.0)
    payout = (200.0 - settle) * 0.1 * 2
    assert fills[0]["side"] == "settle_itm"
    assert cash == pytest.approx(1000.0 - payout, rel=1e-9)


def test_check_exits_skips_when_no_mark():
    """无 mark 的持仓保持不动（fail-safe，不误平）。"""
    d = _driver()
    pos = [SimPosition("SOL-USD_UM-260920-NOPE-P", "SOL-USD_UM", 50.0,
                       _exp_ms("SOL-USD_UM-260920-50-P"), 1, 1.0,
                       _IDX[0], "buy9", 0.1)]
    fills: list[dict] = []
    d.data.seek(_IDX[40])
    cash = d._check_exits(_IDX[40], pos, fills, 1000.0)
    assert len(pos) == 1 and cash == 1000.0 and fills == []


def test_check_exits_takes_profit_reuses_live_decision():
    """止盈走实盘 evaluate_exits —— 回落够就买回，记账价含滑点与手续费。"""
    d = _driver(tp_pct=50.0)
    d.data.seek(_IDX[40])
    # 合约池筛 -P：驱动只卖 put，且方向隔离后 put 止盈线不管 call（§24 C42）；
    # 同时要求「开仓价 999 回落过半」成立——否则本用例前提不成立，与用例意图无关
    inst = next(k for k in d.data._contracts
                if k.endswith("-P") and _exp_ms(k) > _to_ms(_IDX[40])
                and (d.data.premium_of(k, _IDX[40]) or 1e9) < 999.0 * 0.5)
    pos = [SimPosition(inst, "SOL-USD_UM", float(inst.split("-")[3]),
                       _exp_ms(inst), 1, 999.0, _IDX[0], "buy9", 0.1)]
    mark = d.data.premium_of(inst, _IDX[40])
    assert mark is not None and mark < 999.0 * 0.5   # 回落过半，必触发止盈
    fills: list[dict] = []
    cash = d._check_exits(_IDX[40], pos, fills, 1000.0)
    assert pos == []
    assert fills[0]["side"] == "close"
    # 买回吃 ask（与入场 bid 同一套家族 Δσ + tick 地板口径，C43-①）
    buy_px = d.data.ask_at(inst, _IDX[40], extra_slip=d.slippage)
    assert buy_px is not None and buy_px > 0
    assert buy_px > mark            # ask 严格高于中价 mark，不是用中价成交
    rec_px = fills[0]["avg_px"]
    # 记录层把成交价 round 到 6 位（既有设计，不是本次改动引入）
    assert rec_px == pytest.approx(buy_px, abs=1e-6)
    # 期权手续费 = Min(名义价值 × 费率, 7% 权利金)——官方口径（2026-09-19 实盘验证）
    strike = float(fills[0]["strike"])
    nominal_fee = strike * 0.1 * 1 * d.fee_rate
    cap_fee = 0.07 * rec_px * 0.1 * 1
    assert fills[0]["fee_usd"] == pytest.approx(round(min(nominal_fee, cap_fee), 6))
    cost = rec_px * 0.1 * 1 + fills[0]["fee_usd"]
    assert cash == pytest.approx(1000.0 - cost, rel=1e-9)


def test_collateral_gate_fails_closed():
    """现金担保铁律：占用超可用现金 → 不下单（fail-closed）。"""
    d = _driver(initial_cash=1.0)                 # 少到买不起任何担保
    d.data.seek(_IDX[-1])
    pos: list[SimPosition] = []
    fills: list[dict] = []
    cash = d._try_entry(_IDX[-1], pos, fills, 1.0)
    assert pos == [] and fills == [] and cash == 1.0


def test_sim_position_collateral_matches_strike_times_lot():
    p = SimPosition("X", "SOL-USD_UM", 100.0, 0, 3, 1.0, _IDX[0], "buy9", 0.1)
    assert p.collateral == pytest.approx(100.0 * 0.1 * 3)
    assert p.as_position_row(2.5) == {"inst_id": "X", "side": "short",
                                      "pos": 3, "avg_px": 1.0, "mark_px": 2.5}


# ── 端到端（假数据全链路） ─────────────────────────────────────────

def test_run_end_to_end_shape(monkeypatch):
    """整轮跑通：结构完整、信号到位时产生卖开仓、KPI 自洽。

    TD 触发链路依赖 K 线细节（翻转确认、比较长度）——那是引擎自己的单测范围。
    这里把「信号到位」打桩，专测**数据流与记账**：链→选档→担保校验→记账。
    """
    d = _driver(tp_pct=50.0)
    monkeypatch.setattr(type(d), "_td_signal_at", lambda self, ts: _SIG)
    monkeypatch.setattr(type(d.data), "chain_dict_at",
                        lambda self, ts=None, slippage=0.0, dsigma_pts=None,
                        tick=None, **_kw: _fake_chain(ts, slippage))
    res = d.run()
    assert "error" not in res, res.get("error")
    for key in ("kpi", "fills", "final_positions", "skips", "bars",
                "contracts", "notes", "elapsed_s"):
        assert key in res, key
    assert res["bars"]["fetched"] >= _N      # 预取可能合并多批，不少于原始根数
    assert res["bars"]["evaluated"] > 0
    # 信号到位 + 假链可选档 → 必然卖出开仓
    assert any(f["side"] == "sell_open" for f in res["fills"]), res["skips"]
    k = res["kpi"]
    assert k["fills"] == len(res["fills"])
    assert k["final_net_usd"] == pytest.approx(
        res["initial_cash"] * (1 + k["roi_pct"] / 100), rel=1e-6)


def test_run_records_open_positions_with_mark(monkeypatch):
    """期末未平仓按最后 bar 的 mark 折算（「如现在全部买回」口径）。"""
    d = _driver(tp_pct=999.0)                     # 止盈线不可达 → 必然留下未平仓
    monkeypatch.setattr(type(d), "_td_signal_at", lambda self, ts: _SIG)
    monkeypatch.setattr(type(d.data), "chain_dict_at",
                        lambda self, ts=None, slippage=0.0, dsigma_pts=None,
                        tick=None, **_kw: _fake_chain(ts, slippage))
    res = d.run()
    opens = res["final_positions"]
    assert opens, "止盈不可达时必然留下未平仓"
    for row in opens:
        assert set(row) >= {"inst_id", "strike", "sz", "entry_px", "mark_px",
                            "collateral_usd", "mark_value_usd", "pnl_pct"}
        assert row["collateral_usd"] == pytest.approx(
            row["strike"] * 0.1 * row["sz"])


def test_run_open_premium_separate_from_settled(monkeypatch):
    """未平仓那张的权利金单列（与净值闭合），不混进「已了结」栏。

    2026-09-25 实测：权利金栏 0.358 = 3 张已了结之和（0.124+0.152+0.082），
    而净值含第 4 张（未平仓、权利金 0.066）—— 只给一栏会看着像算错。
    """
    d = _driver(tp_pct=999.0)                     # 止盈线不可达 → 必然留下未平仓
    monkeypatch.setattr(type(d), "_td_signal_at", lambda self, ts: _SIG)
    monkeypatch.setattr(type(d.data), "chain_dict_at",
                        lambda self, ts=None, slippage=0.0, dsigma_pts=None,
                        tick=None, **_kw: _fake_chain(ts, slippage))
    res = d.run()
    opens = res["final_positions"]
    assert opens, "止盈不可达时必然留下未平仓"
    lot = 0.1                                     # SOL 家族每张面值
    expect = sum(r["entry_px"] * lot * r["sz"] for r in opens)
    assert res["kpi"]["open_premium_usd"] == pytest.approx(round(expect, 4), abs=1e-4)
    # 已了结栏只能来自带 premium_usd 的记录（到期/买回），不含未平仓那张
    settled = sum(f.get("premium_usd") or 0 for f in res["fills"])
    assert res["kpi"]["premium_income_usd"] == pytest.approx(round(settled, 4), abs=1e-4)


def test_run_result_is_json_serializable(monkeypatch):
    """端点返回前必须能 JSON 序列化 —— 曾因 pandas Timestamp 直接 500。

    这条是回归锁：修的是 ``fills[].ts`` 用 ``str(ts)`` 而非 Timestamp 对象。
    """
    import json

    d = _driver(tp_pct=999.0)
    monkeypatch.setattr(type(d), "_td_signal_at", lambda self, ts: _SIG)
    monkeypatch.setattr(type(d.data), "chain_dict_at",
                        lambda self, ts=None, slippage=0.0, dsigma_pts=None,
                        tick=None, **_kw: _fake_chain(ts, slippage))
    res = d.run()
    json.dumps(res)          # 不带 default=，必须原生可序列化
