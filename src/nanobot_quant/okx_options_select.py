"""卖 put 候选选择（C24 合约选择机制，设计见 docs/quant-system.md 33.26）。

从 OKX 期权链按「选择参数」挑出可卖的 put 合约：

1. 硬过滤：到期天数窗口 → 有买盘（bid > 0，卖得出去）→ ``min_distance_pct``
   （strike ≤ 基准价 ×(1−距离%)，安全边际优先）→ delta 带（控制被行权概率）；
2. 排序：净收益率（默认，= 净权利金 ÷ 担保额）→ |delta − 0.25| 距离；

口径（2026-09-15 定稿，见 33.25）：**净权利金 = bid × 每张面值 − 名义 × 0.03% 手续费**
（``OPTION_FEE_RATE_TAKER``），担保额 = strike × 每张面值；卖出吃买盘用 bid（非 ask）。

参数存 ``option_params.json`` 的 ``selector`` 字段（与担保比例 / 到期巡检同文件）。
"""

from __future__ import annotations

DEFAULT_SELECTOR: dict = {
    "min_distance_pct": 5.0,     # strike 必须 ≤ 基准价×(1−该百分比)；0 = 关闭硬过滤
    "expiry_min_days": 3.0,      # 到期天数窗口下限
    "expiry_max_days": 7.0,      # 到期天数窗口上限
    "delta_min": 0.05,           # delta 带下限（绝对值）；默认宽松（低 delta 更安全；
                                 # 上限才是风险控制，「保费太薄」交给 min_net_yield_pct）
    "delta_max": 0.35,           # delta 带上限
    "min_net_yield_pct": 0.0,    # 净收益率下限（0 = 关闭；>0 时低于该值不出候选）
    "top_n": 5,                  # 返回候选数
    "sort_by": "net_yield",      # net_yield | net_premium | apr
}
SORT_MODES = ("net_yield", "net_premium", "apr")
_DELTA_TARGET = 0.25             # 排序次键：越接近该 delta 越靠前

# validate_selector 的值域（保存时严格校验，读取时钳制）
_LIMITS = {
    "min_distance_pct": (0.0, 50.0),
    "expiry_min_days": (0.0, 180.0),
    "expiry_max_days": (0.0, 180.0),
    "delta_min": (0.0, 1.0),
    "delta_max": (0.0, 1.0),
    "min_net_yield_pct": (0.0, 10.0),
    "top_n": (1, 20),
}


