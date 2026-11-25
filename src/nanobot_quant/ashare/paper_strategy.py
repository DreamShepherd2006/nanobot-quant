"""A 股 ETF 期权模拟账本的决策规则（纯函数，无 IO、无状态）。

与 ``paper_ledger.py`` 分工：那边只管记账，这边只管「选哪张、什么时候平」。
两者都不碰网络，行情由 runner（实时）或回放脚本（历史）喂进来 —— 这样同一套
规则既能在历史数据上自测，也能在实时行情上推进，不会出现「回测一套、实盘
另一套」。

规则口径对齐 QMT 脚本 v5_6（2026-11-22 回测基线）
------------------------------------------------
- 信号：1m TD 买9 首穿（``setup_buy`` 从 <9 到 ≥9），按交易日切分；
- 选档：到期窗口 [min_dte, max_dte] → 虚值 ≥``min_distance_pct`` → bid>0
  → 净权利金 > 0 → 按**净收益率**降序取第一张；
- 出场：权利金回落到 ``take_profit_pct`` 即买回；``dte <= force_close_dte``
  一律强平（宁少赚不接货）；到期按结算价处理（ledger 那边做）。

净收益率分母是**担保额** ``strike × mult``（接货时真正占用的那笔钱），不是
权利金本身 —— 口径错了排序就会挑到最贵的而不是最划算的。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

DEFAULT_MULT = 10_000
FEE_OPEN_PER_CONTRACT = 1.2


@dataclass
class PaperParams:
    """模拟账本的规则参数（默认值 = QMT v5_6 基线）。"""

    # 选档
    min_dte: int = 6                 # 开仓最短剩余天数（force_close_dte 之上一档才有意义）
    max_dte: int = 35                # 最长剩余天数（更远期流动性差、占用资金久）
    min_distance_pct: float = 5.0    # 虚值硬过滤：strike <= spot × (1 − 5%)
    band_pct: float = 12.0           # 候选扫描带（spot ± 12%，与盘口采集口径一致）
    # 张数
    max_per_underlying: int = 3
    max_total: int = 6
    max_per_wave: int = 1            # 同浪（一次持仓周期）每标的只开 1 张；0 = 关闭
    # 出场
    take_profit_pct: float = 50.0    # 权利金回落 50% 买回
    force_close_dte: int = 5         # DTE <= 5 强平（赚权利金的标的：宁可少赚也不接货）
    # 费用
    fee_open: float = FEE_OPEN_PER_CONTRACT
    mult: int = DEFAULT_MULT

    def __post_init__(self) -> None:
        if self.min_dte <= self.force_close_dte:
            raise ValueError(
                f"min_dte({self.min_dte}) 必须大于 force_close_dte("
                f"{self.force_close_dte})，否则开仓当根就会被 DTE 强平"
            )


def days_to_expiry(expiry: str | date, today: date) -> int:
    """剩余自然日。``expiry`` 支持 ``"YYYYMMDD"`` 与 ``date``。"""
    if isinstance(expiry, date):
        d = expiry
    else:
        s = str(expiry).strip()
        d = datetime.strptime(s, "%Y%m%d").date()
    return (d - today).days


def net_premium(bid: float, mult: int = DEFAULT_MULT,
                fee: float = FEE_OPEN_PER_CONTRACT) -> float:
    """卖出一张的净权利金（元）= bid × 面值 − 手续费。"""
    return float(bid) * mult - float(fee)


def net_yield_pct(strike: float, bid: float, mult: int = DEFAULT_MULT,
                  fee: float = FEE_OPEN_PER_CONTRACT) -> float:
    """净收益率（%）= 净权利金 ÷ 担保额 × 100。"""
    collateral = float(strike) * mult
    if collateral <= 0:
        return 0.0
    return net_premium(bid, mult, fee) / collateral * 100.0


def pick_put(chain: list[dict], spot: float, today: date,
             params: PaperParams | None = None) -> dict | None:
    """从链上选一张卖出 put。返回带 ``net_yield_pct`` 的候选 dict，无合适则 None。

    ``chain`` 元素需含：``code`` / ``underlying`` / ``strike`` / ``expiry`` /
    ``bid``（缺失或非正会被跳过）。函数不修改入参。
    """
    p = params or PaperParams()
    if not chain or not spot or spot <= 0:
        return None
    floor_strike = float(spot) * (1.0 - p.min_distance_pct / 100.0)
    best: dict | None = None
    best_yield = None
    for c in chain:
        try:
            code = str(c.get("code") or "")
            strike = float(c.get("strike") or 0.0)
            bid = float(c.get("bid") or 0.0)
            expiry = str(c.get("expiry") or "")
        except (TypeError, ValueError):
            continue
        if not code or strike <= 0 or bid <= 0 or not expiry:
            continue
        try:
            dte = days_to_expiry(expiry, today)
        except ValueError:
            continue
        if dte < p.min_dte or dte > p.max_dte:
            continue
        if strike > floor_strike:          # 距离不够（太靠近平价）→ 跳过
            continue
        if abs(strike - float(spot)) > float(spot) * p.band_pct / 100.0:
            continue
        np_ = net_premium(bid, p.mult, p.fee_open)
        if np_ <= 0:                       # 扣费后不赚钱的合约一律排除（fail-closed）
            continue
        y = net_yield_pct(strike, bid, p.mult, p.fee_open)
        if best_yield is None or y > best_yield:
            best_yield = y
            best = dict(c, dte=dte, net_premium=round(np_, 2),
                        net_yield_pct=round(y, 4))
    return best


def should_close(short: dict, mark_px: float, today: date,
                 params: PaperParams | None = None) -> tuple[bool, str]:
    """是否需要买回平仓。返回 ``(bool, reason)``；不需要时 reason 为空串。

    出场优先级：止盈 → DTE 强平。到期由 ledger 的 ``settle_expiry`` 接管，
    这里不返回（到期日当天由调用方先判结算）。
    """
    p = params or PaperParams()
    entry_px = float(short.get("entry_px") or 0.0)
    mark = float(mark_px or 0.0)
    if entry_px > 0 and mark > 0:
        drop_pct = (entry_px - mark) / entry_px * 100.0
        if drop_pct >= p.take_profit_pct:
            return True, f"take_profit: 权利金回落 {drop_pct:.1f}%"
    try:
        dte = days_to_expiry(str(short.get("expiry") or ""), today)
    except ValueError:
        return False, ""
    if dte <= p.force_close_dte:
        return True, f"dte_force: 剩余 {dte} 天 ≤ {p.force_close_dte}"
    return False, ""


def is_expired(short: dict, today: date) -> bool:
    """是否已到期（到期日当天视为已到期、由账本接货或作废）。"""
    try:
        return days_to_expiry(str(short.get("expiry") or ""), today) <= 0
    except ValueError:
        return False


def can_open(shorts: list[dict], underlying: str,
             params: PaperParams | None = None) -> tuple[bool, str]:
    """张数额度门（per-underlying / total / per-wave 由调用方分别判断）。

    ``shorts`` 是当前所有 open 义务仓（dict 列表）。这里只判「标的上限」与
    「总量上限」，同浪计数需要持仓周期状态，留给调用方。
    """
    p = params or PaperParams()
    n_undl = sum(1 for s in shorts if s.get("underlying") == underlying)
    if n_undl >= p.max_per_underlying:
        return False, f"标的额度已满（{n_undl}/{p.max_per_underlying}）"
    if len(shorts) >= p.max_total:
        return False, f"总额度已满（{len(shorts)}/{p.max_total}）"
    return True, ""
