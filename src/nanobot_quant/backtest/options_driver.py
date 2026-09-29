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

import pandas as pd

DEFAULT_SLIPPAGE_PCT = 0.0    # 价差之上**额外**的对称价格滑点（%）；默认 0
# —— 盘口价差由「家族 Δσ + tick 地板」模型给出（okx_options_trade.
# FAMILY_DSIGMA_PTS / FAMILY_TICK，生产 tape 实测，C43-① 方案 A）；
# 填入 >0 表示在此之上再加一层价格滑点（压力测试/保守估计用）。
DEFAULT_FEE_RATE = 0.0003     # 期权 taker 名义费率（OKX 按名义价值收，非权利金比例）
DEFAULT_INITIAL_CASH = 10000.0
DEFAULT_SETTLE_WINDOW_MIN = 30   # 结算价窗口：官方口径 = 到期前 30 分钟标的均价
DEFAULT_SPOT_FEE_RATE = 0.001    # 现货 taker 0.1%（补买接货；实盘从到货基础币扣）
DEFAULT_TP_CALL_PCT = 30.0       # 卖 call 止盈回落默认（§24 C41 D2，与 put 的 50% 分开）
# 合约枚举尾部延伸由 OptionsReplayDataSource 内置（_ENUM_TAIL_DAYS = 7）——
# 区间尾部持有的 put 常在 end_ts 之后才到期，只枚举到 end_ts 会无链可卖。


