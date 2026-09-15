"""卖 put 自动循环的决策核心（纯函数：不碰 SDK、不下单、不写台账）。

一轮决策两件事：

1. ``evaluate_entry()`` —— 该不该卖 put、卖哪个合约
   （TD 衰竭信号 → IV 环境闸门 → 张数上限 → 复用 C28 1a 的 ``select_puts`` 选档）
2. ``evaluate_exits()`` —— 已持有的 short 仓该不该买回（权利金回落止盈）

刻意与执行分离：本模块只产出决策对象，``okx_options_live`` 的循环体负责执行。
好处是同一份决策逻辑同时服务 dry_run 观察、单元测试与将来的期权回测
（一处改三处同步）。

第一版参数取向（用户拍板：先跑通闭环，参数用真实样本后续迭代）：

- **张数上限是硬约束**，先于任何信号判断生效（同家族 / 全局两道）。
- **IV 闸门默认关闭**（``iv_min_percentile=0``）——历史 IV 分位需要样本积累，
  启用前不假装有依据；样本不足时 fail-open 放行（不拦截），并在 note 里标注。
- 卖点价格一路走“bid 侧”：只有 bid > 0（有买盘）的合约才是 ``select_puts``
  的合格候选。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from nanobot_quant.okx_options_select import select_puts

# 权利金回落止盈的默认线（卖 put 实证分水岭：50%）
DEFAULT_TP_PCT = 50.0


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── 决策对象 ─────────────────────────────────────────────

@dataclass
class EntryDecision:
    """卖 put 建议（一条）。"""

    family: str
    inst_id: str
    strike: float
    sz: int
    bid: float
    entry_reason: str
    spot: Optional[float] = None
    net_yield_pct: Optional[float] = None
    apr_pct: Optional[float] = None
    days: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    notional_usd: Optional[float] = None
    note: str = ""

    def to_event(self) -> dict:
        return {
            "family": self.family, "inst_id": self.inst_id, "strike": self.strike,
            "sz": self.sz, "bid": self.bid, "entry_reason": self.entry_reason,
            "spot": self.spot, "net_yield_pct": self.net_yield_pct,
            "apr_pct": self.apr_pct, "days": self.days, "iv": self.iv,
            "delta": self.delta, "notional_usd": self.notional_usd,
            "note": self.note,
        }


@dataclass
class ExitDecision:
    """买回建议（一条）。"""

    inst_id: str
    sz: int
    entry_px: float
    mark_px: float
    drop_pct: float
    reason: str = "take_profit"

    def to_event(self) -> dict:
        return {"inst_id": self.inst_id, "sz": self.sz, "entry_px": self.entry_px,
                "mark_px": self.mark_px, "drop_pct": self.drop_pct,
                "reason": self.reason}


# ── 入场 ─────────────────────────────────────────────────

def td_entry_reason(td_signal: dict, *, entry_setup: int,
                    entry_countdown: int) -> Optional[str]:
    """TD 衰竭信号 → 入场理由；None = 无信号。

    两个通道：``setup_buy >= entry_setup``（买 9 衰竭）或
    ``cd_buy >= entry_countdown``（countdown 13，更彻底）；先到先算。
    """
    if not isinstance(td_signal, dict):
        return None
    su = _f(td_signal.get("setup_buy")) or 0.0
    cd = _f(td_signal.get("cd_buy")) or 0.0
    if entry_setup > 0 and su >= entry_setup:
        return f"buy9(setup_buy={int(su)})"
    if entry_countdown > 0 and cd >= entry_countdown:
        return f"cd13(cd_buy={int(cd)})"
    return None


def evaluate_entry(family: str, *, td_signal: dict, params: dict,
                   open_contracts: int = 0, total_contracts: int = 0,
                   iv_percentile: Optional[float] = None,
                   selector: Optional[dict] = None,
                   chain: Optional[dict] = None,
                   base_px: Optional[float] = None,
                   ) -> tuple[Optional[EntryDecision], str]:
    """卖 put 决策。返回 ``(决策, note)``；决策为 None 时 note 说明为何不动作。

    Args:
        td_signal: 标的的 TD 信号（``td_sequential.calculate()`` 输出的最后一根）。
        params: live 参数（entry_setup / entry_countdown / max_contracts_* /
            iv_min_percentile / selector）。
        open_contracts: 该家族当前在仓张数；total_contracts: 全局在仓张数。
        iv_percentile: 当前 IV 分位（0–100）；None = 样本不足（fail-open）。
        chain: 注入期权链（测试用；None 时自拉）。
    """
    p = params or {}

    # ① 张数上限（硬约束，先于信号判断）
    max_fam = int(p.get("max_contracts_per_family") or 0)
    max_all = int(p.get("max_contracts_total") or 0)
    if max_fam > 0 and open_contracts >= max_fam:
        return None, f"张数上限：{family} 在仓 {open_contracts} ≥ {max_fam}"
    if max_all > 0 and total_contracts >= max_all:
        return None, f"张数上限：全局在仓 {total_contracts} ≥ {max_all}"

    # ② TD 衰竭信号
    entry_setup = int(p.get("entry_setup") or 9)
    entry_cd = int(p.get("entry_countdown") or 13)
    reason = td_entry_reason(td_signal, entry_setup=entry_setup,
                             entry_countdown=entry_cd)
    if reason is None:
        su = int(_f((td_signal or {}).get("setup_buy")) or 0)
        cd = int(_f((td_signal or {}).get("cd_buy")) or 0)
        return None, f"无 TD 衰竭信号（setup_buy={su}/{entry_setup} cd_buy={cd}/{entry_cd}）"

    # ③ IV 环境闸门（默认关；样本不足 fail-open）
    notes = [f"信号：{reason}"]
    iv_min = _f(p.get("iv_min_percentile")) or 0.0
    if iv_min > 0:
        if iv_percentile is None:
            notes.append(f"IV 闸门：样本不足，放行（阈值 {iv_min:g} 分位）")
        elif iv_percentile < iv_min:
            return None, f"IV 闸门：{iv_percentile:.0f} 分位 < {iv_min:g}（恐慌未定价，跳过）"
        else:
            notes.append(f"IV 闸门：{iv_percentile:.0f} 分位 ≥ {iv_min:g}")

    # ④ 选档（复用 C28 1a）
    sel = p.get("selector") if isinstance(p.get("selector"), dict) else selector
    res = select_puts(family, base_px=base_px, selector=sel, chain=chain)
    cands = res.get("candidates") or []
    if not cands:
        return None, (f"无合格候选（{family} base_px={res.get('base_px')} "
                      f"过滤={res.get('filtered')}）")
    c = cands[0]
    if res.get("note"):
        notes.append(f"选择器：{res['note']}")
    decision = EntryDecision(
        family=family,
        inst_id=str(c.get("inst_id") or ""),
        strike=float(c.get("strike") or 0),
        sz=1,
        bid=float(c.get("bid") or 0),
        entry_reason=reason,
        spot=_f(res.get("spot")),
        net_yield_pct=_f(c.get("net_yield_pct")),
        apr_pct=_f(c.get("apr_pct")),
        days=_f(c.get("days")),
        iv=_f(c.get("iv")),
        delta=_f(c.get("delta")),
        notional_usd=_f(c.get("notional_usd")),
        note=" | ".join(notes),
    )
    return decision, decision.note


# ── 出场（止盈买回） ───────────────────────────────

def evaluate_exits(positions, *, tp_pct: float = DEFAULT_TP_PCT) -> list[ExitDecision]:
    """权利金回落止盈：mark 价跌到开仓价的 (1 − tp_pct%) 以下 → 买回。

    Args:
        positions: ``okx_options_trade.open_puts()`` 的输出（含 side/pos/avg_px/mark_px）。
        tp_pct: 回落百分比阈值（50 = 权利金跌掉一半）；≤0 = 关闭止盈。
    """
    out: list[ExitDecision] = []
    if not tp_pct or tp_pct <= 0:
        return out
    for row in positions or []:
        if str((row or {}).get("side") or "").lower() not in ("short", "net_short"):
            continue  # 只处理卖开仓（买 call / 长仓不归本策略管）
        entry = _f(row.get("avg_px"))
        mark = _f(row.get("mark_px"))
        sz = _f(row.get("pos"))
        inst = str(row.get("inst_id") or "")
        if not entry or entry <= 0 or mark is None or not sz or sz <= 0 or not inst:
            continue
        drop = (entry - mark) / entry * 100.0
        if drop >= tp_pct:
            out.append(ExitDecision(inst_id=inst, sz=int(sz), entry_px=entry,
                                    mark_px=mark, drop_pct=round(drop, 2)))
    return out


def contracts_by_family(positions) -> dict[str, int]:
    """按标的统计在仓张数（张数上限判定用）。"""
    out: dict[str, int] = {}
    for row in positions or []:
        if str((row or {}).get("side") or "").lower() not in ("short", "net_short"):
            continue
        inst = str(row.get("inst_id") or "")
        sz = _f(row.get("pos")) or 0
        if not inst or sz <= 0:
            continue
        base = inst.split("-")[0].upper()
        out[base] = out.get(base, 0) + int(sz)
    return out


__all__ = ["EntryDecision", "ExitDecision", "td_entry_reason", "evaluate_entry",
           "evaluate_exits", "contracts_by_family", "DEFAULT_TP_PCT"]
