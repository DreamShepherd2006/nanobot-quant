"""卖 put 自动循环的决策核心（纯函数：不碰 SDK、不下单、不写台账）。

一轮决策三件事：

1. ``evaluate_entry()`` —— 该不该卖 put、卖哪个合约
   （TD 衰竭信号 → IV 环境闸门 → 张数上限 → 复用 C28 1a 的 ``select_puts`` 选档）
2. ``evaluate_call_entry()`` —— 该不该卖 call（covered call）、卖哪个合约
   （**不做信号择时**：张数上限 → covered 容量 → 成本锚 C → ``select_calls`` 选档）
3. ``evaluate_exits()`` —— 已持有的 short 仓该不该买回（权利金回落止盈，按方向分线）

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

from nanobot_quant.okx_options_assets import right_of_inst
from nanobot_quant.okx_options_select import select_calls, select_puts

# 权利金回落止盈的默认线：卖 put 50%（实证分水岭）/ 卖 call 30%（上行无界、快落袋）
DEFAULT_TP_PCT = 50.0
DEFAULT_TP_PCT_CALL = 30.0


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
    opt_type: str = "P"                        # P = 卖 put 线 / C = 卖 call（covered）线
    collateral_usd: Optional[float] = None     # 收益率分母（put=名义 / call=现货市值）
    cost_basis: Optional[float] = None         # 卖 call 的成本锚 C（保本价）

    def to_event(self) -> dict:
        return {
            "family": self.family, "inst_id": self.inst_id, "strike": self.strike,
            "sz": self.sz, "bid": self.bid, "entry_reason": self.entry_reason,
            "spot": self.spot, "net_yield_pct": self.net_yield_pct,
            "apr_pct": self.apr_pct, "days": self.days, "iv": self.iv,
            "delta": self.delta, "notional_usd": self.notional_usd,
            "note": self.note, "opt_type": self.opt_type,
            "collateral_usd": self.collateral_usd, "cost_basis": self.cost_basis,
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

def evaluate_call_entry(family: str, *, params: dict,
                        covered: Optional[dict] = None,
                        open_calls: int = 0, total_calls: int = 0,
                        cost_basis: Optional[float] = None,
                        selector: Optional[dict] = None,
                        chain: Optional[dict] = None,
                        base_px: Optional[float] = None,
                        ) -> tuple[Optional[EntryDecision], str]:
    """卖 call（covered call）决策。返回 ``(决策, note)``；决策为 None 时 note 说明原因。

    **不做信号择时**（用户 2026-09-08 定稿 + 2026-09-24 确认）：入场条件 =
    「有 covered 余量 + 保本门可判定 + 有合格候选」；自节流 = call 张数上限 ×
    covered 容量 × 止盈/到期后才有新余量。

    门序（每一道 fail-closed；执行层 ``_open_option`` 会再校验一遍）：

    ① **call 张数上限**（家族 / 全局，与 put 额度相互独立）
    ② **covered 容量**：现货覆盖张数 − 在仓 call 张数 ≥ 1
    ③ **成本锚 C**：显式传入 > ``covered['cost_hint']``（同家族已 settled_itm
       put 的 max(K)）；两处都无 → 除非 ``allow_no_cost_basis``，否则跳过
       （与手工路径的「无成本锚确认」同语义：不给 C 就不卖）
    ④ 选档 ``select_calls``（含保本门硬过滤 ``strike + bid ≥ C``）

    Args:
        covered: ``okx_options_trade.covered_context()`` 的输出；None = 未取数
            （无法判定 covered 容量 → fail-closed 跳过）。
        open_calls / total_calls: 该家族 / 全局在仓 call 张数。
        cost_basis: 显式成本锚（页面/调用方指定）；None 时用 ``covered['cost_hint']``。
    """
    p = params or {}

    # ① 张数上限（call 自己的额度，硬约束）
    max_fam = int(p.get("max_calls_per_family") or 0)
    max_all = int(p.get("max_calls_total") or 0)
    if max_fam > 0 and open_calls >= max_fam:
        return None, f"张数上限：{family} 在仓 call {open_calls} ≥ {max_fam}"
    if max_all > 0 and total_calls >= max_all:
        return None, f"张数上限：全局在仓 call {total_calls} ≥ {max_all}"

    # ② covered 容量（现货覆盖张数 − 在仓 call）
    cov = covered if isinstance(covered, dict) else None
    if cov is None:
        return None, "covered 上下文不可用（现货查询失败）→ 跳过（fail-closed）"
    sellable = int(_f(cov.get("sellable_sz")) or 0)
    usable = sellable - int(open_calls)
    base = str(cov.get("base") or str(family).split("-")[0])
    if usable < 1:
        return None, (f"covered 容量不足（现货 {cov.get('spot_avail')} {base} → 覆盖 "
                      f"{sellable} 张 − 在仓 call {open_calls} = {usable} 张）")

    # ③ 成本锚 C
    c = _f(cost_basis)
    if c is None:
        c = _f(cov.get("cost_hint"))
    notes = [f"covered（现货 {cov.get('spot_avail')} {base} → 可卖 {sellable} 张）"]
    if c is None:
        if not p.get("allow_no_cost_basis"):
            return None, ("无成本锚 C（同家族无 settled_itm put 接货记录）→ "
                          "fail-closed 跳过；确需放行请勾选「允许无成本锚」")
        notes.append("⚠️ 无成本锚（已允许）—— 本次卖出不校验保本门，台账标「无成本锚」")
    else:
        notes.append(f"成本锚 C={c:g}（保本门 K+px ≥ C）")

    # ④ 选档
    sel = p.get("selector") if isinstance(p.get("selector"), dict) else selector
    res = select_calls(family, base_px=base_px, selector=sel, chain=chain,
                       cost_basis=c)
    cands = res.get("candidates") or []
    if not cands:
        return None, (f"无合格候选（{family} base_px={res.get('base_px')} "
                      f"过滤={res.get('filtered')}）")
    cand = cands[0]
    if res.get("note"):
        notes.append(f"选择器：{res['note']}")
    decision = EntryDecision(
        family=family,
        inst_id=str(cand.get("inst_id") or ""),
        strike=float(cand.get("strike") or 0),
        sz=1,
        bid=float(cand.get("bid") or 0),
        entry_reason=(f"covered(现货 {cov.get('spot_avail')} {base} / 可卖 {sellable} 张)"),
        spot=_f(res.get("spot")),
        net_yield_pct=_f(cand.get("net_yield_pct")),
        apr_pct=_f(cand.get("apr_pct")),
        days=_f(cand.get("days")),
        iv=_f(cand.get("iv")),
        delta=_f(cand.get("delta")),
        notional_usd=_f(cand.get("notional_usd")),
        note=" | ".join(notes),
        opt_type="C",
        collateral_usd=_f(cand.get("collateral_usd")),
        cost_basis=c,
    )
    return decision, decision.note


def _row_right(row: dict) -> str:
    """持仓行的期权方向（``P``/``C``）——行内 ``opt_type`` 优先，缺失时从 instId 尾段解析。

    两者都判不出 → ``""``（消费方按「不匹配」处理：方向不明的行一律不动仓，fail-closed）。
    """
    v = str((row or {}).get("opt_type") or "").strip().upper()
    if v in ("C", "P"):
        return v
    return right_of_inst(str((row or {}).get("inst_id") or ""))


def evaluate_exits(positions, *, tp_pct: float = DEFAULT_TP_PCT,
                   opt_type: str = "P") -> list[ExitDecision]:
    """权利金回落止盈：mark 价跌到开仓价的 (1 − tp_pct%) 以下 → 买回。

    Args:
        positions: ``okx_options_trade.open_option_positions()`` 的输出（含 side/pos/avg_px/mark_px）。
        tp_pct: 回落百分比阈值（50 = 权利金跌掉一半）；≤0 = 关闭止盈。
        opt_type: 只评估该方向的卖开仓（``"P"`` 卖 put 线 / ``"C"`` 卖 call 线）。
            方向隔离是硬约束 —— 卖 put 的止盈线不得平掉卖 call 仓（call 另有自己的
            止盈线），反之亦然（docs/quant-system.md §24 C42）。
    """
    out: list[ExitDecision] = []
    want = str(opt_type or "P").strip().upper()
    if not tp_pct or tp_pct <= 0:
        return out
    for row in positions or []:
        if str((row or {}).get("side") or "").lower() not in ("short", "net_short"):
            continue  # 只处理卖开仓（买 call / 长仓不归本策略管）
        if _row_right(row) != want:
            continue  # 方向隔离：非本方向的仓（含方向不可判定的行）不在本线止盈
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


def cycle_gate(state: dict, family: str, *, td_signal: dict, params: dict,
               has_position: bool = False) -> "str | None":
    """信号周期门控（纯函数，状态由调用方持有）。

    同一 TD 信号周期内同一家族只开一次仓 —— 与现货线
    ``td_sequential_strategy`` 的 ``_cycle_state`` 同规则：setup 计数单调
    不减（9→10→11）视为同周期 → 跳过；计数变小（12→8 / 9→1）标记 reset
    → 新周期放行。``cd_triggered`` 单独成位：setup 翻转但 countdown 仍在
    累积（cd_buy 未归 0）时保持，阻止「setup 买9 + 几根 bar 后 cd13 再补
    一张」的重复建仓。

    背景（期权线回测实测 2026-09-17）：首版无此门控，同一个「setup_buy
    9 → 10 → 11」的衰竭波里连开三张（其中两张同合约），31 天里 7 个独立
    信号被记成 12 次开仓 —— 样本虚高 1.7 倍，与用户「不追连续信号」原则
    相背。

    抽成纯函数是为了**实盘与回测共用同一份决策代码**（回测 driver 直接
    调决策函数、不经过 lumibot Strategy 类，门控写在类里回测就看不到）。

    Args:
        state: ``{family: {...}}`` 状态字典，调用方持有（策略类实例 /
            driver 实例）；首次见到的 family 由本函数初始化。
        family: 标的家族（如 ``SOL-USD_UM``）。
        has_position: 该家族当前是否已有持仓 —— 重启边界用：有仓视为
            本周期已建仓（保守不追，避免 setup 累加期重启后立即再开）。

    Returns:
        None = 放行；否则返回拦截原因（调用方落日志）。
    """
    st = state.get(family)
    if st is None:
        st = {"bought": False, "prev_setup": 0, "reset": False,
              "cd_triggered": False}
        if has_position:
            st["bought"] = True
            st["cd_triggered"] = True
        state[family] = st

    setup_buy = int(_f((td_signal or {}).get("setup_buy")) or 0)
    cd_buy = int(_f((td_signal or {}).get("cd_buy")) or 0)
    p = params or {}
    entry_setup = int(_f(p.get("entry_setup")) or 9)
    entry_cd = int(_f(p.get("entry_countdown")) or 13)

    if setup_buy < st["prev_setup"]:
        st["reset"] = True              # 计数变小 → 新信号周期
    st["prev_setup"] = setup_buy
    if st["reset"] and cd_buy == 0:
        st["cd_triggered"] = False

    if not (setup_buy >= entry_setup or cd_buy >= entry_cd):
        return None                      # 没信号，门控不参与
    if st["bought"] and not st["reset"]:
        return f"同周期已建仓（setup_buy={setup_buy} 未重置）"
    if st["cd_triggered"] and cd_buy >= entry_cd:
        return f"同 countdown 周期已建仓（cd_buy={cd_buy}）"
    return None


def cycle_mark_bought(state: dict, family: str) -> None:
    """建仓（含 dry-run 意图）后置位 —— 本周期内不再开仓。

    ⚠️ ``reset = False`` 不能漏：reset 是「计数变小 → 新周期」的一次性
    通行证，建仓时必须消费掉。否则 reset 一旦置位就永久保持 True，
    ``bought and not reset`` 恒为 False —— 门控从第一次计数回落之后就
    永久失效（期权线首版实测：只拦住 4 次，之后 setup 10/11 全放行）。
    与现货线 ``td_sequential_strategy`` 建仓处的三行赋值保持一致。
    """
    st = (state or {}).get(family)
    if st is None:
        return
    st["bought"] = True
    st["reset"] = False
    st["cd_triggered"] = True


def contracts_by_family(positions, *, opt_type: str = "P") -> dict[str, int]:
    """按标的统计在仓张数（张数上限判定用）—— 只统计 ``opt_type`` 方向的卖开仓。

    卖 put 与卖 call 各占自己的额度：call 仓不再吃掉 put 的张数额度（§24 C42）。
    """
    out: dict[str, int] = {}
    want = str(opt_type or "P").strip().upper()
    for row in positions or []:
        if str((row or {}).get("side") or "").lower() not in ("short", "net_short"):
            continue
        if _row_right(row) != want:
            continue
        inst = str(row.get("inst_id") or "")
        sz = _f(row.get("pos")) or 0
        if not inst or sz <= 0:
            continue
        base = inst.split("-")[0].upper()
        out[base] = out.get(base, 0) + int(sz)
    return out


__all__ = ["EntryDecision", "ExitDecision", "td_entry_reason", "evaluate_entry",
           "evaluate_call_entry", "evaluate_exits", "contracts_by_family",
           "DEFAULT_TP_PCT", "DEFAULT_TP_PCT_CALL"]
