"""期权卖 put 策略（lumibot ``Strategy``）—— E 期接线步 2，方案 B。

与 :class:`TdSequentialStrategy` 同构：节拍交给 lumibot 引擎
（``StrategyExecutor.run()``），策略只负责「一轮里做什么」。期权机制层
（daemon / 启停 / 优雅停止）复用 :class:`LiveRunnerBase`，本类只填决策。

每轮 ``on_trading_iteration``：

  ① **到期判定**：``ot.settle_expired_puts()``（OTM 自动关账 / ITM 记赔付 /
     矛盾挂 settled_review fail-closed / 账单未出保持 open 下轮重试）。
  ② **入场**（逐家族）：标的 TD 信号（OKX 现货 K 线 + 原版 TD 引擎）→
     IV 环境闸门 → 张数上限 → ``select_puts`` 选档 → 卖 put。
  ③ **止盈**：持仓权利金回落 ≥ 止盈线 → 买回平仓。
  ④ 状态写入 ``okx_options_live_state`` + 事件文件落盘。

``dry_run=True``（默认）时**只记录决策、不下单** —— 「AI 不能自行授权实盘」
在闭环里的结构性落实；需用户在期权页手动取消勾选才会真实下单。

停止：**不调 ``executor.stop()``**（lumibot 内部 ``shutdown(wait=True)`` 会等
业务轮收尾，遇网络卡死即永久挂住 —— TD live 已踩过）。改为
``parameters["stop_requested"]`` 标志，每轮开头检查后主动退出，
使停止永远落在两轮之间，绝不打断正在执行的一轮。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lumibot.strategies.strategy import Strategy

from .. import okx_options_trade as ot
from .. import okx_options_strategy as st
from .. import okx_options_live_state as lst

# 事件文件与期权台账同目录（append-only JSONL，跨重启保留）
_EVENTS_NAME = "okx_options_live_events.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        "dry_run": True,
        "live_mode": False,        # 事件文件写入开关（回测置 False 不污染实盘监控）
        "stop_requested": False,   # 见模块 docstring：唯一的停止通道
    }

    # ══════════════════════ 生命周期 ══════════════════════

    def initialize(self, **kwargs) -> None:
        """不作账户切换、不预拉数据 —— 全部延迟到轮次内（失败也不阻塞启动）。"""
        self._log("策略已初始化（卖 put + 权利金回落止盈）")

    def before_market_opens(self) -> None:  # pragma: no cover - 期权 7×24，无需
        pass

    # ══════════════════════ 主轮次 ══════════════════════

    def on_trading_iteration(self) -> None:
        """一轮：到期判定 → 入场 → 止盈 → 状态落盘。异常不外抛给引擎。"""
        if self.parameters.get("stop_requested"):
            # 停止永远落在两轮之间（绝不打断正在执行的一轮）
            self._log("收到停止请求 —— 本轮结束后退出")
            raise SystemExit("options strategy stop requested")

        p = self.parameters
        account = str(p.get("account") or "")
        dry = bool(p.get("dry_run", True))
        self._log("── 巡检轮次开始 ──")

        settled = self._settle_expired()

        positions = self._positions(account)
        if positions is None:
            self._log("⚠️ 持仓查询失败 —— 本轮跳过策略（fail-closed）")
            self._finish(settled, None, "持仓查询失败")
            return

        counts = st.contracts_by_family(positions)
        total = sum(counts.values())
        self._log(f"当前持仓 | 在仓合约数={total} 分家族={counts or '{}'} "
                  f"明细={[(x.get('inst_id'), x.get('pos')) for x in positions]}")

        entries = self._entries(account, p, dry, counts, total, positions)
        exits = self._exits(account, p, dry, positions)

        self._finish(settled, {"entries": entries, "exits": exits, "counts": counts},
                     None)

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
        out: list[dict] = []
        for family in (p.get("families") or []):
            base = str(family).split("-")[0]
            sig = self._td_signal(family, base, p)
            if sig is None:
                out.append({"family": base, "status": "no_signal",
                            "note": "无 K 线数据"})
                continue
            self._log(f"{base} TD | setup_buy={sig.get('setup_buy')}/{p.get('entry_setup')} "
                      f"cd_buy={sig.get('cd_buy')}/{p.get('entry_countdown')} "
                      f"setup_sell={sig.get('setup_sell')} cd_sell={sig.get('cd_sell')} "
                      f"price={sig.get('price')} rec={sig.get('recommendation')}")

            gate = self._cycle_gate(family, sig, p, positions)
            if gate:
                self._log(f"{base} → 周期门控拦截：{gate}")
                out.append({"family": base, "status": "cycle_wait", "note": gate})
                continue

            dec, note = st.evaluate_entry(
                family, td_signal=sig, params=p,
                open_contracts=counts.get(base, 0), total_contracts=total)
            if dec is None:
                self._log(f"{base} → 无动作：{note}")
                out.append({"family": base, "status": "no_action", "note": note})
                continue

            rec = {**dec.to_event(), "dry_run": dry}
            if dry:
                rec["status"] = "dry_run(would_sell)"
                self._log(f"{base} → 【dry-run】卖出 {dec.inst_id} ×{dec.sz} "
                          f"| 盘口 bid={rec.get('bid')} "
                          f"净收益率={rec.get('net_yield_pct')}% "
                          f"年化={rec.get('apr_pct')}% 担保=${rec.get('notional_usd')} "
                          f"IV={rec.get('iv')} delta={rec.get('delta')} "
                          f"天数={rec.get('days')} | 理由={dec.entry_reason}")
            else:
                ok, err = self._submit_option(dec, p)
                rec["status"] = "sold" if ok else "failed"
                if err:
                    rec["error"] = err
                    self._log(f"⚠️ {base} 卖出失败 {dec.inst_id} ×{dec.sz}：{err}")
                else:
                    counts[base] = counts.get(base, 0) + dec.sz
                    total += dec.sz
                    self._log(f"{base} → 已提交卖出 {dec.inst_id} ×{dec.sz}")
            out.append(rec)
            # 建仓（含 dry-run 意图）即置位 —— 之后同周期不再开仓
            self._cycle_mark_bought(family, sig)
            self._record({"type": "entry", **rec})
        return out

    # ══════════════════════ ③ 止盈 ══════════════════════

    def _exits(self, account: str, p: dict, dry: bool, positions) -> list[dict]:
        try:
            tp = float(p.get("take_profit_pct") or 0)
        except (TypeError, ValueError):
            tp = 0.0
        try:
            rows = st.evaluate_exits(positions, tp_pct=tp)
        except Exception as e:  # noqa: BLE001
            self._log(f"⚠️ 止盈评估异常：{type(e).__name__}: {e}")
            return []
        out: list[dict] = []
        for x in rows:
            rec = {**x.to_event(), "dry_run": dry}
            if dry:
                rec["status"] = "dry_run(would_buy_back)"
                self._log(f"→ 【dry-run】买回 {x.inst_id} ×{x.sz} | "
                          f"开仓 {rec.get('entry_px')} → 现价 {rec.get('mark_px')} "
                          f"（回落 {rec.get('drop_pct')} ≥ 止盈线 {tp}%）")
            else:
                ok, err = self._submit_option(x, p, closing=True)
                rec["status"] = "bought_back" if ok else "failed"
                if err:
                    rec["error"] = err
                    self._log(f"⚠️ 买回失败 {x.inst_id} ×{x.sz}：{err}")
                else:
                    self._log(f"→ 已提交买回 {x.inst_id} ×{x.sz}")
            out.append(rec)
            self._record({"type": "exit", **rec})
        if not rows and positions:
            self._log(f"止盈巡检 | {len(positions)} 张在仓，均未达回落 {tp}% 门槛，继续持有")
        return out

    # ══════════════════════ 下单（经 lumibot broker 抽象）══════════════════════

    def _submit_option(self, dec, p: dict, closing: bool = False):
        """决策 → lumibot ``create_order`` → ``OkxOptionsBroker._submit_order``。

        卖开：side="sell"（broker 按 ``right`` 分派 open_put/open_call）；
        买回：side="buy"（走 close_*）。返回 ``(ok, error_message)``。
        """
        try:
            asset = self._asset_for(dec)
            side = "buy" if closing else "sell"
            order = self.create_order(asset, side, int(dec.sz))
            if order is None:
                return False, "create_order 返回 None"
            err = getattr(order, "error", None) or getattr(order, "_error", None)
            if err:
                return False, str(err)
            return True, None
        except Exception as e:  # noqa: BLE001 —— 失败必须可见，不静默
            return False, f"{type(e).__name__}: {e}"

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
            return ot.open_puts(account) or []
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
        entries = len((strat or {}).get("entries") or [])
        exits = len((strat or {}).get("exits") or [])
        self._bump("entries", entries)
        self._bump("exits", exits)
        try:
            lst.set_round(settled=settled, strategy=strat, error=error or "")
        except Exception:  # noqa: BLE001 —— 状态展示失败不阻塞策略
            pass
        self._log(f"── 巡检轮次结束 ── 到期判定 {len(settled or [])} 笔 · "
                  f"策略 卖 {entries} / 买回 {exits}"
                  + (f" · error={error}" if error else ""))

    # ══════════════════════ 小工具 ══════════════════════

    def _log(self, msg: str) -> None:
        """诊断日志 —— 一律 stderr（gatekeeper 丢 logger.info；launch.sh 会 eval stdout）。"""
        print(f"[OPT-LIVE] {msg}", file=sys.stderr, flush=True)

    def _record(self, event: dict) -> None:
        """append-only 事件落盘（仅实盘；回测置 live_mode=False 不污染监控）。"""
        if not self.parameters.get("live_mode"):
            return
        try:
            p = self._events_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": _utc_now(), **event}
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
