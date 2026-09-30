"""期权卖 put 策略（lumibot ``Strategy``）—— E 期接线步 2，方案 B。

与 :class:`TdSequentialStrategy` 同构：节拍交给 lumibot 引擎
（``StrategyExecutor.run()``），策略只负责「一轮里做什么」。期权机制层
（daemon / 启停 / 优雅停止）复用 :class:`LiveRunnerBase`，本类只填决策。

每轮 ``on_trading_iteration``：

  ① **到期判定**：``ot.settle_expired_puts()``（OTM 自动关账 / ITM 记赔付 /
     矛盾挂 settled_review fail-closed / 账单未出保持 open 下轮重试）。
  ② **入场**：卖 put 线（逐家族）标的 TD 信号（OKX 现货 K 线 + 原版 TD 引擎）→
     IV 环境闸门 → 张数上限 → ``select_puts`` 选档；卖 call 线（``call_enabled``）
     covered 容量 → 成本锚 C → 张数上限 → ``select_calls`` 选档（**无信号择时**）。
  ③ **止盈**：持仓权利金回落 ≥ 止盈线 → 买回平仓（put 线 50% / call 线 30%，各自参数）。
  ④ 状态写入 ``okx_options_live_state`` + 事件文件落盘。

``dry_run=True``（默认）时**只记录决策、不下单** —— 「AI 不能自行授权实盘」
在闭环里的结构性落实；需用户在期权页手动取消勾选才会真实下单。

停止（与 td_live 同构，2026-09-25 修正）：**不调 ``executor.stop()``**
（lumibot 内部 ``shutdown(wait=True)`` 会等业务轮收尾，遇网络卡死即永久挂住
—— TD live 已踩过）。真正的退出通道是 runner 侧 ``executor.stop_event``
（lumibot 主循环据此 break → ``executor.run()`` 返回），策略侧只做两件事：
``_track_iteration`` 维护 ``self._iteration_active``（runner 据此等当前轮
自然结束）+ 看到 ``stop_requested`` 时**直接 return**。

**不可用 ``raise SystemExit`` 退出**：异常会被 APScheduler 的 job 层捕获
并记日志、主循环照跑（2026-09-25 实测：「循环已停止(thread_alive=True)」
+ 每 60s 空转抛一次 traceback，且 ``start()`` 见 alive=True 直接「已在运行」
⇒ 停一次就再也起不来，只能重启空间）。
"""

from __future__ import annotations

import functools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lumibot.strategies.strategy import Strategy

from .. import event_log
from .. import okx_options_trade as ot
from .. import okx_options_strategy as st
from .. import okx_options_live_state as lst

# 事件文件与期权台账同目录（append-only JSONL，跨重启保留）
_EVENTS_NAME = "okx_options_live_events.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 「卖出 / 买回」计数只认**动作**记录（2026-09-25 实测修正）：
#   entries/call_entries 里混着 no_action / cycle_wait / no_signal 这类
#   「本轮无动作」说明记录，按条数计数会把 fail-closed 跳过记成「卖call 1」
#   （每轮无动作都 +1）。failed 也不计入（单独用「失败 N」显式提示）。
_ENTRY_ACTION_STATUSES = frozenset({"sold", "dry_run(would_sell)"})
_EXIT_ACTION_STATUSES = frozenset({"bought_back", "dry_run(would_buy_back)"})
_FAILED_STATUSES = frozenset({"failed"})
# 「交易所已受理、未定案」——不是成交也不是失败，单独计（仪表盘显「在途 N」）：
# 台账已留 pending 行 + 去重门拦住重复提交，下一轮复检定案。
_PENDING_STATUSES = frozenset({"pending"})
# lumibot Order.custom_params["opt_status"] 中的在途状态（broker 透传）
_IN_FLIGHT_ORDER_STATUSES = frozenset({
    "pending", "unknown", "live", "open", "partially_filled", "submitted",
})

# 提交三档 → 记录 status（2026-09-29 定稿；见 _submit_option）
_SUBMIT_STATE_ENTRY = {"filled": "sold", "pending": "pending", "failed": "failed"}
_SUBMIT_STATE_EXIT = {"filled": "bought_back", "pending": "pending",
                     "failed": "failed"}


def _count_status(rows: Any, statuses: frozenset) -> int:
    """按 status 计数（忽略非 dict / 无 status 的记录）。"""
    return sum(1 for r in (rows or [])
               if isinstance(r, dict) and r.get("status") in statuses)


