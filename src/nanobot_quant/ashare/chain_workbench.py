"""期权链工作台：把「期权链 → 选档 → IV 分位 → 额度」拼成一张候选表。

纯函数、无 IO、无状态 —— 页面 handler 组装输入（链快照行 / 参数 / 两臂持仓 /
IV 分位查表回调），渲染输出。

选档规则**不在这里实现**：直接调 ``paper_strategy.rank_puts()``，与模拟账本
同一份代码。否则页面会推荐账本根本不会买的那张，看到的就是假象。

口径（与账本、``trades_md`` 保持一致）
------------------------------------
- 净收益率：``(bid×面值 − 开仓费) ÷ 担保额``，分母是接货真正占用的钱；
- 往返后收益率：再扣买入平仓费 + 1 tick 价差，**仅展示、不参与排序**；
- 打平回落%：往返成本 ÷ 开仓权利金 —— 权利金跌到这条线以下，这一轮就是白干；
- 虚值%：``(现货 − strike) ÷ 现货``，与 ``min_distance_pct`` 门槛**同源**（链行自带的
  ``otm_pct`` 用隐含远期 F 算，那是 IV/Δ 的口径，不能拿来展示门槛）；
- ATM 分位：该到期档 **ATM put IV**（``|Δ|`` 最小那张）在同 DTE 桶历史样本中的分位。
  历史序列只存 ATM IV，拿单张深虚值合约的 IV 去比会因 skew 系统性落在高位，
  桶无样本 → ``None``。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .paper_strategy import (FEE_CLOSE_PER_CONTRACT, PaperParams, rank_puts)

TICK_COST_YUAN = 1.0    # 1 tick 价差近似（元/张）。2026-11-22 盘口实测 1–3 tick，取下界
CN = timezone(timedelta(hours=8))
ARM_LABELS = {"A": "A", "B": "B"}


def cn_today() -> date:
    return datetime.now(CN).date()


def _arm_status(shorts: list[dict], params: PaperParams, underlying: str,
                code: str) -> dict:
    """单臂能否开这一张：已持有 → 本标的满 → 总额满 → 可开（顺序固定）。"""
    held = {str(s.get("code") or "") for s in shorts}
    if code in held:
        return {"ok": False, "label": "已持有"}
    same = sum(1 for s in shorts
               if str(s.get("underlying") or "") == underlying)
    if same >= params.max_per_underlying:
        return {"ok": False, "label": f"本标的满 {same}/{params.max_per_underlying}"}
    if len(shorts) >= params.max_total:
        return {"ok": False, "label": f"总额满 {len(shorts)}/{params.max_total}"}
    return {"ok": True, "label": "可开"}


def _atm_iv_ref(puts: list[dict], expiry: str, spot: float) -> Optional[float]:
    """该到期档的 ATM put IV：取 strike 最接近现货那张，缺 strike 时用 ``|Δ|`` 距 0.5 最近。

    分位基准必须是 ATM —— ``iv_series`` 历史每桶只存 ATM IV。不能用「``|Δ|`` 最小」
    （那是**最虚值**那张，skew 最高）也不用单张合约自己的 IV，否则分位恒落在高位。
    """
    pool = [r for r in puts
            if (not expiry or str(r.get("expiry") or "") == expiry)]
    best_key: Optional[tuple] = None
    best: Optional[float] = None
    for r in pool:
        iv = r.get("iv")
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        try:
            strike = float(r.get("strike") or 0.0)
        except (TypeError, ValueError):
            strike = 0.0
        d = r.get("delta")
        if strike > 0 and spot > 0:
            key = (0, abs(strike - float(spot)))
        else:
            try:
                key = (1, abs(abs(float(d)) - 0.5)) if d is not None else (2, 0.0)
            except (TypeError, ValueError):
                key = (2, 0.0)
        if best_key is None or key < best_key:
            best_key, best = key, iv
    return best


def build_candidates(rows: list[dict], spot: float, today: Optional[date] = None,
                     params: Optional[PaperParams] = None, *,
                     expiry: str = "",
                     underlying: str = "",
                     arm_shorts: Optional[dict[str, list[dict]]] = None,
                     iv_pct_fn: Optional[Callable[[object, object], Optional[float]]] = None,
                     iv_src: str = "",
                     ) -> dict:
    """候选表。``rows`` = 链快照的全部行（含 call，内部只取 put）。

    返回 ``{rows, pick, pick_other_expiry, params}``：
    - ``rows`` 只含 ``expiry == expiry`` 的候选（跟随页面当前到期 tab）；
    - ``pick`` = 自动线此刻会选的那张（**全链**选档结果，可能不在当前 tab）；
    - ``pick_other_expiry`` 非空表示「自动线选的是别的到期档」，页面要提示，
      不能悄悄不标星。
    """
    p = params or PaperParams()
    today = today or cn_today()
    puts = [r for r in (rows or [])
            if str(r.get("right") or "").upper() == "P"]
    ranked = rank_puts(puts, spot, today, p)
    pick = ranked[0] if ranked else None
    pick_code = str(pick.get("code") or "") if pick else ""
    pick_expiry = str(pick.get("expiry") or "") if pick else ""
    arms = arm_shorts or {}
    # (到期, dte) → (ATM 参考 IV, 该档分位)。同到期多行同一个分位，只算一次
    pct_cache: dict[tuple[str, int], tuple[Optional[float], Optional[float]]] = {}

    out = []
    for c in ranked:
        code = str(c.get("code") or "")
        if expiry and str(c.get("expiry") or "") != expiry:
            continue
        strike = float(c.get("strike") or 0.0)
        bid = float(c.get("bid") or 0.0)
        dte = int(c.get("dte") or 0)
        collateral = strike * p.mult
        np_ = float(c.get("net_premium") or 0.0)
        rt_cost = FEE_CLOSE_PER_CONTRACT + TICK_COST_YUAN
        rt_pct = (np_ - rt_cost) / collateral * 100.0 if collateral > 0 else None
        breakeven = rt_cost / (bid * p.mult) * 100.0 if bid > 0 else None
        ann = (float(c.get("net_yield_pct") or 0.0) * 365.0 / dte) if dte > 0 else None
        iv = c.get("iv")
        exp = str(c.get("expiry") or "")
        ck = (exp, dte)
        if ck not in pct_cache:
            ref = _atm_iv_ref(puts, exp, spot)
            pct_cache[ck] = (ref, iv_pct_fn(ref, dte)
                             if (iv_pct_fn and ref is not None) else None)
        atm_iv, iv_pct = pct_cache[ck]
        # 虚值% 用现货算 —— 与 min_distance_pct 门槛同源；链行的 otm_pct 是 F 口径
        try:
            otm_pct = ((float(spot) - strike) / float(spot) * 100.0
                       if spot and strike > 0 else None)
        except (TypeError, ValueError):
            otm_pct = None
        undl = underlying or str(c.get("underlying") or "")
        out.append({
            "code": code,
            "strike": strike,
            "expiry": exp,
            "dte": dte,
            "bid": bid,
            "iv": iv,
            "iv_pct": iv_pct,
            "atm_iv": atm_iv,
            "delta": c.get("delta"),
            "otm_pct": otm_pct,
            "net_premium": np_,
            "net_yield_pct": c.get("net_yield_pct"),
            "annual_pct": ann,
            "collateral": collateral,
            "round_trip_pct": rt_pct,
            "breakeven_drop_pct": breakeven,
            "pick": bool(pick_code and code == pick_code),
            "arms": {a: _arm_status(arms.get(a) or [], p, undl, code)
                     for a in ARM_LABELS},
        })
    return {
        "rows": out,
        "pick": pick_code,
        "pick_other_expiry": pick_expiry if (pick_expiry and expiry
                                             and pick_expiry != expiry) else "",
        "params": {
            "min_dte": p.min_dte, "max_dte": p.max_dte,
            "min_distance_pct": p.min_distance_pct, "band_pct": p.band_pct,
            "max_per_underlying": p.max_per_underlying,
            "max_total": p.max_total, "force_close_dte": p.force_close_dte,
            "fee_open": p.fee_open, "fee_close": FEE_CLOSE_PER_CONTRACT,
            "tick_cost": TICK_COST_YUAN, "mult": p.mult,
            "iv_pct_basis": "atm_put", "otm_basis": "spot", "iv_src": iv_src,
        },
    }
