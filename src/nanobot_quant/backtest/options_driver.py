"""期权回测驱动 —— 逐 bar 重放，跑实盘同一份决策函数。

对齐现货侧四层结构（E 期步 3）：

============  ==========================================================
①回放数据源    ``OptionsReplayDataSource``（#349 已交付）
②撮合          本文件内联记账 —— mark ± 滑点 + 名义手续费（期权 taker 按名义计费）
③驱动          本文件
④接入层        WebUI / MCP / CLI（后续 PR）
============  ==========================================================

**策略代码零改动**（E 期设计约束）：

* 入场 → ``okx_options_strategy.evaluate_entry()``（含 TD 衰竭信号 + IV 闸门 + 张数上限）
* 选档 → ``okx_options_select.select_puts()``（经 ``chain_dict_at()`` 桥接，过滤链一行不改）
* 出场 → ``okx_options_strategy.evaluate_exits()``（权利金回落止盈）

回测与实盘走的是**同一批函数**；回测验证过的参数，实盘语义一致。

资金模型（对齐「100% 现金担保」铁律）：每笔卖 put 占用
``strike × 面值 × 张数`` 的可用现金，占用不足则 fail-closed 跳过该笔。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_SLIPPAGE_PCT = 0.5    # mark 中价 ± 滑点（%）；与现货回测「滑点是百分比」同口径
DEFAULT_FEE_RATE = 0.0003     # 期权 taker 名义费率（OKX 按名义价值收，非权利金比例）
DEFAULT_INITIAL_CASH = 10000.0
# 合约枚举尾部延伸由 OptionsReplayDataSource 内置（_ENUM_TAIL_DAYS = 7）——
# 区间尾部持有的 put 常在 end_ts 之后才到期，只枚举到 end_ts 会无链可卖。


@dataclass
class SimPosition:
    """模拟空头 put 持仓。

    字段刻意与 ``okx_options_trade.open_puts()`` 同形状，使
    ``evaluate_exits()`` 能原样复用 —— 回测不另写一套出场判定。
    """

    inst_id: str
    family: str
    strike: float
    exp_ms: int
    sz: int
    entry_px: float          # 每名义币权利金（USD）
    entry_ts: Any
    entry_reason: str
    lot_coin: float

    @property
    def collateral(self) -> float:
        """现金担保额 = 行权价 × 面值 × 张数（铁律：足额现金，无杠杆）。"""
        return self.strike * self.lot_coin * self.sz

    def as_position_row(self, mark_px: Optional[float]) -> dict:
        return {"inst_id": self.inst_id, "side": "short", "pos": self.sz,
                "avg_px": self.entry_px, "mark_px": mark_px}


class OptionsBacktestDriver:
    """期权卖 put 策略回测（逐 bar 重放）。"""

    def __init__(
        self,
        family: str,
        *,
        timestep: str = "15m",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        td_bars: int = 120,
        strategy_name: Optional[str] = None,
        td_params: Optional[dict] = None,
        opt_params: Optional[dict] = None,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
        fee_rate: float = DEFAULT_FEE_RATE,
        tp_pct: Optional[float] = None,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        strike_pct: float = 0.2,
    ) -> None:
        self.family = str(family).upper()
        self.timestep = timestep
        self.start_ts = start_ts
        self.end_ts = int(end_ts or time.time())
        self.td_bars = max(20, int(td_bars))
        self.strategy_name = strategy_name
        self.td_params = td_params
        self.opt_params = opt_params or {}
        self.slippage = max(0.0, float(slippage_pct)) / 100.0   # 百分比 → 小数
        self.fee_rate = max(0.0, float(fee_rate))
        self.tp_pct = tp_pct
        self.initial_cash = float(initial_cash)
        self.strike_pct = strike_pct

        self.data = None
        self.notes: list[str] = []
        self.skips: list[str] = []

    # ── 构造 ────────────────────────────────────────────────

    def _build_data(self) -> Any:
        from nanobot_quant.backtest.options_replay_data_source import (
            OptionsReplayDataSource,
        )

        start = self.start_ts
        if start is None:
            # 留出 TD 窗口与合约枚举余量，否则首个可评估 bar 会落在很后面
            start = self.end_ts - 90 * 86400
        return OptionsReplayDataSource(
            family=self.family, timestep=self.timestep,
            start_ts=int(start), end_ts=self.end_ts,
            length=self.td_bars, strike_pct=self.strike_pct,
        )

    def _strategy_and_params(self) -> tuple[str, dict]:
        """策略变体与 TD 参数 —— 默认跟随实盘配置，允许显式注入覆盖。"""
        name = self.strategy_name
        params = self.td_params
        if name is None or params is None:
            from nanobot_quant.strategies.registry import load_selected
            from nanobot_quant.td_params import load_td_params

            name = name or load_selected()
            params = params if params is not None else load_td_params(name)
        return name, params

    def _opt_params(self) -> dict:
        """实盘同一份策略参数（entry_setup / 张数上限 / selector / IV 闸门）。"""
        p = dict(self.opt_params or {})
        if not p:
            try:
                from nanobot_quant.okx_options_live import _strategy_params, live_config

                p = _strategy_params(live_config())
            except Exception as exc:  # noqa: BLE001 —— 配置缺失不阻塞回测
                self.notes.append(f"策略参数回退默认（读取失败：{type(exc).__name__}）")
        return p

    # ── 每 bar 的三件事 ─────────────────────────────────────

    def _td_signal_at(self, ts) -> Optional[dict]:
        """标的 TD 信号（取窗口最后一根）—— 与实盘/td-table 同一条计算路径。"""
        df = self.data._underlying
        if df is None or df.empty:
            return None
        window = df.loc[:ts].tail(self.td_bars)
        if len(window) < self.td_bars:
            return None
        try:
            from nanobot_quant.td_table_handlers import _engine_run

            name, params = self._strategy_and_params()
            seq = _engine_run(window, name, params)
            last = seq.iloc[-1]
        except Exception as exc:  # noqa: BLE001 —— 单 bar 计算失败不炸整轮
            self.notes.append(f"TD 计算失败 @{ts}：{type(exc).__name__}: {exc}")
            return None
        return {
            "setup_buy": int(last.get("buy_setup_count", 0) or 0),
            "setup_sell": int(last.get("sell_setup_count", 0) or 0),
            "cd_buy": int(last.get("buy_countdown_count", 0) or 0),
            "cd_sell": int(last.get("sell_countdown_count", 0) or 0),
            "score": float(last.get("combined_score", 0) or 0),
            "price": float(last.get("Close", 0) or 0),
        }

    def _settle_expired(self, ts, positions: list, fills: list,
                        cash: float) -> float:
        """到期现金结算（先于止盈/入场 —— 到期仓位不再有选择权）。"""
        ts_ms = _to_ms(ts)
        still: list = []
        for p in positions:
            if p.exp_ms > ts_ms:
                still.append(p)
                continue
            settle = self.data.price_of()       # 到期时刻的标的价格（近似结算价）
            itm = settle > 0 and settle < p.strike
            payout = (p.strike - settle) * p.lot_coin * p.sz if itm else 0.0
            cash -= payout
            fills.append({
                "ts": ts, "inst_id": p.inst_id, "side": "settle_itm" if itm else "settle_otm",
                "sz": p.sz, "strike": p.strike, "settle_px": settle,
                "payout_usd": round(payout, 6),
                "premium_usd": round(p.entry_px * p.lot_coin * p.sz, 6),
                "pnl_usd": round(p.entry_px * p.lot_coin * p.sz - payout, 6),
                "reason": "到期被行权" if itm else "到期作废（全收权利金）",
            })
        positions[:] = still
        return cash

    def _check_exits(self, ts, positions: list, fills: list,
                     cash: float) -> float:
        """权利金回落止盈 —— 复用实盘 ``evaluate_exits()``。"""
        from nanobot_quant.okx_options_strategy import evaluate_exits

        rows, alive = [], []
        for p in positions:
            mark = self.data.premium_of(p.inst_id, ts)
            if mark is None or mark <= 0:
                alive.append(p)          # 无 mark 的持仓保持不动（fail-safe）
                continue
            alive.append(p)
            rows.append(p.as_position_row(mark))
        exits = evaluate_exits(rows, tp_pct=self.tp_pct)
        if not exits:
            return cash
        by_inst = {p.inst_id: p for p in positions}
        done: list[str] = []
        for e in exits:
            p = by_inst.get(e.inst_id)
            if p is None:
                continue
            buy_px = e.mark_px * (1 + self.slippage)
            fee = buy_px * p.lot_coin * e.sz * self.fee_rate
            cash -= buy_px * p.lot_coin * e.sz + fee
            fills.append({
                "ts": ts, "inst_id": p.inst_id, "side": "close",
                "sz": e.sz, "strike": p.strike, "strategy_px": p.entry_px,
                "avg_px": round(buy_px, 6), "fee_usd": round(fee, 6),
                "reason": f"止盈（回落 {e.drop_pct:.1f}% ≥ {self.tp_pct:g}%）",
            })
            done.append(e.inst_id)
        if done:
            positions[:] = [p for p in positions if p.inst_id not in done]
        return cash

    def _try_entry(self, ts, positions: list, fills: list,
                   cash: float) -> float:
        """TD 衰竭信号 + 选档 + 记账 —— 复用实盘 ``evaluate_entry()``。"""
        from nanobot_quant.okx_options_strategy import evaluate_entry

        spot = self.data.price_of()
        if spot <= 0:
            return cash
        sig = self._td_signal_at(ts)
        if sig is None:
            return cash
        chain = self.data.chain_dict_at(ts, slippage=self.slippage)
        d, note = evaluate_entry(
            self.family, td_signal=sig, params=self._opt_params(),
            open_contracts=len(positions), total_contracts=len(positions),
            chain=chain, base_px=spot)
        if d is None:
            self.skips.append(note)
            return cash
        # 现金担保铁律：占用 = strike × 面值 × 张数
        occupied = sum(p.collateral for p in positions)
        lot = float(chain.get("lot_coin") or 0.1)
        collateral = d.strike * lot * d.sz
        if occupied + collateral > cash:
            self.skips.append(
                f"担保不足：需 {collateral:.2f} + 已占 {occupied:.2f} > 可用 {cash:.2f}")
            return cash
        sell_px = d.bid                     # 已是 mark × (1 − 滑点)，选档成交同口径
        premium = sell_px * lot * d.sz * (1 - self.fee_rate)
        cash += premium
        positions.append(SimPosition(
            inst_id=d.inst_id, family=self.family, strike=d.strike,
            exp_ms=_exp_ms_of(d.inst_id, ts), sz=d.sz, entry_px=sell_px,
            entry_ts=ts, entry_reason=d.entry_reason, lot_coin=lot))
        fills.append({
            "ts": ts, "inst_id": d.inst_id, "side": "sell_open", "sz": d.sz,
            "strike": d.strike, "avg_px": round(sell_px, 6),
            "fee_usd": round(sell_px * lot * d.sz * self.fee_rate, 6),
            "iv": d.iv, "delta": d.delta, "days": d.days,
            "net_yield_pct": d.net_yield_pct,
            "reason": d.entry_reason,
        })
        return cash

    # ── 主流程 ──────────────────────────────────────────────

    def run(self) -> dict:
        t0 = time.time()
        self.skips = []
        self.data = self._build_data()
        self.data.prefetch()

        bt = self.data.bar_times
        out: dict[str, Any] = {
            "family": self.family, "timestep": self.timestep,
            "bar": self.data.bar, "ref_inst": self.data.ref_inst,
            "start_ts": None, "end_ts": None,
            "initial_cash": self.initial_cash,
            "slippage_pct": self.slippage * 100, "fee_rate": self.fee_rate,
            "td_bars": self.td_bars, "tp_pct": self.tp_pct,
            "bars": {"fetched": len(bt), "evaluated": 0},
            "contracts": {"enumerated": len(self.data._contracts),
                          "with_mark": len(self.data._premiums)},
            "fills": [], "final_positions": [], "skips": {},
        }
        if not bt:
            out["error"] = "没有标的 K 线"
            out["notes"] = list(self.data.notes)
            return out

        idx = self.data.start_idx
        positions: list[SimPosition] = []
        fills: list[dict] = []
        cash = self.initial_cash
        out["start_ts"] = str(bt[idx])
        out["end_ts"] = str(bt[-1])
        out["bars"]["evaluated"] = len(bt) - idx

        for ts in bt[idx:]:
            self.data.seek(ts)
            if self.data.price_of() <= 0:
                continue
            cash = self._settle_expired(ts, positions, fills, cash)
            cash = self._check_exits(ts, positions, fills, cash)
            cash = self._try_entry(ts, positions, fills, cash)

        # 期末市值：未平仓按最后 bar 的 mark 折算（「如现在全部买回」）
        last_ts = bt[-1]
        self.data.seek(last_ts)
        open_value, open_rows = 0.0, []
        for p in positions:
            mark = self.data.premium_of(p.inst_id, last_ts)
            if mark is None:
                mark = p.entry_px
            val = mark * p.lot_coin * p.sz
            open_value += val
            open_rows.append({
                "inst_id": p.inst_id, "strike": p.strike, "sz": p.sz,
                "entry_px": round(p.entry_px, 6), "mark_px": round(mark, 6),
                "collateral_usd": round(p.collateral, 4),
                "mark_value_usd": round(val, 4),
                "pnl_pct": round((p.entry_px - mark) / p.entry_px * 100, 2)
                if p.entry_px else None,
            })

        net = cash + open_value
        out["fills"] = fills
        out["final_positions"] = open_rows
        out["skips"] = _count_skips(self.skips)
        out["kpi"] = {
            "final_net_usd": round(net, 4),
            "roi_pct": round((net - self.initial_cash) / self.initial_cash * 100, 4),
            "premium_income_usd": round(sum(f.get("premium_usd") or 0 for f in fills), 4),
            "payout_usd": round(sum(f.get("payout_usd") or 0 for f in fills), 4),
            "open_mark_value_usd": round(open_value, 4),
            "fills": len(fills),
            "wins": sum(1 for f in fills if (f.get("pnl_usd") or 0) > 0),
            "losses": sum(1 for f in fills if (f.get("pnl_usd") or 0) < 0),
        }
        out["notes"] = list(self.data.notes) + self.notes
        out["elapsed_s"] = round(time.time() - t0, 1)
        return out


# ── 工具 ────────────────────────────────────────────────────

def _to_ms(ts) -> int:
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    return int(ts)


def _exp_ms_of(inst_id: str, ts) -> int:
    """从 instId 反解到期毫秒时间戳。

    ``SOL-USD_UM-260918-100-P`` → 段从**右侧**倒数取（``_UM`` 后缀会破坏
    左侧段位假设；踩过一次）。
    """
    parts = str(inst_id).split("-")
    if len(parts) < 3:
        return _to_ms(ts) + 86400_000
    exp = parts[-3]                     # YYMMDD
    try:
        dt = datetime.strptime(exp, "%y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return _to_ms(ts) + 86400_000
    # OKX 期权到期时刻 08:00 UTC（现金结算）
    return int((dt.timestamp() + 8 * 3600) * 1000)


def _count_skips(notes: list[str]) -> dict:
    """跳过原因归类 —— 静默降级不可接受，必须看得见被什么拦住。"""
    buckets: dict[str, int] = {}
    for n in notes:
        key = str(n).split("：")[0].split("（")[0][:24]
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：``python -m nanobot_quant.backtest.options_driver --family SOL-USD_UM``。"""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="期权卖 put 策略回测（逐 bar 重放）")
    ap.add_argument("--family", default="SOL-USD_UM")
    ap.add_argument("--timestep", default="15m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--td-bars", type=int, default=120)
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE_PCT)
    ap.add_argument("--tp", type=float, default=None, help="止盈回落%%（缺省跟随实盘）")
    ap.add_argument("--cash", type=float, default=DEFAULT_INITIAL_CASH)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    drv = OptionsBacktestDriver(
        a.family, timestep=a.timestep, end_ts=int(time.time()),
        start_ts=int(time.time()) - a.days * 86400,
        td_bars=a.td_bars, slippage_pct=a.slippage, tp_pct=a.tp,
        initial_cash=a.cash)
    res = drv.run()
    if a.out:
        from pathlib import Path

        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, default=str,
                                          indent=2))
        print(f"结果已写入 {a.out}")
    else:
        print(json.dumps(res.get("kpi", res), ensure_ascii=False,
                         default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