def _num(v, d, lo=None, hi=None):
    """宽松数值解析：非法/NaN → 默认值；超出值域 → 钳制。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return d
    if x != x:                    # NaN
        return d
    if lo is not None:
        x = max(x, lo)
    if hi is not None:
        x = min(x, hi)
    return x


def selector_params(raw: dict | None = None) -> dict:
    """完整选择参数：raw（或磁盘 ``option_params.json`` 的 selector 字段）→ 默认 → 钳制。"""
    src = raw if isinstance(raw, dict) else {}
    if not src:
        from .okx_options_trade import load_option_params
        src = load_option_params().get("selector") or {}
        if not isinstance(src, dict):
            src = {}
    s = {}
    for k, (lo, hi) in _LIMITS.items():
        s[k] = int(_num(src.get(k), DEFAULT_SELECTOR[k], lo, hi)) if k == "top_n" else \
            _num(src.get(k), DEFAULT_SELECTOR[k], lo, hi)
    s["sort_by"] = src.get("sort_by") if src.get("sort_by") in SORT_MODES else DEFAULT_SELECTOR["sort_by"]
    if s["expiry_max_days"] < s["expiry_min_days"]:
        s["expiry_max_days"] = s["expiry_min_days"]
    if s["delta_max"] < s["delta_min"]:
        s["delta_max"] = s["delta_min"]
    return s


def validate_selector(raw: dict) -> tuple[dict | None, str | None]:
    """保存前严格校验（非法值明确报错，不静默钳制）。返回 (cleaned, error)。"""
    if not isinstance(raw, dict):
        return None, "选择参数格式错误（需 JSON 对象）"
    out = {}
    for k, (lo, hi) in _LIMITS.items():
        if k not in raw or raw.get(k) in (None, ""):
            out[k] = DEFAULT_SELECTOR[k]
            continue
        try:
            x = float(raw[k])
        except (TypeError, ValueError):
            return None, f"{k} 必须是数字"
        if x != x:
            return None, f"{k} 不能为 NaN"
        if not lo <= x <= hi:
            return None, f"{k} 须在 {lo:g}–{hi:g} 之间（当前 {x:g}）"
        if k == "top_n" and x != int(x):
            return None, "top_n 必须是整数"
        out[k] = int(x) if k == "top_n" else x
    sb = raw.get("sort_by") or DEFAULT_SELECTOR["sort_by"]
    if sb not in SORT_MODES:
        return None, f"sort_by 只能是 {', '.join(SORT_MODES)}"
    out["sort_by"] = sb
    if out["expiry_max_days"] < out["expiry_min_days"]:
        return None, "到期天数上限不能小于下限"
    if out["delta_max"] < out["delta_min"]:
        return None, "delta 上限不能小于下限"
    return out, None


def select_puts(family: str, base_px: float | None = None,
                selector: dict | None = None, chain: dict | None = None,
                exp_ms=None) -> dict:
    """按选择参数挑卖 put 候选。

    - ``base_px``：基准价（默认标的实时现价）；
    - ``chain``：注入期权链数据（测试用），None 时按到期窗口拉 OKX 链；
    - ``exp_ms``：锁定到期档（毫秒时间戳）——候选跟随期权链页当前 tab，指定时
      只在该到期里挑、跳过「到期天数窗口」（窗口退化为组合档/全部档的默认值）；
    - 返回 {family, base_px, spot, lot_coin, selector, candidates, scanned, filtered, note,
      expiry_mode, expiry_locked_ms}。
    """
    from . import okx_options_data as od
    from .okx_options_trade import OPTION_FEE_RATE_TAKER

    sel = selector_params(selector)
    lo, hi = sel["expiry_min_days"], sel["expiry_max_days"]
    note = ""
    lock_ms = None
    if exp_ms not in (None, ""):
        try:
            lock_ms = int(exp_ms)
        except (TypeError, ValueError):
            lock_ms = None
    if chain is None:
        if lock_ms is not None:
            chain = od.fetch_chain(family, expiries=[lock_ms])     # 锁定到期：跟随链 tab
        else:
            exps = od.list_expiries(family)
            pick = [e for e in exps if lo <= e.get("days", -1) <= hi]
            if not pick:
                pick = exps[:3]
                if pick:
                    note = f"窗口内（{lo:g}–{hi:g} 天）无在售到期，已放宽为最近 {len(pick)} 个到期"
            chain = od.fetch_chain(family, expiries=[e["exp_ms"] for e in pick])

    spot = chain.get("spot")
    base = float(base_px) if base_px else spot
    lot = chain.get("lot_coin") or 0.0
    f_rate = OPTION_FEE_RATE_TAKER

    cands: list[dict] = []
    filtered = {"expiry": 0, "no_bid": 0, "distance": 0, "delta": 0, "net": 0, "yield": 0,
                "no_lot": 0}
    for g in chain.get("groups", []):
        days = g.get("days")
        if lock_ms is not None:
            if str(g.get("exp_ms")) != str(lock_ms):                # 锁定档：只留该到期
                filtered["expiry"] += 1
                continue
        elif days is None or not (lo <= days <= hi):
            filtered["expiry"] += 1
            continue
        for row in g.get("rows", []):
            cell = (row or {}).get("P") or {}
            inst = cell.get("inst_id")
            if not inst:
                continue
            if not lot:
                filtered["no_lot"] += 1
                continue
            bid = cell.get("bid")
            if bid is None or bid <= 0:          # 无买盘 = 卖不出去
                filtered["no_bid"] += 1
                continue
            strike = float(row["strike"])
            if base and sel["min_distance_pct"] > 0 and \
                    strike > base * (1 - sel["min_distance_pct"] / 100.0):
                filtered["distance"] += 1
                continue
            delta = cell.get("delta")
            ad = abs(delta) if delta is not None else None
            if ad is not None and (sel["delta_min"] > 0 or sel["delta_max"] > 0) and \
                    not (sel["delta_min"] <= ad <= sel["delta_max"]):
                filtered["delta"] += 1
                continue
            notional = strike * lot
            prem = bid * lot
            fee = notional * f_rate
            net = prem - fee
            if net <= 0:                          # 扣手续费后无利可图（薄权利金）
                filtered["net"] += 1
                continue
            net_yield = net / notional * 100 if notional else 0.0
            if sel["min_net_yield_pct"] > 0 and net_yield < sel["min_net_yield_pct"]:
                filtered["yield"] += 1
                continue
            cands.append({
                "inst_id": inst,
                "strike": strike,
                "exp_ms": g.get("exp_ms"),
                "date": g.get("date"),
                "days": days,
                "bid": bid,
                "ask": cell.get("ask"),
                "iv": cell.get("iv"),
                "delta": delta,
                "lot_coin": lot,
                "notional_usd": round(notional, 6),
                "premium_usd": round(prem, 8),
                "fee_usd": round(fee, 8),
                "net_premium_usd": round(net, 8),
                "net_yield_pct": round(net_yield, 4),
                "apr_pct": round(net / notional * 100 * 365 / max(float(days), 0.5), 2)
                if notional else None,
                "delta_gap": round(abs(ad - _DELTA_TARGET), 4) if ad is not None else 9.9,
            })

    keys = {
        "net_yield": lambda c: (-(c["net_yield_pct"] or 0.0), c["delta_gap"]),
        "net_premium": lambda c: (-(c["net_premium_usd"] or 0.0), c["delta_gap"]),
        "apr": lambda c: (-(c["apr_pct"] or 0.0), c["delta_gap"]),
    }
    cands.sort(key=keys[sel["sort_by"]])
    return {
        "family": family,
        "base_px": base,
        "spot": spot,
        "lot_coin": lot,
        "selector": sel,
        "candidates": cands[:sel["top_n"]],
        "scanned": len(cands),
        "filtered": filtered,
        "note": note,
        "expiry_mode": "locked" if lock_ms is not None else "window",
        "expiry_locked_ms": lock_ms,
    }
