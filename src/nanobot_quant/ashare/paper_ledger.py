"""A 股 ETF 期权模拟账本 —— 记账核心（纸上交易，2026-11-25 立项）。

为什么需要它
------------
云端拿不到券商持仓（QMT 的 FileIO 只放行「新建不存在的文件」，已存在的
一律 ``PermissionError``，读不回来），但两件事都绕不开「一个有成本和浮盈的
账户」：

1. **D 方案出场（TD 高9 + 浮盈）** 需要成本价才能判断浮盈；
2. **规则的前向验证** —— 回测跑的是静态历史，模拟账本用实时/离线行情逐 bar
   推进，攒出来的是无人干预的前向样本。

它不是什么
----------
- **不是影子账本**：不录入真实成交、不与 QMT 持仓对齐（那是另一条路）；
- **不能当实盘验收证据**：虚拟成交 ≠ 真实成交，展示层必须与实盘数字物理
  隔开、标题写明「模拟账本（不接券商）」；
- **不替代 QMT 的出场判断**：真实持仓在券商端，云端看不见，止盈 / DTE 强平
  仍由 QMT 脚本执行。

三条硬约束（都来自已经踩过的坑）
------------------------------
1. **100% 全损担保**：``可用现金 = cash − Σ(strike × mult × qty)``。
   平台只收名义的 7–12% 保证金，按它建模会允许开太多张、把资金效率算成
   虚高（用户实盘纪律是「按接货原则备足全额」）。
2. **成交价保守化**：卖出 = bar 收盘 − 1 tick、买回 = bar 收盘 + 1 tick。
   实测卖方价差 1–3 tick（≈1 元/张），按 mid 成交会系统性高估 ——
   OKX 线旧价差模型曾高估权利金约 1.41×。
3. **到期必须模拟行权接货**：只记权利金收入的账本在跌市里严重高估表现
   （卖 2.80 put、标的跌到 2.70，真实亏损 1000 元/张，不是「没赚到」）。

净值口径
--------
``equity = cash + Σ(ETF 数量 × ETF 现价) − Σ(空头数量 × mult × 空头现价)``

其中 ``cash`` 已经收了权利金，空头一侧是「按当前价买回的负债」，两者相抵
才是这笔义务仓的真实盈亏。``unrealized()`` 单列浮盈亏，与 ``equity`` 满足::

    equity − initial_cash == Σ 已实现 + Σ 浮盈

本模块只做记账：**无网络、无文件 IO、不含任何选档或择时规则**
（决策在 ``paper_strategy.py``、驱动在 runner / 回放脚本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MULT = 10_000                 # 每张 = 10000 份名义（A 股 ETF 期权统一）
DEFAULT_TICK = 0.0001                 # 最小变动价位；1 tick × 10000 = 1 元/张
FEE_OPEN_PER_CONTRACT = 1.2           # 卖出开仓手续费（元/张，用户提供，待确认是否含交易所费）
FEE_CLOSE_PER_CONTRACT = 1.7          # 买入平仓手续费（元/张）
FEE_EXERCISE_RATIO = 0.0002           # 行权费：0.02% × 名义，仅 ITM 收取


def _round2(x: float) -> float:
    return round(float(x), 2)


@dataclass
class ShortPut:
    """一条 sold put 义务仓（同一合约最多一条 —— 券商端合并持仓不可拆）。"""

    code: str
    underlying: str
    strike: float
    expiry: str                       # "YYYYMMDD"，字典序即时间序
    qty: int                          # 张
    entry_px: float                   # 权利金单价（已含 −1 tick 的保守化）
    entry_ts: str
    mult: int = DEFAULT_MULT
    entry_fee: float = 0.0
    mark_px: float = 0.0              # 当前 mark（用于浮盈亏与净值）
    reason: str = ""

    @property
    def collateral(self) -> float:
        """全损担保占用（元）：行权价 × 每张面值 × 张数。"""
        return float(self.strike) * self.mult * self.qty

    def pnl_pct(self) -> float:
        """按权利金口径的浮盈百分比（正 = 权利金回落、在赚）。"""
        if not self.entry_px:
            return 0.0
        return (self.entry_px - self.mark_px) / self.entry_px * 100.0

    def unrealized(self) -> float:
        """浮盈亏（元）= (卖出价 − 现价) × 面值 × 张数。"""
        return (self.entry_px - self.mark_px) * self.mult * self.qty

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "underlying": self.underlying,
            "strike": self.strike,
            "expiry": self.expiry,
            "qty": self.qty,
            "entry_px": self.entry_px,
            "entry_ts": self.entry_ts,
            "collateral": _round2(self.collateral),
            "mark_px": self.mark_px,
            "pnl_pct": round(self.pnl_pct(), 4),
            "unrealized": _round2(self.unrealized()),
            "entry_fee": _round2(self.entry_fee),
            "reason": self.reason,
        }


@dataclass
class EtfHolding:
    """被行权接货得到的 ETF 现货（后续 covered call 的供体）。"""

    underlying: str
    qty: float                        # 份
    cost_basis: float                 # 元/份（含行权费摊薄）
    since: str
    mark: float = 0.0

    def unrealized(self) -> float:
        return (self.mark - self.cost_basis) * self.qty

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "qty": round(self.qty, 3),
            "cost_basis": round(self.cost_basis, 6),
            "mark": round(self.mark, 4),
            "value": _round2(self.mark * self.qty),
            "unrealized": _round2(self.unrealized()),
            "since": self.since,
        }


@dataclass
class PaperLedger:
    """模拟账本。全部状态在内存，``snapshot()`` 给 runner 落盘用。"""

    initial_cash: float = 500_000.0
    cash: float = 0.0
    mult: int = DEFAULT_MULT
    tick: float = DEFAULT_TICK
    fee_open: float = FEE_OPEN_PER_CONTRACT
    fee_close: float = FEE_CLOSE_PER_CONTRACT
    fee_exercise_ratio: float = FEE_EXERCISE_RATIO
    shorts: dict[str, ShortPut] = field(default_factory=dict)   # code → 义务仓
    etf: dict[str, EtfHolding] = field(default_factory=dict)    # underlying → 现货
    fills: list[dict] = field(default_factory=list)             # append-only 成交

    def __post_init__(self) -> None:
        if not self.cash:
            self.cash = float(self.initial_cash)

    # ── 资金 ──────────────────────────────────────────────

    def collateral_locked(self) -> float:
        return sum(s.collateral for s in self.shorts.values())

    def available_cash(self) -> float:
        """可用现金 = 现金 − 全损担保占用（用户纪律：按接货备足全额）。"""
        return self.cash - self.collateral_locked()

    # ── 交易 ──────────────────────────────────────────────

    def sell_price(self, bar_close: float) -> float:
        """卖出成交价：吃 bid ≈ bar 收盘 − 1 tick（保守化）。"""
        return max(0.0, float(bar_close) - self.tick)

    def buy_price(self, bar_close: float) -> float:
        """买回成交价：吃 ask ≈ bar 收盘 + 1 tick（保守化）。"""
        return float(bar_close) + self.tick

    def sell_open(self, *, code: str, underlying: str, strike: float, expiry: str,
                  qty: int, bar_close: float = 0.0, ts: str, reason: str = "",
                  px: float | None = None) -> dict:
        """卖出开仓。返回 ``{"ok": True, "fill": {...}}`` 或 ``{"ok": False, "reason": ...}``。

        fail-closed 判据（任一不满足即拒绝，不打折、不近似）：
        合约已存在持仓、张数非正、成交价非正、可用现金不足担保额。

        ``px`` 给了真实卖一价就直接用（盘口优先），否则按 bar 收盘 − 1 tick
        保守化 —— 有实盘盘口时 "bar 收盘" 本身就是近似值，不该再叠一层 tick。
        """
        if code in self.shorts:
            return {"ok": False, "reason": f"合约已有持仓，不可重复开仓（{code}）"}
        if int(qty) <= 0:
            return {"ok": False, "reason": f"张数必须为正（qty={qty}）"}
        px = float(px) if (px is not None and float(px) > 0) else self.sell_price(bar_close)
        if px <= 0:
            return {"ok": False, "reason": f"成交价非正（bar_close={bar_close}）"}
        need = float(strike) * self.mult * int(qty)
        avail = self.available_cash()
        if avail + 1e-9 < need:
            return {"ok": False,
                    "reason": f"担保不足：需 {need:.2f}、可用 {avail:.2f}（100% 全损口径）"}
        premium = px * self.mult * int(qty)
        fee = self.fee_open * int(qty)
        self.cash += premium - fee
        self.shorts[code] = ShortPut(
            code=code, underlying=underlying, strike=float(strike),
            expiry=str(expiry), qty=int(qty), entry_px=px, entry_ts=ts,
            mult=self.mult, entry_fee=fee, mark_px=px, reason=reason,
        )
        fill = {
            "ts": ts, "action": "sell_open", "code": code, "underlying": underlying,
            "strike": float(strike), "expiry": str(expiry), "qty": int(qty),
            "px": round(px, 6), "premium": _round2(premium), "fee": _round2(fee),
            "cash_delta": _round2(premium - fee), "reason": reason,
        }
        self.fills.append(fill)
        return {"ok": True, "fill": fill}

    def buy_close(self, *, code: str, bar_close: float = 0.0, ts: str,
                  reason: str = "", px: float | None = None) -> dict:
        """买入平仓。合约不存在则拒绝（fail-closed，不做空、不猜）。

        ``px`` 给了真实买一价就直接用（吃 ask），否则 bar 收盘 + 1 tick。
        """
        sp = self.shorts.get(code)
        if sp is None:
            return {"ok": False, "reason": f"无该合约持仓，不可平仓（{code}）"}
        px = float(px) if (px is not None and float(px) > 0) else self.buy_price(bar_close)
        if px <= 0:
            return {"ok": False, "reason": f"成交价非正（bar_close={bar_close}）"}
        cost = px * sp.mult * sp.qty
        fee = self.fee_close * sp.qty
        self.cash -= cost + fee
        realized = (sp.entry_px - px) * sp.mult * sp.qty - sp.entry_fee - fee
        del self.shorts[code]
        fill = {
            "ts": ts, "action": "buy_close", "code": code, "underlying": sp.underlying,
            "strike": sp.strike, "expiry": sp.expiry, "qty": sp.qty,
            "px": round(px, 6), "cost": _round2(cost), "fee": _round2(fee),
            "cash_delta": _round2(-(cost + fee)), "realized": _round2(realized),
            "hold_ts": sp.entry_ts, "reason": reason,
        }
        self.fills.append(fill)
        return {"ok": True, "fill": fill}

    def settle_expiry(self, *, code: str, settle_spot: float, ts: str,
                      reason: str = "到期结算") -> dict:
        """到期结算。ITM → 按行权价接货；OTM → 义务消失、权利金全收。

        ``settle_spot`` 用到期日标的收盘价（A 股 ETF 期权实物交割，行权价成交）。
        """
        sp = self.shorts.get(code)
        if sp is None:
            return {"ok": False, "reason": f"无该合约持仓，不可结算（{code}）"}
        itm = float(settle_spot) < float(sp.strike)      # put 实值 = 标的价格 < 行权价
        if itm:
            gross = sp.strike * sp.mult * sp.qty
            fee = self.fee_exercise_ratio * gross
            self.cash -= gross + fee
            shares = sp.mult * sp.qty
            h = self.etf.get(sp.underlying)
            new_qty = (h.qty if h else 0.0) + shares
            new_cost = ((h.cost_basis * h.qty if h else 0.0) + gross + fee) / new_qty
            self.etf[sp.underlying] = EtfHolding(
                underlying=sp.underlying, qty=new_qty, cost_basis=new_cost,
                since=h.since if h else ts,
                mark=h.mark if h else float(settle_spot),
            )
            # 行权费已摊进 ETF 成本（cost_basis），这里不再重复扣，否则
            # ``equity − initial == 已实现 + 浮盈`` 会差一笔行权费。
            realized = sp.entry_px * sp.mult * sp.qty - sp.entry_fee
            action = "assign"
            note = (f"实值接货 {shares:.0f} 份 @ 行权价 {sp.strike}"
                    f"（成本 {new_cost:.4f} 元/份）")
        else:
            gross = 0.0
            fee = 0.0
            realized = sp.entry_px * sp.mult * sp.qty - sp.entry_fee
            action = "settle_otm"
            note = "虚值作废，权利金全额收取"
        del self.shorts[code]
        fill = {
            "ts": ts, "action": action, "code": code, "underlying": sp.underlying,
            "strike": sp.strike, "expiry": sp.expiry, "qty": sp.qty,
            "settle_spot": round(float(settle_spot), 4),
            "premium": _round2(sp.entry_px * sp.mult * sp.qty),
            "assign_cost": _round2(gross), "fee": _round2(fee),
            "realized": _round2(realized), "cash_delta": _round2(-(gross + fee)),
            "note": note, "reason": reason,
        }
        self.fills.append(fill)
        return {"ok": True, "fill": fill}

    # ── 估值 ──────────────────────────────────────────────

    def mark_short(self, code: str, px: float) -> None:
        sp = self.shorts.get(code)
        if sp is not None and px and px > 0:
            sp.mark_px = float(px)

    def mark_etf(self, underlying: str, px: float) -> None:
        h = self.etf.get(underlying)
        if h is not None and px and px > 0:
            h.mark = float(px)

    def short_liability(self) -> float:
        """义务仓负债 = 按现价买回的成本（未 mark 的用卖出价，浮盈为 0）。"""
        return sum(s.qty * s.mult * (s.mark_px or s.entry_px) for s in self.shorts.values())

    def etf_value(self) -> float:
        return sum(h.qty * (h.mark or h.cost_basis) for h in self.etf.values())

    def equity(self) -> float:
        return self.cash + self.etf_value() - self.short_liability()

    def unrealized(self) -> float:
        return (sum(s.unrealized() for s in self.shorts.values())
                + sum(h.unrealized() for h in self.etf.values()))

    def realized(self) -> float:
        """已实现盈亏 = 已平仓 / 已结算的 net（不含仍在仓的浮盈）。"""
        return sum(float(f.get("realized") or 0.0) for f in self.fills
                   if f.get("action") in ("buy_close", "assign", "settle_otm"))

    def open_entry_fees(self) -> float:
        """未平仓合约已付的开仓手续费（浮盈里不含它，但现金里已经扣了）。"""
        return sum(s.entry_fee for s in self.shorts.values())

    def check_consistency(self, tol: float = 0.02) -> dict:
        """``equity − initial == 已实现 + 浮盈 − 在仓开仓费`` 的自检（不参与决策）。

        在仓合约的 ``entry_fee`` 已经从现金里扣掉，而 ``unrealized()`` 是从
        ``entry_px`` 起算的净额（不含费），所以等式里要单独减掉这一项。
        """
        lhs = self.equity() - self.initial_cash
        rhs = self.realized() + self.unrealized() - self.open_entry_fees()
        return {"ok": abs(lhs - rhs) <= max(tol, abs(lhs) * 1e-9),
                "equity_minus_initial": round(lhs, 4),
                "realized_plus_unrealized": round(rhs, 4),
                "diff": round(lhs - rhs, 4)}

    # ── 输出 ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "initial_cash": _round2(self.initial_cash),
            "cash": _round2(self.cash),
            "available_cash": _round2(self.available_cash()),
            "collateral_locked": _round2(self.collateral_locked()),
            "etf_value": _round2(self.etf_value()),
            "short_liability": _round2(self.short_liability()),
            "equity": _round2(self.equity()),
            "realized": _round2(self.realized()),
            "unrealized": _round2(self.unrealized()),
            "return_pct": round((self.equity() / self.initial_cash - 1.0) * 100.0, 4),
            "shorts": [s.to_dict() for s in self.shorts.values()],
            "etf": [h.to_dict() for h in self.etf.values()],
            "fills": len(self.fills),
        }

    # ── 持久化（跨重启恢复，供 runner 落盘用） ─────────────

    def to_state(self) -> dict:
        """可完整反解的账本状态（与 ``snapshot()`` 不同：不含派生字段）。"""
        return {
            "version": 1,
            "initial_cash": float(self.initial_cash),
            "cash": float(self.cash),
            "mult": int(self.mult),
            "tick": float(self.tick),
            "fee_open": float(self.fee_open),
            "fee_close": float(self.fee_close),
            "fee_exercise_ratio": float(self.fee_exercise_ratio),
            "shorts": [
                {"code": s.code, "underlying": s.underlying, "strike": s.strike,
                 "expiry": s.expiry, "qty": s.qty, "entry_px": s.entry_px,
                 "entry_ts": s.entry_ts, "mult": s.mult, "entry_fee": s.entry_fee,
                 "mark_px": s.mark_px, "reason": s.reason}
                for s in self.shorts.values()
            ],
            "etf": [
                {"underlying": h.underlying, "qty": h.qty, "cost_basis": h.cost_basis,
                 "since": h.since, "mark": h.mark}
                for h in self.etf.values()
            ],
            "fills": list(self.fills),
        }

    @classmethod
    def from_state(cls, state: dict | None) -> "PaperLedger":
        """从 ``to_state()`` 的输出恢复。残缺/非法的行跳过（不拿猜测补仓）。"""
        st = state if isinstance(state, dict) else {}
        ledger = cls(
            initial_cash=float(st.get("initial_cash") or 500_000.0),
            cash=float(st.get("cash") or 0.0),
            mult=int(st.get("mult") or DEFAULT_MULT),
            tick=float(st.get("tick") if st.get("tick") is not None else DEFAULT_TICK),
            fee_open=float(st.get("fee_open") if st.get("fee_open") is not None
                           else FEE_OPEN_PER_CONTRACT),
            fee_close=float(st.get("fee_close") if st.get("fee_close") is not None
                            else FEE_CLOSE_PER_CONTRACT),
            fee_exercise_ratio=float(st.get("fee_exercise_ratio")
                                     if st.get("fee_exercise_ratio") is not None
                                     else FEE_EXERCISE_RATIO),
        )
        for row in st.get("shorts") or []:
            try:
                sp = ShortPut(
                    code=str(row["code"]), underlying=str(row.get("underlying") or ""),
                    strike=float(row["strike"]), expiry=str(row["expiry"]),
                    qty=int(row["qty"]), entry_px=float(row["entry_px"]),
                    entry_ts=str(row.get("entry_ts") or ""),
                    mult=int(row.get("mult") or ledger.mult),
                    entry_fee=float(row.get("entry_fee") or 0.0),
                    mark_px=float(row.get("mark_px") or 0.0),
                    reason=str(row.get("reason") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if sp.qty > 0 and sp.strike > 0:
                ledger.shorts[sp.code] = sp
        for row in st.get("etf") or []:
            try:
                h = EtfHolding(
                    underlying=str(row["underlying"]), qty=float(row["qty"]),
                    cost_basis=float(row["cost_basis"]),
                    since=str(row.get("since") or ""),
                    mark=float(row.get("mark") or 0.0),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if h.qty > 0:
                ledger.etf[h.underlying] = h
        for row in st.get("fills") or []:
            if isinstance(row, dict):
                ledger.fills.append(dict(row))
        return ledger


def fee_of_premium(px: float, qty: int, mult: int = DEFAULT_MULT,
                   fee_open: float = FEE_OPEN_PER_CONTRACT) -> float:
    """卖出开仓手续费：按张计（用户口径），并受官方「7% × 权利金」上限约束。

    OKX 线的教训是「费用口径错了整批结论作废」；A 股这边官方对卖方同样有
    ``Min(名义 × 费率, 7% × 权利金)`` 的 cap，深虚值薄权利金时按张计反而更贵，
    所以这里取「按张」为下限、不设 cap —— 用户明确给的是 1.2 元/张，先对齐
    实付，真实账单出现偏差再校准。
    """
    return float(fee_open) * int(qty)
