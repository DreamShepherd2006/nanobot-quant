"""期权回测驱动 —— 逐 bar 重放，跑实盘同一份决策函数。

对齐现货侧四层结构（E 期步 3）：

============  ==========================================================
①回放数据源    ``OptionsReplayDataSource``（#349 已交付）
②撮合          本文件内联记账 —— 家族 Δσ 价差（卖收 bid / 买付 ask）+ 名义手续费
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

import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

DEFAULT_SLIPPAGE_PCT = 0.0    # 价差之上**额外**的对称价格滑点（%）；默认 0
# —— 盘口价差由「家族 Δσ + tick 地板」模型给出（okx_options_trade.
# FAMILY_DSIGMA_PTS / FAMILY_TICK，生产 tape 实测，C43-① 方案 A）；
# 填入 >0 表示在此之上再加一层价格滑点（压力测试/保守估计用）。
DEFAULT_FEE_RATE = 0.0003     # 期权 taker 名义费率（OKX 按名义价值收，非权利金比例）
DEFAULT_INITIAL_CASH = 10000.0
# 合约枚举尾部延伸由 OptionsReplayDataSource 内置（_ENUM_TAIL_DAYS = 7）——
# 区间尾部持有的 put 常在 end_ts 之后才到期，只枚举到 end_ts 会无链可卖。


@dataclass
class SimPosition:
    """模拟空头 put 持仓。

    字段刻意与 ``okx_options_trade.open_option_positions()`` 同形状，使
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
        dsigma_pts: Optional[float] = None,
        tick: Optional[float] = None,
        progress_cb: Any = None,
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
        # 价差模型覆盖（仅 CLI/实验用；None = 家族实测常数 + 家族 tick）：
        # dsigma_pts=0 且 tick=0 时退化为「mark 中价」，配 --slippage 0.5 即旧代理模型。
        self.dsigma_pts = None if dsigma_pts is None else max(0.0, float(dsigma_pts))
        self.tick = None if tick is None else max(0.0, float(tick))
        # 进度回调（接入层用来把 progress 写进 run 文件）；日志走 stderr
        self._progress_cb = progress_cb

        self.data = None
        self.notes: list[str] = []
        self.skips: list[str] = []
        self._ask_fallback_logged: set[str] = set()   # 出场回退留痕去重
        self._opt_cache: Optional[dict] = None
        self._effective_cap: Optional[int] = None

    # ── 日志 / 进度 ─────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """回测日志 —— 一律走 stderr。

        与 ``[OPT-LIVE]`` 同规矩：stdout 可能被 MCP stdio / lumibot handler
        占用，日志只能走 stderr；也必须 flush，否则被管道缓冲吞掉、跑完才
        一次性吐出来（等于没日志）。
        """
        print(f"[OPT-BT] {msg}", file=sys.stderr, flush=True)

    def _progress(self, stage: str, done: int, total: int, extra: str = "") -> None:
        if self._progress_cb is None:
            return
        try:
            self._progress_cb({
                "stage": stage, "done": done, "total": total,
                "pct": round(done * 100.0 / total, 1) if total else 0.0,
                "extra": extra,
            })
        except Exception:  # noqa: BLE001 —— 进度只是 UX，绝不阻塞回测
            pass

    @staticmethod
    def _fill_line(f: dict, cash: float) -> str:
        """一条成交的单行摘要（开仓/平仓/到期三种口径）。"""
        side = {"sell_open": "开仓 SELL", "sell_close": "平仓 BUY",
                "settle_otm": "到期作废 OTM", "settle_itm": "到期被行权 ITM"}.get(
                    f.get("side"), str(f.get("side")))
        head = (f"{side} {f.get('ts')} {f.get('inst_id')} sz={f.get('sz')} "
                f"K={f.get('strike')}")
        if f.get("side") == "sell_open":
            return (f"{head} px={f.get('avg_px')} iv={f.get('iv')} "
                    f"delta={f.get('delta')} 剩余={f.get('days')}天 "
                    f"净收益率={f.get('net_yield_pct')}% "
                    f"手续费={f.get('fee_usd')} 原因={f.get('reason')}"
                    f" | cash={cash:.2f}")
        pnl = f.get("pnl_usd")
        return (f"{head} px={f.get('avg_px') or f.get('settle_px')} "
                f"盈亏={pnl} 手续费={f.get('fee_usd')} 原因={f.get('reason')}"
                f" | cash={cash:.2f}")

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
            length=self.td_bars,
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
        """实盘同一份策略参数（entry_setup / 张数上限 / selector / IV 闸门）。

        缓存 —— 该函数一次回测里会被多处调用（入场评估 / 选档 / 参数日志），
        重复读配置既慢又会让 notes 重复。
        """
        if self._opt_cache is not None:
            return self._opt_cache
        p = dict(self.opt_params or {})
        if not p:
            try:
                from nanobot_quant.okx_options_live import _strategy_params, live_config

                p = _strategy_params(live_config())
            except Exception as exc:  # noqa: BLE001 —— 配置缺失不阻塞回测
                self.notes.append(f"策略参数回退默认（读取失败：{type(exc).__name__}）")
        # 张数上限：实盘有两个值 —— 单家族 ``max_contracts_per_family`` 与跨家族
        # 总量 ``max_contracts_total``，策略取小者生效。回测基本只跑一个家族，
        # 后者在此只会无谓地卡住页面设的值（实盘默认 3 → 页面填 10 也会被吃成 3）。
        # 因此：页面上填的单家族上限更大时，把总量一并抬到同值 —— 回测以页面参数为准。
        try:
            per = int(p.get("max_contracts_per_family") or 0)
            tot = int(p.get("max_contracts_total") or 0)
            if per > 0 and per > tot:
                self.notes.append(
                    f"张数上限：跨家族总量 {tot or '未设'} → {per}"
                    f"（回测单家族，跟随单家族上限）"
                )
                p["max_contracts_total"] = per
        except (TypeError, ValueError):
            pass
        self._opt_cache = p
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
                "ts": str(ts), "inst_id": p.inst_id, "side": "settle_itm" if itm else "settle_otm",
                "sz": p.sz, "strike": p.strike, "settle_px": settle,
                "spot": round(self.data.price_of() or 0.0, 4),
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
        exits = evaluate_exits(rows, tp_pct=self.tp_pct, opt_type="P")
        if not exits:
            return cash
        by_inst = {p.inst_id: p for p in positions}
        done: list[str] = []
        for e in exits:
            p = by_inst.get(e.inst_id)
            if p is None:
                continue
            # 买回吃 ask（与入场 bid 同一套家族 Δσ + tick 地板口径）。
            # 取不到 ask 时回退 mark×(1+滑点) 并留痕 —— 静默降价不可接受。
            buy_px = self.data.ask_at(p.inst_id, ts, extra_slip=self.slippage,
                                      dsigma_pts=self.dsigma_pts, tick=self.tick)
            if buy_px is None or buy_px <= 0:
                buy_px = e.mark_px * (1 + self.slippage)
                if p.inst_id not in self._ask_fallback_logged:
                    self._ask_fallback_logged.add(p.inst_id)
                    self.notes.append(
                        f"[出场] {p.inst_id} 无 ask（Δσ 模型）→ 回退 mark×(1+滑点)")
            fee = self._option_fee(p.strike, p.lot_coin, e.sz, premium_px=buy_px)
            cash -= buy_px * p.lot_coin * e.sz + fee
            fills.append({
                "ts": str(ts), "inst_id": p.inst_id, "side": "close",
                "sz": e.sz, "strike": p.strike, "strategy_px": p.entry_px,
                "avg_px": round(buy_px, 6), "fee_usd": round(fee, 6),
                "spot": round(self.data.price_of() or 0.0, 4),
                "reason": f"止盈（回落 {e.drop_pct:.1f}% ≥ {self.tp_pct:g}%）",
            })
            done.append(e.inst_id)
        if done:
            positions[:] = [p for p in positions if p.inst_id not in done]
        return cash

    def _option_fee(self, strike: float, lot: float, sz: int,
                    premium_px: float | None = None) -> float:
        """期权手续费 = Min(名义价值 × fee_rate, 7% × 权利金)。

        OKX 期权 taker 费率作用于名义（strike × 面值 × 张数）—— 实盘
        ``okx_options_trade.option_fee_est()`` 就是这个口径。回测原先写成
        ``权利金 × fee_rate``，与实盘差 strike/premium 倍（实测 98-P 差 81 倍），
        手续费被系统性少扣、PnL 高估。

        ``premium_px`` 传入时再套官方 cap（7% 权利金）——薄权利金合约受其保护，
        不套会让回测扣费高于实盘、ROI 被低估（与实盘口径同步）。
        """
        from nanobot_quant.okx_options_trade import OPTION_FEE_CAP_RATIO
        fee = abs(float(strike) * float(lot) * int(sz)) * self.fee_rate
        if premium_px is not None:
            premium = abs(float(premium_px)) * float(lot) * int(sz)
            fee = min(fee, OPTION_FEE_CAP_RATIO * premium)
        return fee

    def _try_entry(self, ts, positions: list, fills: list,
                   cash: float) -> float:
        """TD 衰竭信号 + 选档 + 记账 —— 复用实盘 ``evaluate_entry()``。"""
        from nanobot_quant.okx_options_strategy import (
            cycle_gate,
            cycle_mark_bought,
            evaluate_entry,
        )

        spot = self.data.price_of()
        if spot <= 0:
            return cash
        sig = self._td_signal_at(ts)
        if sig is None:
            return cash

        # 信号周期门控（与实盘策略同一份纯函数）—— 同一衰竭波只开一次，
        # 避免 setup 9→10→11 连开多张把样本打虚（实测虚高 1.7 倍）。
        # 放在选链之前：被门控拦下时不必算链。
        if not hasattr(self, "_cycle_state"):
            self._cycle_state: dict = {}
        gate = cycle_gate(
            self._cycle_state, self.family, td_signal=sig,
            params=self._opt_params(),
            has_position=any(getattr(p, "family", None) == self.family
                             for p in positions))
        if gate:
            self.skips.append(f"周期门控：{gate}")
            return cash

        chain = self.data.chain_dict_at(ts, opt_type="P", dsigma_pts=self.dsigma_pts,
                                        tick=self.tick, slippage=self.slippage)
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
        sell_px = d.bid                     # 家族 Δσ 模型的买一价（选档成交同口径）
        fee = self._option_fee(d.strike, lot, d.sz, premium_px=sell_px)
        premium = sell_px * lot * d.sz - fee
        cash += premium
        positions.append(SimPosition(
            inst_id=d.inst_id, family=self.family, strike=d.strike,
            exp_ms=_exp_ms_of(d.inst_id, ts), sz=d.sz, entry_px=sell_px,
            entry_ts=ts, entry_reason=d.entry_reason, lot_coin=lot))
        # 建仓即置位 —— 本周期内不再开仓（与实盘同一份纯函数）
        cycle_mark_bought(self._cycle_state, self.family)
        fills.append({
            "ts": str(ts), "inst_id": d.inst_id, "side": "sell_open", "sz": d.sz,
            "strike": d.strike, "avg_px": round(sell_px, 6),
            "fee_usd": round(fee, 6),
            "spot": round(spot, 4),
            "iv": d.iv, "delta": d.delta, "days": d.days,
            "net_yield_pct": d.net_yield_pct,
            "reason": d.entry_reason,
        })
        return cash

    # ── 主流程 ──────────────────────────────────────────────

    def run(self) -> dict:
        t0 = time.time()
        self.skips = []
        self._log(
            f"启动 family={self.family} timestep={self.timestep} "
            f"td_bars={self.td_bars} 初始={self.initial_cash:.2f} "
            f"额外滑点={self.slippage * 100:.2f}% 手续费率={self.fee_rate} "
            f"价差模型=家族Δσ+tick地板 "
            f"区间={self.start_ts or '默认'}→{self.end_ts}"
        )
        op = self._opt_params()
        # selector 不在策略参数里（它是 option_params.json 的兄弟字段）——
        # 统一走 selector_params() 这个唯一入口，没传就用实盘磁盘配置。
        from nanobot_quant.okx_options_select import selector_params

        sel = selector_params(op)
        self._log(
            f"策略参数 entry_setup={op.get('entry_setup')} "
            f"entry_countdown={op.get('entry_countdown')} "
            f"td_period={op.get('td_period')} "
            f"单家族上限={op.get('max_contracts_per_family')} "
            f"全局上限={op.get('max_contracts_total')} "
            f"IV闸门={op.get('iv_min_percentile')} 止盈={op.get('take_profit_pct')}%"
        )
        self._log(
            f"选档 最小距离={sel['min_distance_pct']}% "
            f"delta={sel['delta_min']}~{sel['delta_max']} "
            f"到期窗={sel['expiry_min_days']}~{sel['expiry_max_days']}天 "
            f"净收益率下限={sel['min_net_yield_pct']}% "
            f"top={sel['top_n']} 排序={sel['sort_by']}"
        )
        # 两个上限同时存在时取小值 —— 不写出来的话，“设了 5 却只开 3”看着像 bug。
        self._effective_cap = None
        try:
            cap = min(int(op.get("max_contracts_per_family") or 10 ** 6),
                      int(op.get("max_contracts_total") or 10 ** 6))
            self._effective_cap = cap
            self._log(f"生效张数上限={cap}（单家族 × ={op.get('max_contracts_per_family')}、"
                      f"全局 × ={op.get('max_contracts_total')}，取小值）")
        except (TypeError, ValueError):
            pass
        self._progress("prefetch", 0, 1)
        self.data = self._build_data()
        self.data.prefetch()

        bt = self.data.bar_times
        from nanobot_quant.okx_options_trade import (
            family_dsigma_pts,
            family_tick,
        )
        out: dict[str, Any] = {
            "family": self.family, "timestep": self.timestep,
            "bar": self.data.bar, "ref_inst": self.data.ref_inst,
            "start_ts": None, "end_ts": None,
            "initial_cash": self.initial_cash,
            "slippage_pct": self.slippage * 100, "fee_rate": self.fee_rate,
            "spread_model": "family_dsigma+tick",
            "spread_model_note": (
                ("Δσ 覆盖=%.2f IV 点、tick 覆盖=%.4g（实验）" % (self.dsigma_pts, self.tick))
                if (self.dsigma_pts is not None or self.tick is not None) else
                (f"家族 Δσ + tick 地板（{self.family}："
                 f"Δσ={family_dsigma_pts(self.family):g} IV 点，"
                 f"tick={family_tick(self.family):g}）")
                + f"；额外滑点 {self.slippage * 100:.2f}%"),
            "td_bars": self.td_bars, "tp_pct": self.tp_pct,
            "bars": {"fetched": len(bt), "evaluated": 0},
            "contracts": {"in_archive": len(self.data._contracts),
                          "with_iv": (len({p.inst_id
                                           for p in self.data._surface.points})
                                       if self.data._surface else 0)},
            "fills": [], "final_positions": [], "skips": {},
        }
        if not bt:
            out["error"] = "没有标的 K 线"
            out["notes"] = list(self.data.notes)
            self._log(f"失败：没有标的 K 线 notes={out['notes']}")
            return out

        stats = None
        try:
            stats = self.data.window_spot_stats()
        except Exception:  # noqa: BLE001  # 现货统计只是报告增强
            stats = None
        if stats:
            out["spot_range"] = {k: round(v, 4) for k, v in stats.items()}
        idx = self.data.start_idx
        positions: list[SimPosition] = []
        fills: list[dict] = []
        cash = self.initial_cash
        out["start_ts"] = str(bt[idx])
        out["end_ts"] = str(bt[-1])
        out["bars"]["evaluated"] = len(bt) - idx
        self._log(
            f"数据 bar={len(bt)} 预热={idx} 评估={len(bt) - idx} 根 "
            f"({out['start_ts']} → {out['end_ts']}) "
            f"合约={out['contracts']['in_archive']} 档、有IV={out['contracts']['with_iv']} "
            f"参考现货={self.data.ref_inst}"
            + (f"（{out['spot_range']['first']:g} → {out['spot_range']['last']:g}）"
               if out.get("spot_range") else "")
        )
        for n in self.data.notes:
            self._log(f"数据备注：{n}")

        total = len(bt) - idx
        step = max(1, total // 20)          # 每 ~5% 打一次进度
        seen = 0
        for i, ts in enumerate(bt[idx:], 1):
            self.data.seek(ts)
            if self.data.price_of() <= 0:
                continue
            cash = self._settle_expired(ts, positions, fills, cash)
            cash = self._check_exits(ts, positions, fills, cash)
            cash = self._try_entry(ts, positions, fills, cash)

            # 新成交逐笔上日志（开仓 / 平仓 / 到期三种都经这里落出）
            while seen < len(fills):
                self._log(self._fill_line(fills[seen], cash))
                seen += 1

            if i % step == 0 or i == total:
                self._log(
                    f"进度 {i * 100 // total}% ({i}/{total}) "
                    f"持仓={len(positions)} 成交={len(fills)} 现金={cash:.2f}"
                )
                self._progress("replay", i, total,
                               f"持仓={len(positions)} 成交={len(fills)}")

        # 期末市值：未平仓按最后 bar 的 mark 折算（「如现在全部买回」）
        self._log(f"重放结束，计算期末市值（未平仓 {len(positions)} 笔）…")
        last_ts = bt[-1]
        self.data.seek(last_ts)
        open_value, open_rows = 0.0, []
        # 未平仓那张的权利金早已进 cash，但不在 ``premium_usd``（只有了结记录带
        # 这个字段）里 —— 单列出来才与净值闭合。
        open_premium = 0.0
        for p in positions:
            mark = self.data.premium_of(p.inst_id, last_ts)
            if mark is None:
                mark = p.entry_px
            val = mark * p.lot_coin * p.sz
            open_value += val
            open_premium += p.entry_px * p.lot_coin * p.sz
            open_rows.append({
                "inst_id": p.inst_id, "strike": p.strike, "sz": p.sz,
                "entry_px": round(p.entry_px, 6), "mark_px": round(mark, 6),
                "collateral_usd": round(p.collateral, 4),
                "mark_value_usd": round(val, 4),
                "pnl_pct": round((p.entry_px - mark) / p.entry_px * 100, 2)
                if p.entry_px else None,
            })

        # 卖出开仓收到的权利金已在 ``cash`` 里，而期末持仓是**负债** —— 还欠市场
        # 一张 put，按「如现在全部买回」的口径应当**扣减** ``open_value``，不是相加。
        # 原先写成 ``cash + open_value`` 把负债当资产，净值被高估 2 × open_value。
        net = cash - open_value
        out["fills"] = fills
        out["final_positions"] = open_rows
        out["skips"] = _count_skips(self.skips)
        # 生效张数上限（页面/日志同源）—— 「填了 10 却只开 3」这类闷棍靠它显形
        out["max_contracts"] = {
            "effective": self._effective_cap,
            "per_family": op.get("max_contracts_per_family"),
            "total": op.get("max_contracts_total"),
        }
        out["kpi"] = {
            "final_net_usd": round(net, 4),
            "roi_pct": round((net - self.initial_cash) / self.initial_cash * 100, 4),
            # 权利金口径：``premium_usd`` 只出现在到期/买回了结的记录里 ⇒ 本项是
            # **已了结**部分；期末未平仓那张的权利金在 cash 里、却不在此列（净值含它）。
            "premium_income_usd": round(sum(f.get("premium_usd") or 0 for f in fills), 4),
            "open_premium_usd": round(open_premium, 4),
            "payout_usd": round(sum(f.get("payout_usd") or 0 for f in fills), 4),
            "open_mark_value_usd": round(open_value, 4),
            "fills": len(fills),
            "wins": sum(1 for f in fills if (f.get("pnl_usd") or 0) > 0),
            "losses": sum(1 for f in fills if (f.get("pnl_usd") or 0) < 0),
        }
        out["notes"] = list(self.data.notes) + self.notes
        out["elapsed_s"] = round(time.time() - t0, 1)
        k = out["kpi"]
        self._log(
            f"完成 用时={out['elapsed_s']}s 评估={out['bars']['evaluated']} 根 "
            f"成交={k['fills']}（盈 {k['wins']} / 亏 {k['losses']}） "
            f"期末持仓={len(open_rows)} 净值={k['final_net_usd']} "
            f"ROI={k['roi_pct']}% 权利金={k['premium_income_usd']} 赔付={k['payout_usd']}"
        )
        if out["skips"]:
            self._log(f"SKIP 汇总：{out['skips']}")
        for n in self.notes:
            self._log(f"备注：{n}")
        self._progress("done", 1, 1)
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
    ap.add_argument("--dsigma", type=float, default=None,
                    help="Δσ 覆盖（IV 点）；0 配 --tick 0 即旧「mark 中价」模型（实验）")
    ap.add_argument("--tick", type=float, default=None, help="px tick 覆盖（实验）")
    ap.add_argument("--tp", type=float, default=None, help="止盈回落%%（缺省跟随实盘）")
    ap.add_argument("--cash", type=float, default=DEFAULT_INITIAL_CASH)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    drv = OptionsBacktestDriver(
        a.family, timestep=a.timestep, end_ts=int(time.time()),
        start_ts=int(time.time()) - a.days * 86400,
        td_bars=a.td_bars, slippage_pct=a.slippage, tp_pct=a.tp,
        initial_cash=a.cash, dsigma_pts=a.dsigma, tick=a.tick)
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