class OkxOptionsPutStrategy(Strategy):
    """卖 put + 权利金回落止盈（U 本位线性期权，USDSⓈ-M 家族）。"""

    # lumibot 的类级默认参数（``_build_executor`` 会整体覆盖）
    parameters: dict = {
        "account": "",
        "families": ["SOL-USD_UM"],
        "td_period": "5m",
        "td_bars": 120,
        "entry_setup": 9,
        "entry_countdown": 13,
        "iv_min_percentile": 0,
        "take_profit_pct": 50,
        "max_contracts_per_family": 1,
        "max_contracts_total": 3,
        # ── 卖 call（covered call）支线 —— 与 put 参数互不影响（§33.40）──
        "put_enabled": True,             # put 线总开关（默认开；便于只跑 call 线验证）
        "call_enabled": False,           # call 线总开关（默认关，用户在页面手动开）
        "take_profit_pct_call": 30,      # call 止盈线（上行无界、快落袋）
        "max_calls_per_family": 1,        # 单家族在仓 call 张数上限
        "max_calls_total": 2,             # 全局在仓 call 张数上限
        "allow_no_cost_basis": False,     # 无成本锚 C 时是否放行（台账标「无成本锚」）
        "dry_run": True,
        "live_mode": False,        # 事件文件写入开关（回测置 False 不污染实盘监控）
        # 停止位（见模块 docstring）：由 runner 置位，策略看到后只 return
        "stop_requested": False,
    }

    # ══════════════════════ 生命周期 ══════════════════════

    def initialize(self, **kwargs) -> None:
        """不作账户切换、不预拉数据 —— 全部延迟到轮次内（失败也不阻塞启动）。"""
        self._iteration_active = False   # 当前业务轮是否在跑（runner 停止路径据此等轮）
        self._log("策略已初始化（卖 put / 卖 call + 权利金回落止盈）")

    def before_market_opens(self) -> None:  # pragma: no cover - 期权 7×24，无需
        pass

    # ══════════════════════ 主轮次 ══════════════════════

    def _track_iteration(fn):
        """包一轮业务（与 ``TdSequentialStrategy._track_iteration`` 同款）。

        ① ``stop_requested`` 置位 → **直接 return**（不是抛异常：异常会被
           APScheduler 的 job 层捕获记日志、主循环照跑 —— 2026-09-25 实测踩过）；
        ② 运行期 ``self._iteration_active`` 为 True（finally 复位）——runner 的
           停止路径据此等当前轮自然结束，**绝不强行中断正在执行的一轮**。
        """
        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            if self.parameters.get("stop_requested"):
                self._log("收到停止请求 —— 本轮不再执行（循环即将退出）")
                return None
            self._iteration_active = True
            try:
                return fn(self, *a, **kw)
            finally:
                self._iteration_active = False
        return wrapper

    @_track_iteration
    def on_trading_iteration(self) -> None:
        """一轮：到期判定 → 入场 → 止盈 → 状态落盘。异常不外抛给引擎。"""
        p = self.parameters
        account = str(p.get("account") or "")
        dry = bool(p.get("dry_run", True))
        self._log("── 巡检轮次开始 ──")

        settled = self._settle_expired()
        # 未定案复检（IOC 未成交 / 未定案的行）——先落定再决策，避免重复下单
        self._resolve_pending(account)

        positions = self._positions(account)
        if positions is None:
            self._log("⚠️ 持仓查询失败 —— 本轮跳过策略（fail-closed）")
            self._finish(settled, None, "持仓查询失败")
            return

        counts = st.contracts_by_family(positions, opt_type="P")
        total = sum(counts.values())
        call_counts = st.contracts_by_family(positions, opt_type="C")
        call_total = sum(call_counts.values())
        self._log(f"当前持仓 | 在仓 put {total} 张 分家族={counts or '{}'} · "
                  f"在仓 call {call_total} 张 分家族={call_counts or '{}'} "
                  f"明细={[(x.get('inst_id'), x.get('pos')) for x in positions]}")

        covers = self._auto_cover(account, p, dry)
        entries = self._entries(account, p, dry, counts, total, positions)
        call_entries = self._call_entries(account, p, dry, positions,
                                          call_counts, call_total)
        exits = self._exits(account, p, dry, positions, opt_type="P")
        call_exits = self._exits(account, p, dry, positions, opt_type="C")

        self._finish(settled, {"entries": entries, "exits": exits,
                               "call_entries": call_entries,
                               "call_exits": call_exits,
                               "covers": covers,
                               # dry_run 必须随快照下发：缺失时页面按「dry=true」渲染
                               # （2026-09-29 实测：真实下单的轮次被标成「仅记录」）
                               "dry_run": dry,
                               "counts": counts, "call_counts": call_counts},
                     None)

    def _resolve_pending(self, account: str) -> list[dict]:
        """复检未定案台账行（IOC 未成交 / 未定案）——每轮开头一次。

    2026-09-29 实测：买回 IOC 未成交时返回内存 pending 状态，被上层当成成功
    （页面显示「已买回」而持仓未动），每 60s 重复提交同一合约。本方法把状态
    按交易所结果落定（filled → 完成回填/关账；cancelled → 标未成交），
    与 ``has_pending_ledger`` 去重门配合：先复检、再决策。
    """
        try:
            rows = ot.resolve_pending(account)
        except Exception as e:  # noqa: BLE001 —— 复检失败不杀轮
            self._log(f"⚠️ 未定案复检失败：{type(e).__name__}: {e}")
            return []
        for r in rows:
            kind = "买回" if str(r.get("kind") or "").startswith("close") else "卖出"
            extra = ""
            if r.get("filled_px"):
                extra += f" 成交价={r['filled_px']}"
            if r.get("ref_ask"):
                extra += f" ref_ask={r['ref_ask']}"
            if r.get("error"):
                extra += f" 错误={r['error']}"
            self._log(f"复检 | {kind} {r.get('inst_id')} → 状态={r.get('status')}{extra}")
        return rows

    # ══════════════════════ ①' 自动补买（接货闭环）══════════════════════

    def _auto_cover(self, account: str, p: dict, dry: bool) -> list[dict]:
        """自动补买（§33.43 Step 1）：卖 put 被判 ITM 后，按 ``cover.mode`` 推进接货。

        受 option_params.json 的 ``cover.auto`` 控制（**默认关**，用户页面手动开）；
        dry-run 只记意图（不写台账状态、不下单）。数量机械 = 台账行 sz×面值；
        价格/时机由纯函数 ``evaluate_cover`` 决定（limit 默认 / signal 等衰竭 /
        immediate 判定即市价），所有模式共有超时兜底 T。异常不得杀策略轮。
        """
        try:
            cp = ot.cover_params()
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ 补买参数读取失败：{type(e).__name__}: {e}")
            return []
        if not cp.get("auto"):
            return []
        sig_fn = None
        if cp.get("mode") == "signal":
            def _cover_sig(base):
                """与 put 入场同源：同周期、同源 K 线的 TD 衰竭信号。"""
                return self._td_signal(f"{base}-USD_UM", base, p)

            sig_fn = _cover_sig
        try:
            res = ot.auto_cover_pending(
                account, dry_run=dry, signal_fn=sig_fn,
                entry_setup=int(p.get("entry_setup") or 9),
                entry_countdown=int(p.get("entry_countdown") or 13)) or []
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ 自动补买异常：{type(e).__name__}: {e}")
            return []
        for r in res:
            act = str(r.get("action") or "")
            head = {"market": "市价补买", "limit": "挂限价补买", "filled": "补买成交",
                    "wait_signal": "等衰竭信号", "skip": "跳过",
                    "error": "异常"}.get(act, act)
            tgt = r.get("target_px")
            self._log(f"COVER {head} | {r.get('inst_id')} · {r.get('qty') or 0:g} 币"
                      + (f" · 目标价 {float(tgt):g}" if tgt else "")
                      + f" · mode={r.get('mode')} · {r.get('reason') or ''}")
            self._record({"type": "cover", "id": r.get("id"),
                          "inst_id": r.get("inst_id"), "account": account,
                          "action": act, "mode": r.get("mode"), "qty": r.get("qty"),
                          "target_px": r.get("target_px"), "px": r.get("px"),
                          "ord_id": r.get("ord_id"), "status": r.get("status"),
                          "reason": r.get("reason") or "", "dry_run": bool(dry)})
        return res

    # ══════════════════════ ① 到期判定 ══════════════════════

    def _settle_expired(self) -> list[dict]:
        try:
            settled = ot.settle_expired_puts() or []
        except Exception as e:  # noqa: BLE001 —— 巡检自愈：异常不得杀策略轮
            self._log(f"⚠️ 到期判定异常：{type(e).__name__}: {e}")
            return []
        if not settled:
            self._log("到期判定：本轮无新判定（未到期或账单未出）")
            return []
        by_id = {}
        try:
            by_id = {r.get("id"): r for r in ot.load_ledger()}
        except Exception:  # noqa: BLE001
            pass
        for s in settled:
            row = by_id.get(s.get("id")) or {}
            self._log(f"到期判定：{s.get('inst_id')} → {s.get('status')} "
                      f"结算价={s.get('settle_px')} 净盈亏={s.get('settle_pnl')} "
                      f"毛赔付={s.get('settle_payout')} | {s.get('note', '')}")
            self._record({
                "type": "settle",
                "id": s.get("id"),
                "inst_id": s.get("inst_id"),
                "account": row.get("account") or "",
                "strike": row.get("strike"),
                "sz": row.get("sz"),
                "exp_ms": row.get("exp_ms"),
                "status": s.get("status"),
                "opt_type": s.get("opt_type") or
                ("C" if str(s.get("inst_id") or "").endswith("-C") else "P"),
                "settle_px": s.get("settle_px"),
                "settle_pnl": s.get("settle_pnl"),
                "settle_payout": s.get("settle_payout"),
                "note": s.get("note", ""),
            })
        self._bump("settled", len(settled))
        return settled

    # ══════════════════════ ② 入场 ══════════════════════

    # ══════════════════ ⓪ 信号周期门控 ══════════════════

    def _cycle_gate(self, family: str, sig: dict, p: dict,
                    positions: list) -> "str | None":
        """信号周期门控 —— 逻辑在 ``okx_options_strategy.cycle_gate()``。

        抽成纯函数是为了**实盘与回测共用同一份决策代码**：回测 driver 直接
        调决策函数、不经过本策略类，门控写在类里回测就看不到。
        """
        if not hasattr(self, "_cycle_state"):
            self._cycle_state: dict[str, dict] = {}
        has_pos = any(str(x.get("family") or "") == family
                      for x in (positions or []))
        return st.cycle_gate(self._cycle_state, family, td_signal=sig,
                             params=p, has_position=has_pos)

    def _cycle_mark_bought(self, family: str, sig: dict) -> None:
        """建仓（含 dry-run 意图）后置位 —— 本周期内不再开仓。"""
        st.cycle_mark_bought(getattr(self, "_cycle_state", None) or {}, family)

    def _entries(self, account: str, p: dict, dry: bool,
                 counts: dict, total: int, positions: list) -> list[dict]:
        """卖 put 支线（TD 衰竭信号驱动）。"""
        if not p.get("put_enabled", True):
            self._log("PUT 线已关闭（put_enabled=false）—— 跳过卖 put 支线")
            return []
        out: list[dict] = []
        # 现金担保前置门（§33.43 Step 3）：先取子账号 USDC 可用额；查询失败用 -1.0
        # 哨兵强制拦截 —— 宁可本轮跳过建仓，也不在无法核对现金时按「够」假设放行。
        try:
            cash = ot.usdc_avail(account)
            self._log(f"PUT 现金担保：可用 USDC ${cash:.2f}（每张需 strike×面值 全损担保）")
        except Exception as e:  # noqa: BLE001
            cash = -1.0
            self._log(f"PUT 现金担保查询失败：{type(e).__name__}: {e} "
                      f"→ 本轮跳过卖 put（fail-closed）")
        for family in (p.get("families") or []):
            base = str(family).split("-")[0]
            sig = self._td_signal(family, base, p)
            if sig is None:
                out.append({"family": base, "status": "no_signal",
                            "note": "无 K 线数据"})
                continue
            self._log(f"PUT {base} TD | setup_buy={sig.get('setup_buy')}/{p.get('entry_setup')} "
                      f"cd_buy={sig.get('cd_buy')}/{p.get('entry_countdown')} "
                      f"setup_sell={sig.get('setup_sell')} cd_sell={sig.get('cd_sell')} "
                      f"price={sig.get('price')} rec={sig.get('recommendation')}")

            gate = self._cycle_gate(family, sig, p, positions)
            if gate:
                self._log(f"PUT {base} → 周期门控拦截：{gate}")
                out.append({"family": base, "status": "cycle_wait", "note": gate})
                continue

            dec, note = st.evaluate_entry(
                family, td_signal=sig, params=p,
                open_contracts=counts.get(base, 0), total_contracts=total,
                cash_avail=cash)
            if dec is None:
                self._log(f"PUT {base} → 无动作：{note}")
                out.append({"family": base, "status": "no_action", "note": note})
                continue

            rec = {**dec.to_event(), "dry_run": dry}
            if dry:
                rec["status"] = "dry_run(would_sell)"
                self._log(f"PUT {base} → 【dry-run】卖出 {dec.inst_id} ×{dec.sz} "
                          f"| 盘口 bid={rec.get('bid')} "
                          f"净收益率={rec.get('net_yield_pct')}% "
                          f"年化={rec.get('apr_pct')}% 担保=${rec.get('notional_usd')} "
                          f"IV={rec.get('iv')} delta={rec.get('delta')} "
                          f"天数={rec.get('days')} | 理由={dec.entry_reason}")
            else:
                # 去重门：同合约已有未定案开仓台账行 → 不重复提交（IOC 未成交时
                # 交易所在途查不到，必须靠台账行拦住重复下单）
                if ot.has_pending_ledger(dec.inst_id, ("open_put",)):
                    rec["status"] = "pending_confirm"
                    rec["note"] = f"已有未定案卖出单（{dec.inst_id}）——本轮不重复提交，等复检"
                    self._log(f"⏳ PUT {base} 卖出跳过 {dec.inst_id} ×{dec.sz}：{rec['note']}")
                else:
                    state, err = self._submit_option(dec, p)
                    rec["status"] = _SUBMIT_STATE_ENTRY.get(state, "failed")
                    if state == "filled":
                        counts[base] = counts.get(base, 0) + dec.sz
                        total += dec.sz
                        self._log(f"PUT {base} → ✅ 卖出成交 {dec.inst_id} ×{dec.sz}")
                    elif state == "pending":
                        # 在途：交易所已受理、未定案 —— 不算卖出（不报成交），
                        # 但照旧占用本周期（防重复提交），台账 pending 行 + 复检接管。
                        counts[base] = counts.get(base, 0) + dec.sz
                        total += dec.sz
                        rec["error"] = err
                        self._log(f"⏳ PUT {base} 挂单在途 {dec.inst_id} ×{dec.sz}：{err}"
                                  "（台账 pending 行接管，下一轮复检定案）")
                    else:
                        rec["error"] = err
                        self._log(f"⚠️ PUT {base} 卖出失败 {dec.inst_id} ×{dec.sz}：{err}")
            out.append(rec)
            # 建仓（含 dry-run 意图 / 在途未定案）即置位 —— 之后同周期不再开仓。
            # 例外：**提交失败不置位**（交易所在所无单），否则一次薄盘口 IOC
            # 未成交就会白白吃掉整轮信号周期（2026-09-29 实测：假成功后周期
            # 门控立刻拦住后续所有尝试）。
            if rec.get("status") != "failed":
                self._cycle_mark_bought(family, sig)
            self._record({"type": "entry", **rec})
        return out

    def _call_entries(self, account: str, p: dict, dry: bool, positions: list,
                      call_counts: dict, call_total: int) -> list[dict]:
        """卖 call（covered call）支线 —— 无信号择时：有 covered 余量就卖。

        决策全在 ``okx_options_strategy.evaluate_call_entry()``（纯函数，回测可复用）；
        本方法只负责取 ``covered_context``、下单与日志/事件。
        """
        if not p.get("call_enabled"):
            return []
        out: list[dict] = []
        for family in (p.get("families") or []):
            base = str(family).split("-")[0]
            try:
                cov = ot.covered_context(account, family)
            except Exception as e:  # noqa: BLE001 —— 现货查不到就不卖（fail-closed）
                note = f"现货查询失败：{type(e).__name__}: {e}"
                self._log(f"CALL {base} → 无动作：{note}（fail-closed）")
                out.append({"family": base, "opt_type": "C",
                            "status": "no_action", "note": note})
                continue
            dec, note = st.evaluate_call_entry(
                family, params=p, covered=cov,
                open_calls=call_counts.get(base, 0), total_calls=call_total)
            if dec is None:
                self._log(f"CALL {base} → 无动作：{note}")
                out.append({"family": base, "opt_type": "C",
                            "status": "no_action", "note": note})
                continue

            # 去重门（§33.43 Step 4b）：同合约已有在途委托 → 不重复提交
            # （重复卖出 = 超额 short；covered 门扣的「在仓 call」只管已成交部分）
            # 叠加台账未定案行判定（2026-09-29）：IOC 未成交时交易所在途查不到，
            # 只能靠本地 pending 行拦住重复下单。
            if (ot.has_pending_inst(account, family, dec.inst_id)
                    or ot.has_pending_ledger(dec.inst_id, ("open_call",))):
                note = f"已有在途委托（{dec.inst_id}）→ 跳过（去重，fail-closed）"
                self._log(f"CALL {base} → 无动作：{note}")
                out.append({"family": base, "opt_type": "C",
                            "status": "no_action", "note": note})
                continue

            rec = {**dec.to_event(), "dry_run": dry}
            if dry:
                rec["status"] = "dry_run(would_sell)"
                self._log(f"CALL {base} → 【dry-run】卖出 {dec.inst_id} ×{dec.sz} "
                          f"| 盘口 bid={rec.get('bid')} "
                          f"净收益率={rec.get('net_yield_pct')}%（分母=现货市值） "
                          f"delta={rec.get('delta')} 天数={rec.get('days')} "
                          f"成本锚 C={rec.get('cost_basis')} | 理由={dec.entry_reason}")
            else:
                state, err = self._submit_option(dec, p)
                rec["status"] = _SUBMIT_STATE_ENTRY.get(state, "failed")
                if state == "filled":
                    call_counts[base] = call_counts.get(base, 0) + dec.sz
                    call_total += dec.sz
                    self._log(f"CALL {base} → ✅ 卖出成交 {dec.inst_id} ×{dec.sz}")
                elif state == "pending":
                    call_counts[base] = call_counts.get(base, 0) + dec.sz
                    call_total += dec.sz
                    rec["error"] = err
                    self._log(f"⏳ CALL {base} 挂单在途 {dec.inst_id} ×{dec.sz}：{err}"
                              "（下一轮复检定案）")
                else:
                    rec["error"] = err
                    self._log(f"⚠️ CALL {base} 卖出失败 {dec.inst_id} ×{dec.sz}：{err}")
            out.append(rec)
            self._record({"type": "entry", **rec})
        return out

    # ══════════════════════ ③ 止盈 ══════════════════════

    def _exits(self, account: str, p: dict, dry: bool, positions,
               opt_type: str = "P") -> list[dict]:
        """止盈买回巡检（按方向分线：put 用 ``take_profit_pct``、call 用 ``take_profit_pct_call``）。"""
        is_call = opt_type == "C"
        key = "take_profit_pct_call" if is_call else "take_profit_pct"
        tag = "CALL" if is_call else "PUT"
        default_tp = st.DEFAULT_TP_PCT_CALL if is_call else st.DEFAULT_TP_PCT
        try:
            raw = p.get(key)
            tp = float(default_tp if raw is None else raw)
        except (TypeError, ValueError):
            tp = float(default_tp)
        try:
            rows = st.evaluate_exits(positions, tp_pct=tp, opt_type=opt_type)
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ {tag} 止盈评估异常：{type(e).__name__}: {e}")
            return []
        out: list[dict] = []
        for x in rows:
            rec = {**x.to_event(), "dry_run": dry, "opt_type": opt_type}
            if x.reason == "expired":
                # 已到期：交易所在该合约上已停止接单（IOC 必拒）——不提交、但显式可见
                rec["status"] = "skipped_expired"
                rec["note"] = "已到期，不提交买回（等台账到期判定闭回）"
                self._log(f"⏳ {tag} 已到期 {x.inst_id} ×{x.sz} → 不提交买回（等结算判定）")
            elif dry:
                rec["status"] = "dry_run(would_buy_back)"
                self._log(f"{tag} → 【dry-run】买回 {x.inst_id} ×{x.sz} | "
                          f"开仓 {rec.get('entry_px')} → 现价 {rec.get('mark_px')} "
                          f"（回落 {rec.get('drop_pct')} ≥ 止盈线 {tp}%）")
            else:
                # 去重门（2026-09-29）：同合约已有未定案台账行（IOC 未成交但已
                # 留下 pending 行）或交易所在途委托 → 不重复提交，先等复检
                if ot.has_pending_ledger(x.inst_id, ("close_put", "close_call")):
                    rec["status"] = "pending_confirm"
                    rec["note"] = f"已有未定案买回单（{x.inst_id}）——本轮不重复提交，等复检"
                    self._log(f"⏳ {tag} 买回跳过 {x.inst_id} ×{x.sz}：{rec['note']}")
                else:
                    state, err = self._submit_option(x, p, closing=True)
                    rec["status"] = _SUBMIT_STATE_EXIT.get(state, "failed")
                    if state == "filled":
                        self._log(f"{tag} → ✅ 买回成交 {x.inst_id} ×{x.sz}")
                    elif state == "pending":
                        rec["error"] = err
                        self._log(f"⏳ {tag} 买回在途 {x.inst_id} ×{x.sz}：{err}"
                                  "（下一轮复检定案）")
                    else:
                        rec["error"] = err
                        self._log(f"⚠️ {tag} 买回失败 {x.inst_id} ×{x.sz}：{err}")
            out.append(rec)
            self._record({"type": "exit", **rec})
        if not rows and positions:
            n = sum(1 for x in (positions or [])
                    if str(x.get("inst_id") or "").endswith("-" + opt_type))
            if n:
                self._log(f"{tag} 止盈巡检 | {n} 张在仓，均未达回落 {tp}% 门槛，继续持有")
        return out

    # ══════════════════════ 下单（经 lumibot broker 抽象）══════════════════════

    def _submit_option(self, dec, p: dict, closing: bool = False):
        """决策 → lumibot ``create_order`` → ``OkxOptionsBroker._submit_order``。

        卖开：side="sell"（broker 按 ``right`` 分派 open_put/open_call）；
        买回：side="buy"（走 close_*）。

        返回 **三档** ``(state, note)``（2026-09-29 定稿）：

        * ``"filled"``  —— 已成交（note 为 None）；
        * ``"pending"`` —— 交易所已受理、未定案（IOC 在途 / 限价未成交 / 状态
          查不清）：**不是失败**，台账 pending 行接管，下一轮复检定案；
        * ``"failed"``  —— 本轮无单（撤销 / 拒单 / 异常）：不占本周期，下轮可重试。
        """
        try:
            asset = self._asset_for(dec)
            side = "buy" if closing else "sell"
            # lumibot v4.5.78 签名 = create_order(asset, quantity, side, ...)：
            # 参数写反（asset, side, sz）会让 quantity="sell"、side=1，
            # Order.__init__ 的 `quantity < 0` 直接 TypeError（与现货线
            # td_sequential_strategy/portfolio.engine 保持同一写法）。
            order = self.create_order(asset, int(dec.sz), side)
            if order is None:
                return "failed", "create_order 返回 None"
            # 卖 call 的成本锚 C 随订单下传（covered call 保本门）：lumibot 的
            # ``create_order`` 不接收自定义 kwarg（会被静默丢弃 —— 与 data_source
            # 同坑），只能经 ``order.custom_params`` 传；broker 侧按 **order** 读。
            # 2026-09-30 实测：断在这里 → broker 拿不到 C → 自动卖 call 每轮被
            # 保本门 fail-closed（页面手动卖 call 显式传 C，不受影响）。
            cb = getattr(dec, "cost_basis", None)
            if cb and not closing:
                order.custom_params = getattr(order, "custom_params", None) or {}
                order.custom_params["cost_basis"] = float(cb)
            # ★ lumibot v4.5.78 的 ``Strategy.create_order`` **只创建 Order 对象、
            # 不提交**（docstring: "Once created, an order must still be submitted."）
            # —— 漏掉 submit_order 会让整条期权线变「假成功」：日志报「已提交卖出」
            # 但交易所在所无单、无持仓、台账无行（2026-09-29 13:31 实测：
            # 卖 SOL-USD_UM-261002-114-P ×1 报成功，OKX 无委托/无仓位、
            # frozen=0、台账无新行）。现货线一直显式 `submit_order`，故只有期权线中招。
            self.submit_order(order)
            cp = getattr(order, "custom_params", None) or {}
            raw = str(cp.get("opt_status") or "")
            err = getattr(order, "error", None) or getattr(order, "_error", None)
            if not err:
                return "filled", None
            # 在途（broker 明确标记 live/open/pending/unknown）≠ 失败：
            # 当作失败会让本周期不占位 → 下轮重复提交（实测 2026-09-29 14:05：
            # IOC 在 5s 窗口报 live、随后成交，却被记成「卖出失败」）。
            if raw in _IN_FLIGHT_ORDER_STATUSES:
                return "pending", str(err)
            return "failed", str(err)
        except Exception as e:  # noqa: BLE001 —— 失败必须可见，不静默
            return "failed", f"{type(e).__name__}: {e}"

    def _asset_for(self, dec):
        """决策里的 inst_id → lumibot 期权 Asset（instId 解析在 assets 模块）。"""
        from ..okx_options_assets import inst_to_asset
        return inst_to_asset(dec.inst_id)

    # ══════════════════════ 数据 / 状态 ══════════════════════

    def _td_signal(self, family: str, base: str, p: dict) -> Optional[dict]:
        """标的 TD 信号 —— OKX K 线（与期权执行同源），原版 TD 引擎。"""
        from ..okx_options_data import td_kline
        from ..strategies.td_sequential import calculate

        try:
            df, err = td_kline(family, period=str(p.get("td_period") or "5m"),
                               bars=int(p.get("td_bars") or 120))
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ {base}: K 线取数失败 {type(e).__name__}: {e}")
            return None
        if df is None or len(df) == 0:
            if err:
                self._log(f"⚠️ {base}: 标的 K 线不可用 —— {err}")
            return None
        try:
            return calculate(df)
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ {base}: TD 计算失败 {type(e).__name__}: {e}")
            return None

    def _positions(self, account: str):
        try:
            return ot.open_option_positions(account) or []
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ 持仓查询异常：{type(e).__name__}: {e}")
            return None

    def _bump(self, key: str, n: int = 1) -> None:
        """累计计数（写进程内状态，页面读）。"""
        try:
            lst.bump(key, n)
        except Exception:  # noqa: BLE001
            pass

    def _finish(self, settled, strat, error) -> None:
        """轮次收尾：写 LIVE_STATE + 结束日志（字段名与接线前保持一致）。"""
        rows = strat or {}
        # 只计**动作**记录：no_action / cycle_wait / no_signal 是「本轮无动作」的
        # 说明记录，不是卖出（否则每轮无动作都 +1 —— 2026-09-25 实测：call 线
        # fail-closed 跳过却记成「卖call 1」）。失败单单独显示「失败 N」，不静默。
        entries = _count_status(rows.get("entries"), _ENTRY_ACTION_STATUSES)
        exits = _count_status(rows.get("exits"), _EXIT_ACTION_STATUSES)
        c_entries = _count_status(rows.get("call_entries"), _ENTRY_ACTION_STATUSES)
        c_exits = _count_status(rows.get("call_exits"), _EXIT_ACTION_STATUSES)
        covers = sum(1 for r in (rows.get("covers") or [])
                     if str(r.get("action")) in ("market", "limit", "filled"))
        failed = sum(_count_status(rows.get(k), _FAILED_STATUSES)
                     for k in ("entries", "exits", "call_entries", "call_exits"))
        pending = sum(_count_status(rows.get(k), _PENDING_STATUSES)
                      for k in ("entries", "exits", "call_entries", "call_exits"))
        self._bump("entries", entries)
        self._bump("exits", exits)
        self._bump("call_entries", c_entries)
        self._bump("call_exits", c_exits)
        self._bump("covers", covers)
        try:
            lst.set_round(settled=settled, strategy=strat, error=error or "")
        except Exception:  # noqa: BLE001 —— 状态展示失败不阻塞策略
            pass
        self._log(f"── 巡检轮次结束 ── 到期判定 {len(settled or [])} 笔 · "
                  f"策略 卖put {entries} / 买回put {exits} · "
                  f"卖call {c_entries} / 买回call {c_exits}"
                  + (f" · 补买 {covers}" if covers else "")
                  + (f" · 在途 {pending}" if pending else "")
                  + (f" · 失败 {failed}" if failed else "")
                  + (f" · error={error}" if error else ""))

    # ══════════════════════ 小工具 ══════════════════════

    def _log(self, msg: str) -> None:
        """诊断日志 —— 一律 stderr（gatekeeper 丢 logger.info；launch.sh 会 eval stdout）。"""
        print(f"[OPT-LIVE] {msg}", file=sys.stderr, flush=True)

    def _record(self, event: dict) -> None:
        """append-only 事件落盘（仅实盘；回测置 live_mode=False 不污染监控）。

        同类「无动作」事件（skipped_* / failed / dry_run* …）按
        (inst_id, type, status) 抑制重复（见 event_log）——首条必记、之后至少间隔
        1 小时，避免到期日 480 条 skipped_expired 把真事件淹在噪音里。
        """
        if not self.parameters.get("live_mode"):
            return
        try:
            row = {"ts": _utc_now(), **event}
            if not event_log.should_log_event(row):
                return
            p = self._events_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 —— 事件是 UX 信息，不阻塞业务
            pass

    def _events_path(self) -> Path:
        """事件文件路径 —— 与 runner 同源（`okx_options_live.events_path()`）。

        惰性 import 而非模块级：runner 也是惰性 import 本策略，这样两边
        都不会在 import 期成环。测试只需 patch 一处路径即可完全隔离。
        """
        from .. import okx_options_live as _ol
        return _ol.events_path()


__all__ = ["OkxOptionsPutStrategy"]