@dataclass
class SimPosition:
    """模拟空头期权持仓（put / call）。

    字段刻意与 ``okx_options_trade.open_option_positions()`` 同形状，使
    ``evaluate_exits()`` 能原样复用 —— 回测不另写一套出场判定。
    ``opt_type`` 对应台账的 P/C：止盈线、保本与结算口径都按方向分派
    （§33.39 方向隔离 —— 卖 put 的止盈线不得平掉 call 仓）。
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
    opt_type: str = "P"      # P=卖 put（现金担保）/ C=卖 call（covered）

    @property
    def collateral(self) -> float:
        """现金担保额 = 行权价 × 面值 × 张数（铁律：足额现金，无杠杆）。

        仅对卖 put 成立 —— 卖 call 的担保是现货（covered），不占现金。
        """
        if self.opt_type == "C":
            return 0.0
        return self.strike * self.lot_coin * self.sz

    def as_position_row(self, mark_px: Optional[float]) -> dict:
        return {"inst_id": self.inst_id, "side": "short", "pos": self.sz,
                "avg_px": self.entry_px, "mark_px": mark_px,
                "opt_type": self.opt_type}


@dataclass
class SpotCoverTask:
    """一笔「被行权 → 补买接货」的在途任务（§33.43 资金链段）。

    决策与实盘 ``option_params.cover`` 三模式同构：每一步都交给
    ``okx_options_strategy.evaluate_cover()``（同一份纯函数），本类只保存
    回放需要的状态（目标价 / 起始时刻 / 对应台账行）。
    """

    settle_row: dict         # 对应的到期台账行（回填 cost_basis / cover_status）
    inst_id: str             # 触发接货的 put 合约（归因）
    strike: float
    settle_px: float
    sz: int
    lot_coin: float
    payout_usd: float        # 现金赔付（ITM 内在价值）
    mode: str                # limit / immediate / signal
    discount_pct: float
    timeout_hours: float
    started_ts: Any
    started_epoch: float
    target_px: Optional[float] = None
    status: str = "waiting"  # waiting / done


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
        cover_enabled: bool = True,
        cover_mode: Optional[str] = None,
        cover_discount_pct: Optional[float] = None,
        cover_timeout_hours: Optional[float] = None,
        call_enabled: bool = True,
        tp_call_pct: Optional[float] = None,
        settle_window_min: int = DEFAULT_SETTLE_WINDOW_MIN,
        spot_fee_rate: float = DEFAULT_SPOT_FEE_RATE,
        data_source: Any = None,
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

        # ── 资金链段（§33.43 Step 6 / C41b）─────────────────────
        # 默认「建模全链」（被行权 → 补买 → 成本锚 → covered 卖 call）：回测的
        # 用途就是裁决这条链，关掉它反而看不到真实资金占用。模式/折让/超时
        # 未显式传入时跟随 opt_params.cover（再退实盘 cover_params() 磁盘配置）。
        self.cover_enabled = bool(cover_enabled)
        self.cover_mode = cover_mode
        self.cover_discount_pct = cover_discount_pct
        self.cover_timeout_hours = cover_timeout_hours
        self.call_enabled = bool(call_enabled)
        self.tp_call_pct = tp_call_pct
        self.settle_window_min = max(0, int(settle_window_min))
        self.spot_fee_rate = max(0.0, float(spot_fee_rate))
        self._injected_data = data_source

        self.data = None
        self.notes: list[str] = []
        self.skips: list[str] = []
        self._ask_fallback_logged: set[str] = set()   # 出场回退留痕去重
        self._opt_cache: Optional[dict] = None
        self._effective_cap: Optional[int] = None
        self._cover_cache: Optional[dict] = None
        self._settle_note_logged = False
        self._dedupe_note_logged = False
        # 接货现货（模拟子账号余额）：数量扣除 0.1% 手续费后的到货量
        self._spot_qty = 0.0
        self._spot_spend = 0.0            # 补买累计现金支出
        self._covers: list[SpotCoverTask] = []
        self._cost_bases: list[dict] = []  # 每笔接货的核算成本 C（= 卖 call 的成本锚）

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
                "settle_otm": "到期作废 OTM", "settle_itm": "到期被行权 ITM",
                "cover": "补买接货 BUY"}.get(
                    f.get("side"), str(f.get("side")))
        if f.get("opt_type") == "C":
            side += "(call)"
        if f.get("side") == "cover":
            return (f"{side} {f.get('ts')} {f.get('inst_id')} "
                    f"数量={f.get('qty')} @{f.get('avg_px')} "
                    f"成本锚C={f.get('cost_basis')} 付款={f.get('cost_usd')} "
                    f"模式={f.get('mode')} | cash={cash:.2f}")
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
        if self._injected_data is not None:
            # 多组参数扫描（网格裁决）共用一份已预取的数据源：
            # prefetch 是每次回测的真正耗时大头，重拉一次 = 白烧一遍网络。
            return self._injected_data
        if self.data is not None:
            return self.data          # 调用方/单测已注入
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

    # ── 资金链段辅助（结算口径 / 补买 / covered）──────────

    def _epoch(self, ts) -> float:
        """bar 时刻 → epoch 秒（``evaluate_cover`` 的 ``now`` 参数用）。"""
        if isinstance(ts, datetime):
            return ts.timestamp()
        try:
            v = float(ts)
        except (TypeError, ValueError):
            return time.time()
        return v / 1000.0 if v > 1e11 else v

    def _underlying_col(self, name: str) -> Optional[str]:
        """标的数据帧里的列名（大小写宽容 —— 各数据源大小写不一致）。"""
        df = getattr(self.data, "_underlying", None)
        if df is None:
            return None
        for c in df.columns:
            if str(c).lower() == str(name).lower():
                return c
        return None

    def _bar_low(self, ts) -> Optional[float]:
        col = self._underlying_col("Low")
        if col is None:
            return None
        try:
            val = self.data._underlying.loc[ts, col]
        except Exception:  # noqa: BLE001 —— 取不到就当作无盘中信息
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _settle_px_at(self, exp_ms: int) -> tuple[float, str]:
        """到期结算价 —— 官方口径「到期前 30 分钟标的均价」的回放近似。

        实盘口径（2026-09-16 定案）= 到期前 30 分钟标的指数算术平均。回放只有
        timestep 粒度的标的 K 线，所以取窗口内的**收盘均值**；窗口内无 bar
        （数据断档 / 区间尾部）时退回「≤ 到期时刻的最后一根收盘」并留痕。
        旧实现用「到期时刻现价」——被行权盈亏会系统性偏。
        """
        df = getattr(self.data, "_underlying", None)
        col = self._underlying_col("Close")
        if df is None or df.empty or col is None:
            return 0.0, "无标的 K 线，结算价不可得"
        exp = datetime.fromtimestamp(exp_ms / 1000.0, tz=timezone.utc)
        up_to = df.loc[:exp]
        if up_to.empty:
            return 0.0, f"到期 {exp:%Y-%m-%d %H:%M} UTC 前无 K 线"
        win = self.settle_window_min * 60
        if win > 0:
            mask = up_to.index >= (exp - pd.Timedelta(seconds=win))
            inside = up_to.loc[mask]
        else:
            inside = up_to
        if inside.empty:
            inside = up_to.tail(1)
            note = (f"结算价：窗口内无 bar → 退回最后收盘价"
                    f"（{inside.index[-1]:%Y-%m-%d %H:%M} UTC）")
        else:
            note = (f"结算价：到期前 {self.settle_window_min} 分钟均价"
                    f"（{len(inside)} 根 {self.timestep} 收盘均值）")
        try:
            px = float(inside[col].mean())
        except Exception:  # noqa: BLE001
            return 0.0, "结算价计算失败"
        if not self._settle_note_logged:
            self._settle_note_logged = True
            self.notes.append(note)
        return px, note

    def _cover_cfg(self) -> dict:
        """补买建模参数：显式传入 > opt_params.cover > 实盘磁盘配置 > 内置默认。

        内置默认 = 实盘默认（limit 1% 24h），使「没传参就回测」也能看到资金链。
        """
        if self._cover_cache is not None:
            return self._cover_cache
        raw = self._opt_params().get("cover")
        raw = raw if isinstance(raw, dict) else {}
        live: dict = {}
        try:
            from nanobot_quant.okx_options_trade import cover_params
            live = cover_params() or {}
        except Exception:  # noqa: BLE001 —— 配置不可读不阻塞回测
            live = {}

        def pick(key: str, ctor, default):
            if ctor is not None:
                return ctor
            for src in (raw, live):
                v = src.get(key)
                if v not in (None, ""):
                    return v
            return default

        cfg = {
            "mode": str(pick("mode", self.cover_mode, "limit") or "limit"),
            "discount_pct": float(pick("discount_pct", self.cover_discount_pct, 1.0)),
            "timeout_hours": float(pick("timeout_hours", self.cover_timeout_hours, 24.0)),
        }
        self._cover_cache = cfg
        return cfg

    def _resolve_tp_call_pct(self, op: dict) -> float:
        """卖 call 止盈线：显式 > opt_params.tp_pct_call > 默认 30%"""
        if self.tp_call_pct is not None:
            try:
                return float(self.tp_call_pct)
            except (TypeError, ValueError):
                pass
        try:
            return float(op.get("tp_pct_call") or DEFAULT_TP_CALL_PCT)
        except (TypeError, ValueError):
            return DEFAULT_TP_CALL_PCT

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
        """到期现金结算（先于止盈/入场 —— 到期仓位不再有选择权）。

        结算价口径 = ``_settle_px_at()``（到期前 30 分钟标的均价，官方口径的
        回放近似）。方向分派：put ITM = 结算价 < strike；call ITM = 结算价 >
        strike（covered：现金结算、现货保留不交币）。

        put 被行权且开了补买建模 → 建一条 ``SpotCoverTask`` 交给后续 bar 的
        ``_tick_covers()``（三模式与实盘 ``evaluate_cover()`` 同源）。
        """
        ts_ms = _to_ms(ts)
        still: list = []
        for p in positions:
            if p.exp_ms > ts_ms:
                still.append(p)
                continue
            settle, why = self._settle_px_at(p.exp_ms)
            if settle <= 0:
                settle = float(self.data.price_of() or 0.0)
                self.notes.append(
                    f"结算价不可得（{p.inst_id}）：{why} → 回退最后收盘 {settle:g}")
            if p.opt_type == "C":
                itm = settle > p.strike
                payout = (settle - p.strike) * p.lot_coin * p.sz if itm else 0.0
            else:
                itm = settle < p.strike
                payout = (p.strike - settle) * p.lot_coin * p.sz if itm else 0.0
            cash -= payout
            reason = "到期被行权" if itm else "到期作废（全收权利金）"
            if itm and p.opt_type == "C":
                reason += "·covered 现金结算，现货保留"
            row = {
                "ts": str(ts), "inst_id": p.inst_id,
                "side": "settle_itm" if itm else "settle_otm",
                "opt_type": p.opt_type,
                "sz": p.sz, "strike": p.strike, "settle_px": round(settle, 6),
                "spot": round(self.data.price_of() or 0.0, 4),
                "payout_usd": round(payout, 6),
                "premium_usd": round(p.entry_px * p.lot_coin * p.sz, 6),
                "pnl_usd": round(p.entry_px * p.lot_coin * p.sz - payout, 6),
                "reason": reason,
            }
            if itm and p.opt_type == "P":
                if self.cover_enabled:
                    cfg = self._cover_cfg()
                    task = SpotCoverTask(
                        settle_row=row, inst_id=p.inst_id, strike=p.strike,
                        settle_px=settle, sz=p.sz, lot_coin=p.lot_coin,
                        payout_usd=payout, mode=cfg["mode"],
                        discount_pct=cfg["discount_pct"],
                        timeout_hours=cfg["timeout_hours"],
                        started_ts=ts, started_epoch=self._epoch(ts))
                    if task.mode == "limit":
                        task.target_px = round(
                            settle * (1 - task.discount_pct / 100.0), 8)
                    row["cover_status"] = "waiting"
                    row["cost_basis"] = None
                    self._covers.append(task)
                    self._log(
                        f"补买任务 {p.inst_id} 模式={task.mode} "
                        f"数量={p.lot_coin * p.sz:g} 结算价={settle:.4f}"
                        + (f" 限价目标={task.target_px:.4f}" if task.target_px else "")
                        + f" 超时={task.timeout_hours:g}h（超时→撤单+市价）")
                else:
                    row["cover_status"] = "manual"
                    row["note"] = "补买建模关闭（cover_enabled=False）"
            fills.append(row)
        positions[:] = still
        return cash

    def _check_exits(self, ts, positions: list, fills: list,
                     cash: float) -> float:
        """权利金回落止盈 —— 复用实盘 ``evaluate_exits()``，put / call 分开走。

        §33.39 方向隔离：put 的止盈线（默认 50%）不得平掉 call 仓，call 用
        自己的线（默认 30%）。实盘策略也是两次调用，回测保持同构。
        """
        cash = self._exit_pass(ts, "P", self.tp_pct, positions, fills, cash)
        if self.call_enabled:
            cash = self._exit_pass(ts, "C", self.tp_call_pct, positions, fills, cash)
        return cash

    def _exit_pass(self, ts, right: str, tp, positions: list, fills: list,
                   cash: float) -> float:
        """单方向止盈买回（``right`` = P / C）。"""
        from nanobot_quant.okx_options_strategy import evaluate_exits

        if not any(p.opt_type == right for p in positions):
            return cash
        rows, alive = [], []
        for p in positions:
            if p.opt_type != right:
                continue
            mark = self.data.premium_of(p.inst_id, ts)
            if mark is None or mark <= 0:
                alive.append(p)          # 无 mark 的持仓保持不动（fail-safe）
                continue
            alive.append(p)
            rows.append(p.as_position_row(mark))
        exits = evaluate_exits(rows, tp_pct=tp, opt_type=right)
        if not exits:
            return cash
        by_inst = {p.inst_id: p for p in positions}
        # 建仓手续费——从已落盘的开仓记录取，用于算这张的净盈亏（不重复推导）
        open_fee = {f["inst_id"]: (f.get("fee_usd") or 0.0)
                    for f in fills if f.get("side") == "sell_open"}
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
            e_fee = open_fee.get(p.inst_id, 0.0)
            # 这张的净盈亏 =（卖出价 − 买回价）× 面值 × 张数 − 两笔手续费。
            # 原先买回记录不带 pnl、明细「盈亏」列显示「—」，止盈收益既进不了
            # 「权利金」也进不了 wins/losses ⇒ 报告与净值不闭合（2026-09-26 复验：
            # 7 笔止盈收益 ~0.286 在 KPI 里完全看不到）。
            pnl = (p.entry_px - buy_px) * p.lot_coin * e.sz - e_fee - fee
            fills.append({
                "ts": str(ts), "inst_id": p.inst_id, "side": "close",
                "opt_type": right,
                "sz": e.sz, "strike": p.strike, "strategy_px": p.entry_px,
                "avg_px": round(buy_px, 6), "fee_usd": round(fee, 6),
                "close_cost_usd": round(buy_px * p.lot_coin * e.sz, 6),
                "pnl_usd": round(pnl, 6),
                "spot": round(self.data.price_of() or 0.0, 4),
                "reason": f"止盈（回落 {e.drop_pct:.1f}% ≥ {tp:g}%）",
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
                   cash: float, sig: Optional[dict] = None) -> float:
        """TD 衰竭信号 + 选档 + 记账 —— 复用实盘 ``evaluate_entry()``。

        ``sig`` 已给（主循环每 bar 只算一次）则不重复算 TD。
        """
        from nanobot_quant.okx_options_strategy import (
            cycle_gate,
            cycle_mark_bought,
            evaluate_entry,
        )

        spot = self.data.price_of()
        if spot <= 0:
            return cash
        sig = sig if sig is not None else self._td_signal_at(ts)
        if sig is None:
            return cash
        # put 额度与波锁只看 put 仓（call 额度独立，§33.39 方向隔离）
        puts = [p for p in positions if p.opt_type == "P"]

        # 信号周期门控（与实盘策略同一份纯函数）—— 同一衰竭波只开一次，
        # 避免 setup 9→10→11 连开多张把样本打虚（实测虚高 1.7 倍）。
        # 放在选链之前：被门控拦下时不必算链。
        if not hasattr(self, "_cycle_state"):
            self._cycle_state: dict = {}
        gate = cycle_gate(
            self._cycle_state, self.family, td_signal=sig,
            params=self._opt_params(),
            has_position=any(getattr(p, "family", None) == self.family for p in puts))
        if gate:
            self.skips.append(f"周期门控：{gate}")
            return cash

        chain = self.data.chain_dict_at(ts, opt_type="P", dsigma_pts=self.dsigma_pts,
                                        tick=self.tick, slippage=self.slippage)
        d, note = evaluate_entry(
            self.family, td_signal=sig, params=self._opt_params(),
            open_contracts=len(puts), total_contracts=len(puts),
            chain=chain, base_px=spot,
            # 与实盘同一道现金担保门（§33.43 Step 3）：可用现金 < 全损担保 → fail-closed
            cash_avail=float(cash) - sum(p.collateral for p in puts))
        if d is None:
            self.skips.append(note)
            return cash
        # 现金担保铁律：占用 = strike × 面值 × 张数
        occupied = sum(p.collateral for p in puts)
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
            # 毛权利金收入（每笔开仓都记）—— KPI「权利金收入（毛）」= 本字段求和。
            # 只落在开仓记录上：到期记录也带同名字段（那张的权利金），不能混求。
            "premium_usd": round(sell_px * lot * d.sz, 6),
            "reason": d.entry_reason,
        })
        return cash

    # ── 资金链段：补买接货 → covered 卖 call（§33.43 Step 6 / C41b）──

    def _tick_covers(self, ts, sig, fills: list, cash: float) -> float:
        """推进在途补买任务（三模式与实盘 ``evaluate_cover()`` 同源）。

        限价模式用本 bar 最低价近似盘中触碰；超时兜底交给纯函数（超时优先于
        进行中幂等）。**建立任务的那一根 bar 不动手** —— 结算发生在 bar 中间，
        拿整根 bar 的最低价当盘中路径会系统性占便宜。
        """
        if not self._covers:
            return cash
        from nanobot_quant.okx_options_strategy import evaluate_cover

        op = self._opt_params()
        try:
            e_setup = int(op.get("entry_setup") or 9)
            e_cd = int(op.get("entry_countdown") or 13)
        except (TypeError, ValueError):
            e_setup, e_cd = 9, 13
        now = self._epoch(ts)
        keep: list[SpotCoverTask] = []
        for t in self._covers:
            if t.status != "waiting":
                continue
            if abs(now - t.started_epoch) < 1e-6:
                keep.append(t)                    # 建立当根 bar 不成交
                continue
            px: Optional[float] = None
            # ① 限价触碰：本 bar 最低价跑到了目标价之下
            if t.mode == "limit" and t.target_px:
                low = self._bar_low(ts)
                if low is not None and low <= t.target_px:
                    px = float(t.target_px)
            # ② 决策（超时兜底 / signal 模式）—— 实盘同一份纯函数
            if px is None:
                row = dict(t.settle_row)
                row["cover_status"] = ""         # 不填状态，否则被幂等门拦住
                row["cover_started_at"] = t.started_epoch
                row["settle_px"] = t.settle_px
                dec = evaluate_cover(row, mode=t.mode,
                                     discount_pct=t.discount_pct,
                                     timeout_hours=t.timeout_hours,
                                     signal=sig, now=now,
                                     entry_setup=e_setup,
                                     entry_countdown=e_cd)
                act = dec.get("action")
                if act == "market":
                    px = float(self.data.price_of() or 0.0)
                    t.settle_row["cover_reason"] = dec.get("reason") or ""
                    if px <= 0:
                        self.notes.append(
                            f"[补买] {t.inst_id} {dec.get('reason')} "
                            f"但取价失败 → 下轮重试")
                        keep.append(t)
                        continue
                elif act == "limit":
                    tp = dec.get("target_px")
                    if tp:
                        t.target_px = float(tp)
                    keep.append(t)
                    continue
                else:                            # skip / wait_signal
                    keep.append(t)
                    continue
            cash = self._fill_cover(t, px, ts, fills, cash)
        self._covers = keep
        return cash

    def _fill_cover(self, t: SpotCoverTask, px: float, ts, fills: list,
                    cash: float) -> float:
        """补买成交记账：现金支出 → 现货到货（扣 0.1%）→ 成本锚 C 回写台账行。

        C =（现金赔付 + 实际补买支出 + 费）÷（面值×张数）——与实盘
        ``refresh_cost_bases()`` 同一口径，也是卖 call 保本门的成本锚。
        """
        qty = t.lot_coin * t.sz
        spend = qty * px
        fee_usd = spend * self.spot_fee_rate
        got = qty * (1 - self.spot_fee_rate)
        cash -= spend
        self._spot_qty += got
        self._spot_spend += spend
        c = (t.payout_usd + spend + fee_usd) / qty if qty > 0 else None
        t.status = "done"
        t.settle_row["cover_status"] = "done"
        t.settle_row["cost_basis"] = round(c, 4) if c else None
        t.settle_row["cover_spend_usd"] = round(spend, 6)
        if c:
            self._cost_bases.append({"inst_id": t.inst_id, "sz": t.sz,
                                     "cost_basis": round(c, 4), "ts": str(ts)})
        fills.append({
            "ts": str(ts), "inst_id": t.inst_id, "side": "cover",
            "opt_type": "P", "sz": t.sz,
            "avg_px": round(px, 6), "qty": round(got, 8),
            "cost_usd": round(spend, 6), "fee_usd": round(fee_usd, 6),
            "payout_usd": round(t.payout_usd, 6),
            "cost_basis": round(c, 4) if c else None,
            "mode": t.mode,
            "spot": round(self.data.price_of() or 0.0, 4),
            "reason": (f"补买接货（{t.mode}）成本锚 C=({t.payout_usd:.4f}+"
                       f"{spend:.4f}+{fee_usd:.4f})/{qty:g}"
                       + (f"={c:.4f}" if c else "")),
        })
        return cash

    def _try_call_entry(self, ts, positions: list, fills: list, cash: float,
                        sig: Optional[dict] = None) -> float:
        """covered 卖 call 支线（C41b）—— 与实盘 ``evaluate_call_entry()`` 同源。

        担保是现货而非现金：容量 = 现货覆盖张数（**扣除在仓 short call**，
        §33.40.3 累计口径）——防「现货只够 1 张却分两笔各卖 1 张」。
        去重门（§33.43 Step 4b）在回测中退化为恒放行（模拟即时成交、无在途
        委托），门的正确性由单测 + 实盘实测覆盖。
        """
        if not self.call_enabled or self._spot_qty <= 0:
            return cash
        from nanobot_quant.okx_options_strategy import evaluate_call_entry
        from nanobot_quant.okx_options_trade import FAMILY_LOT, covered_sellable_sz

        base = self.family.split("-")[0]
        lot = float(FAMILY_LOT.get(base) or 0.0)
        if lot <= 0:
            self.skips.append(f"无 {base} 面值常量（FAMILY_LOT）→ 跳过卖 call")
            return cash
        spot = self.data.price_of()
        if spot <= 0:
            return cash
        op = self._opt_params()
        sellable = covered_sellable_sz(self._spot_qty, lot)
        calls = [p for p in positions if p.opt_type == "C"]
        cost_hint = max((c["cost_basis"] for c in self._cost_bases), default=None)
        covered = {"sellable_sz": sellable,
                   "spot_avail": round(self._spot_qty, 8),
                   "base": base, "lot_coin": lot, "cost_hint": cost_hint,
                   "cost_pending": False, "note": "回测模拟现货持仓"}
        chain = self.data.chain_dict_at(ts, opt_type="C", dsigma_pts=self.dsigma_pts,
                                        tick=self.tick, slippage=self.slippage)
        d, note = evaluate_call_entry(
            self.family, params=op, covered=covered,
            open_calls=len(calls), total_calls=len(calls),
            cost_basis=cost_hint, chain=chain, base_px=spot)
        if d is None:
            self.skips.append(note)
            return cash
        if not self._dedupe_note_logged:
            self._dedupe_note_logged = True
            self.notes.append(
                "卖 call 去重门（§33.43 Step 4b）在回测中退化为恒放行"
                "（模拟即时成交、不存在在途委托）——门的正确性由单测/实盘实测覆盖")
        sell_px = d.bid
        fee = self._option_fee(d.strike, lot, d.sz, premium_px=sell_px)
        cash += sell_px * lot * d.sz - fee
        positions.append(SimPosition(
            inst_id=d.inst_id, family=self.family, strike=d.strike,
            exp_ms=_exp_ms_of(d.inst_id, ts), sz=d.sz, entry_px=sell_px,
            entry_ts=ts, entry_reason=d.entry_reason or "covered",
            lot_coin=lot, opt_type="C"))
        fills.append({
            "ts": str(ts), "inst_id": d.inst_id, "side": "sell_open",
            "opt_type": "C", "sz": d.sz, "strike": d.strike,
            "avg_px": round(sell_px, 6), "fee_usd": round(fee, 6),
            "spot": round(spot, 4), "iv": d.iv, "delta": d.delta, "days": d.days,
            "net_yield_pct": d.net_yield_pct,
            "premium_usd": round(sell_px * lot * d.sz, 6),
            "reason": d.entry_reason or "covered call",
        })
        return cash

    # ── 止盈线 ──────────────────────────────────────────────

    def _resolve_tp_pct(self, op: dict) -> Optional[float]:
        """生效止盈线：显式传入优先，未传则跟随实盘策略参数（0/空 = 关闭）。

        ``evaluate_exits`` 对 falsy 的 ``tp_pct`` 直接返回空 —— 即「不止盈」。
        """
        if self.tp_pct is not None:
            try:
                return float(self.tp_pct)
            except (TypeError, ValueError):
                return None
        raw = op.get("take_profit_pct")
        if raw in (None, ""):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None

    def _tp_txt(self) -> str:
        """日志用：把「不止盈」显示成中文，别让 None 打成 ``None%``。"""
        return f"{self.tp_pct:g}%" if self.tp_pct else "关闭"

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
        # 止盈线：显式传入（CLI --tp / 页面覆盖）优先，未传则跟随实盘策略参数。
        # 此前 driver 的 ``tp_pct`` 默认 None 被直接透传，``evaluate_exits`` 对 falsy
        # 立即返回空 —— 全程零止盈买回、离场全靠到期，而启动行照打「止盈=50%」：
        # 日志与实际行为不一致（2026-09-25 复验：期末浮盈 99.91% 的持仓不被平仓）。
        self.tp_pct = self._resolve_tp_pct(op)
        self.tp_call_pct = self._resolve_tp_call_pct(op)
        ccfg = self._cover_cfg()
        cc = (f"补买=开（{ccfg['mode']} @结算价×(1−{ccfg['discount_pct']:g}%) · "
              f"超时 {ccfg['timeout_hours']:g}h→撤单+市价）" if self.cover_enabled
              else "补买=关（不计现货接货）")
        cl = (f"卖 call=开（止盈 {self.tp_call_pct:g}% · 上限 "
              f"{op.get('max_calls_per_family')}/{op.get('max_calls_total')}）"
              if self.call_enabled else "卖 call=关")
        self._log(f"资金链：{cc} · {cl} · 结算价口径=到期前 "
                  f"{self.settle_window_min} 分钟标的均价（回放近似）")
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
            f"IV闸门={op.get('iv_min_percentile')} "
            f"止盈={op.get('take_profit_pct')}%（生效={self._tp_txt()}）"
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
        if not getattr(self.data, "prepared", False):
            self.data.prefetch()          # 网格共享数据源：已预取就不重拉

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
            "tp_pct_call": self.tp_call_pct if self.call_enabled else None,
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
            sig = self._td_signal_at(ts)      # 每 bar 只算一次：put/call/补买共用
            cash = self._settle_expired(ts, positions, fills, cash)
            cash = self._tick_covers(ts, sig, fills, cash)
            cash = self._check_exits(ts, positions, fills, cash)
            cash = self._try_entry(ts, positions, fills, cash, sig=sig)
            cash = self._try_call_entry(ts, positions, fills, cash, sig=sig)

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
        # 一张 put，按「如现在全部买回」的口径应当**扣减** ``open_value``。
        # 接货现货是**资产**（资金链段的战利品），按最后 bar 现价计价加上去。
        spot_last = float(self.data.price_of() or 0.0)
        spot_value = self._spot_qty * spot_last
        if self._covers:
            self.notes.append(
                f"期末仍有 {len(self._covers)} 笔补买未完成"
                f"（区间尾部/超时未到）："
                + "、".join(f"{t.inst_id}({t.mode})" for t in self._covers[:3]))
        net = cash - open_value + spot_value
        out["fills"] = fills
        out["final_positions"] = open_rows
        out["cash"] = round(cash, 4)          # 期末现金（净值 = 现金 − 持仓负债 + 现货市值）
        out["spot"] = {
            "qty": round(self._spot_qty, 8),
            "px": round(spot_last, 4),
            "value_usd": round(spot_value, 4),
            "spend_usd": round(self._spot_spend, 4),
            "covers_done": len(self._cost_bases),
            "covers_pending": len(self._covers),
            "cost_bases": list(self._cost_bases),
        }
        out["chain"] = {
            "cover_enabled": self.cover_enabled,
            "cover_mode": ccfg["mode"] if self.cover_enabled else None,
            "cover_discount_pct": ccfg["discount_pct"] if self.cover_enabled else None,
            "cover_timeout_hours": ccfg["timeout_hours"] if self.cover_enabled else None,
            "call_enabled": self.call_enabled,
            "tp_call_pct": self.tp_call_pct if self.call_enabled else None,
            "settle_window_min": self.settle_window_min,
        }
        out["skips"] = _count_skips(self.skips)
        # 生效张数上限（页面/日志同源）—— 「填了 10 却只开 3」这类闷棍靠它显形
        out["max_contracts"] = {
            "effective": self._effective_cap,
            "per_family": op.get("max_contracts_per_family"),
            "total": op.get("max_contracts_total"),
        }
        # ── 账目四件套（与净值闭合）────────────────────────────
        # 毛权利金 = 全部开仓收到的；买回支出 = 止盈买回的付出；赔付 = 到期被
        # 行权赔付；手续费 = 全部成交的手续费。净交易损益 = 四者轧差 ⇒
        # 期末净值 = 初始 + 净交易损益 − 期末未平仓市值。
        premium_gross = sum(f.get("premium_usd") or 0 for f in fills
                            if f.get("side") == "sell_open")
        premium_put = sum(f.get("premium_usd") or 0 for f in fills
                          if f.get("side") == "sell_open"
                          and (f.get("opt_type") or "P") != "C")
        premium_call = sum(f.get("premium_usd") or 0 for f in fills
                           if f.get("side") == "sell_open"
                           and f.get("opt_type") == "C")
        buyback = sum(f.get("close_cost_usd") or 0 for f in fills
                      if f.get("side") == "close")
        payout_total = sum(f.get("payout_usd") or 0 for f in fills
                           if f.get("side") in ("settle_itm", "settle_otm"))
        payout_call = sum(f.get("payout_usd") or 0 for f in fills
                          if f.get("side") == "settle_itm"
                          and f.get("opt_type") == "C")
        cover_spend = sum(f.get("cost_usd") or 0 for f in fills
                          if f.get("side") == "cover")
        cover_qty = sum(f.get("qty") or 0 for f in fills
                        if f.get("side") == "cover")
        fees_total = sum(f.get("fee_usd") or 0 for f in fills)
        cbs = [c["cost_basis"] for c in self._cost_bases]
        out["kpi"] = {
            "final_net_usd": round(net, 4),
            "roi_pct": round((net - self.initial_cash) / self.initial_cash * 100, 4),
            "premium_income_usd": round(premium_gross, 4),
            "premium_put_usd": round(premium_put, 4),
            "premium_call_usd": round(premium_call, 4),
            "buyback_cost_usd": round(buyback, 4),
            "payout_usd": round(payout_total, 4),
            "payout_call_usd": round(payout_call, 4),
            "cover_spend_usd": round(cover_spend, 4),
            "cover_qty": round(cover_qty, 8),
            "spot_qty": round(self._spot_qty, 8),
            "spot_value_usd": round(spot_value, 4),
            "cost_basis_avg": round(sum(cbs) / len(cbs), 4) if cbs else None,
            "cost_basis_max": round(max(cbs), 4) if cbs else None,
            "fees_usd": round(fees_total, 4),
            "net_trading_usd": round(premium_gross - buyback - payout_total - fees_total, 4),
            "open_premium_usd": round(open_premium, 4),
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
            f"ROI={k['roi_pct']}% 毛权利金={k['premium_income_usd']}"
            f"（put {k['premium_put_usd']} / call {k['premium_call_usd']}） "
            f"买回={k['buyback_cost_usd']} 赔付={k['payout_usd']} "
            f"补买支出={k['cover_spend_usd']}（{k['cover_qty']:g} 币） "
            f"现货={k['spot_value_usd']} 手续费={k['fees_usd']} "
            f"净交易={k['net_trading_usd']}"
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
    ap.add_argument("--cover-off", action="store_true",
                    help="关闭补买建模（不计现货接货 → covered 卖 call 也无容量）")
    ap.add_argument("--cover-mode", default=None,
                    choices=["limit", "signal", "immediate"],
                    help="补买模式（缺省跟随 opt_params.cover）")
    ap.add_argument("--cover-discount", type=float, default=None,
                    help="限价折让%%（缺省 1）")
    ap.add_argument("--cover-timeout", type=float, default=None,
                    help="超时兜底小时（缺省 24）")
    ap.add_argument("--call-off", action="store_true", help="关闭 covered 卖 call 支线")
    ap.add_argument("--tp-call", type=float, default=None, help="卖 call 止盈%%（缺省 30）")
    ap.add_argument("--settle-window", type=int, default=DEFAULT_SETTLE_WINDOW_MIN,
                    help="结算价窗口分钟（官方口径 30）")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    drv = OptionsBacktestDriver(
        a.family, timestep=a.timestep, end_ts=int(time.time()),
        start_ts=int(time.time()) - a.days * 86400,
        td_bars=a.td_bars, slippage_pct=a.slippage, tp_pct=a.tp,
        initial_cash=a.cash, dsigma_pts=a.dsigma, tick=a.tick,
        cover_enabled=not a.cover_off, cover_mode=a.cover_mode,
        cover_discount_pct=a.cover_discount, cover_timeout_hours=a.cover_timeout,
        call_enabled=not a.call_off, tp_call_pct=a.tp_call,
        settle_window_min=a.settle_window)
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
