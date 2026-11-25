"""A 股 ETF 期权链快照：合约规格 + 盘口 + 隐含远期 + BS 反解 IV。

为什么不直接用新浪行情里的 IV 字段
----------------------------------
新浪 ``CON_OP_`` 行第 10 字段带 IV，但实测失真且无规律：510050 ATM 字段 33.6%
vs 反解 14.9%；588000 字段 33.9~43.5% vs 反解 52.4%（连两个标的方向都相反）。
故一律自算 —— 先用 put-call parity 求每个到期的**隐含远期 F**，再以 F（不是
ETF 现货价）为标的价反解 BS IV；用现货代替 F 会把基差混进 IV。

口径（与 ``paper_strategy`` / QMT 脚本一致）
-------------------------------------------
- **期权报价单位 = 元/份**（新浪五档的 bid/ask 就是每份价，实测 510050 2.65C
  报 0.1887，而其内在价值 = F−K ≈ 0.19 ⇒ 不可能是「元/张」）。
  1 张 = ``MULT`` = 10,000 份，每张金额 = 报价 × MULT（与
  ``paper_strategy.net_premium`` 同口径）。
- ``bs_pricing`` 同样是「每 1 份」口径 ⇒ 报价与 ``bs_pricing`` 之间**不需要**换算。
- 无风险利率 r = 0（A 股期权惯例）。
- ``dte`` = 剩余自然日（与 ``paper_strategy.days_to_expiry`` 同口径），
  ``t = dte / 365``；``dte <= 0``（当日到期）不反解 IV —— 时间价值趋零、
  反解不稳定，返回 ``None`` 而不是给个假数字。
- parity（r=0、欧式、无分红，单位元/份）：``C − P = F − K`` ⇒ ``F = K + (C−P)``。
  同一到期有多组 (C,P) 时取中位，并按 MAD 剔除离群（深度实值档的报价噪声）。

只读：不写台账、不下单、不参与决策。
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from nanobot_quant import bs_pricing
from nanobot_quant.ashare import contracts as ctr
from nanobot_quant.ashare import tape

CN = timezone(timedelta(hours=8))

MULT = 10_000
R_FREE = 0.0
DAYS_PER_YEAR = 365.0

DEFAULT_EXPIRIES = 3
DEFAULT_BAND_PCT = 12.0
CACHE_TTL_S = 15.0
MAD_K = 3.0                      # 离群阈值：|F − median| > 3×MAD 剔除
MIN_TOL_RATIO = 0.002            # MAD 为 0 时的兑底容差（中位的 0.2%）

# 进程内快照缓存：{(undl, expiries, band_pct): {"_at": ts, "payload": {...}}}
_CACHE: dict[tuple, dict] = {}


# ── 小工具 ──────────────────────────────────────────────────

def _f(v) -> float:
    try:
        x = float(v)
        return x if x == x else 0.0          # NaN → 0
    except (TypeError, ValueError):
        return 0.0


def median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def mid_of(row: dict) -> Optional[float]:
    """中价（元/张）。双边齐全 → 均值；只有一边 → 用该边（标注由调用方做）。"""
    bid = _f(row.get("bid"))
    ask = _f(row.get("ask"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return bid if bid > 0 else (ask if ask > 0 else None)


def full_mid_of(row: dict) -> Optional[float]:
    """严格中价：必须双边齐全（parity / IV 用这个，不用单边）。"""
    bid = _f(row.get("bid"))
    ask = _f(row.get("ask"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


def years_of(dte: int) -> float:
    """剩余年化期限（自然日/365）；非正 → 0.0（调用方据此跳过反解）。"""
    try:
        d = int(dte)
    except (TypeError, ValueError):
        return 0.0
    return (d / DAYS_PER_YEAR) if d > 0 else 0.0


# ── 隐含远期（put-call parity）───────────────────────────────

def implied_forward(rows: list[dict]) -> dict:
    """同一到期的隐含远期。

    ``rows``：``[{"strike", "right", "mid"}]``（mid 为元/张、必须双边中价）。
    返回 ``{"f", "n", "n_pairs", "mad_pct", "outliers"}``：
    ``f=None`` 表示无法计算（该到期没有成对的双边 C/P）。
    """
    calls, puts = {}, {}
    for r in rows:
        mid = r.get("mid")
        if mid is None or mid <= 0:
            continue
        k = _f(r.get("strike"))
        if k <= 0:
            continue
        (calls if str(r.get("right", "")).upper() == "C" else puts)[k] = mid

    pairs = []
    for k in sorted(set(calls) & set(puts)):
        f_k = k + (calls[k] - puts[k])        # 元/份口径，无需除 MULT
        if f_k > 0:
            pairs.append((k, f_k))

    if not pairs:
        return {"f": None, "n": 0, "n_pairs": 0, "mad_pct": None, "outliers": 0}

    fs = [v for _, v in pairs]
    med = median(fs)
    mad = median([abs(v - med) for v in fs]) or 0.0
    # MAD 为 0（大多数 pair 完全一致）时仍要能剔单点污染，否则一个离群对会直接
    # 拉动中位（真实数据里不同 strike 的 F 离散通常 < 0.2%）。
    tol = max(MAD_K * mad, abs(med) * MIN_TOL_RATIO)
    kept = [(k, v) for k, v in pairs if abs(v - med) <= tol]
    outliers = len(pairs) - len(kept)
    f = median([v for _, v in kept])
    return {
        "f": f,
        "n": len(kept),
        "n_pairs": len(pairs),
        "mad_pct": (mad / f * 100.0) if f else None,
        "outliers": outliers,
    }


# ── IV / delta ──────────────────────────────────────────────

def iv_of(px: Optional[float], f: float, strike: float, t: float,
          right: str) -> Optional[float]:
    """元/份报价 → BS 反解 IV（小数）。任一输入无效 → None。"""
    if px is None or px <= 0 or f <= 0 or strike <= 0 or t <= 0:
        return None
    return bs_pricing.implied_vol(px, f, strike, t, right, r=R_FREE)


def delta_of(f: float, strike: float, t: float, iv: Optional[float],
             right: str) -> Optional[float]:
    if not iv or iv <= 0 or f <= 0 or strike <= 0 or t <= 0:
        return None
    return bs_pricing.bs_delta(f, strike, t, iv, r=R_FREE, right=right)


# ── 快照 ────────────────────────────────────────────────────

def _pick_expiries(items: list[dict], today: str, n: int) -> list[str]:
    exps = sorted({str(it.get("expire") or "") for it in items})
    exps = [e for e in exps if len(e) == 8 and e >= today]
    return exps[:max(1, int(n))]


def _build_rows(items: list[dict], qmap: dict, today: date) -> list[dict]:
    rows: list[dict] = []
    for it in items:
        code = str(it.get("code") or "")
        q = qmap.get(code) or {}
        bid = _f(q.get("b")) or None
        ask = _f(q.get("a")) or None
        rows.append({
            "code": code,
            "right": str(it.get("right") or "").upper(),
            "strike": _f(it.get("strike")),
            "expiry": str(it.get("expire") or ""),
            "dte": _f(_dte_days(it.get("expire"), today)),
            "bid": bid,
            "ask": ask,
            "mid": mid_of({"bid": bid, "ask": ask}),
            "full_mid": full_mid_of({"bid": bid, "ask": ask}),
            "last": _f(q.get("last")) or None,
            "oi": int(_f(q.get("oi"))),
            "vol": int(_f(q.get("vol"))),
            "quote_ts": str(q.get("ts") or ""),
            "iv": None, "iv_bid": None, "iv_ask": None,
            "delta": None, "moneyness_pct": None, "otm_pct": None,
            "spread_pct": None, "f_src": "",
        })
    return rows


def _dte_days(expire, today: date) -> int:
    try:
        d = datetime.strptime(str(expire), "%Y%m%d").date()
    except (TypeError, ValueError):
        return 0
    return (d - today).days


def _enrich(rows: list[dict], forwards: dict) -> None:
    """补 IV / delta / 虚值度（就地修改）。"""
    for r in rows:
        fw = forwards.get(r["expiry"]) or {}
        f = fw.get("f")
        k = r["strike"]
        t = years_of(r["dte"])
        bid, ask, mid = r["bid"], r["ask"], r["mid"]
        if f and k > 0:
            r["moneyness_pct"] = (k / f - 1.0) * 100.0
            if r["right"] == "P":
                r["otm_pct"] = (f - k) / f * 100.0
            else:
                r["otm_pct"] = (k - f) / f * 100.0
        if mid and f and k > 0 and t > 0:
            r["iv"] = iv_of(mid, f, k, t, r["right"])
            if bid:
                r["iv_bid"] = iv_of(bid, f, k, t, r["right"])
            if ask:
                r["iv_ask"] = iv_of(ask, f, k, t, r["right"])
            r["delta"] = delta_of(f, k, t, r["iv"], r["right"])
            r["f_src"] = "parity"
        if bid and ask and mid:
            r["spread_pct"] = (ask - bid) / mid * 100.0
        # 单边 / 无 F 时也给出折算参考价的对数虚值度（不含 IV）
        if not r["f_src"]:
            r["f_src"] = "spot" if f else ""


def chain_snapshot(undl: str, storage_dir, *, expiries: int = DEFAULT_EXPIRIES,
                   band_pct: float = DEFAULT_BAND_PCT, today: date | None = None,
                   now: datetime | None = None, force: bool = False,
                   ttl: float = CACHE_TTL_S, timeout: float = 12.0) -> dict:
    """期权链快照（只读）。

    返回：``{symbol, spot, source, asof, in_session, cached, frozen, age_s,
    expiries, forwards, rows, errors, note}``。

    缓存语义：进程内 TTL（默认 15s）内直接复用；非交易时段**不重拉**
    （数据不会再变），返回上次结果并标 ``frozen=True``；``force=True``
    （页面手动刷新 / 交易时段首次）仍真拉。
    """
    now_dt = now or tape.now_cn()
    in_sess = tape.in_session(now_dt)
    key = (str(undl), int(expiries), round(float(band_pct), 3))
    hit = _CACHE.get(key)
    if hit and not force:
        age = time.time() - hit["_at"]
        if age < ttl:
            return {**hit["payload"], "cached": True, "age_s": int(age)}
        if not in_sess:
            return {**hit["payload"], "cached": True, "frozen": True,
                    "age_s": int(age)}

    today_d = today or now_dt.date()
    today_s = today_d.strftime("%Y%m%d")
    note = ""

    snap = ctr.contracts(storage_dir, force=force, today=today_d)
    items = [it for it in (snap.get("items") or [])
             if str(it.get("undl") or "") == undl]
    exps = _pick_expiries(items, today_s, expiries)
    items = [it for it in items if str(it.get("expire") or "") in exps]

    spots = tape.fetch_spot([undl])
    spot = _f(spots.get(undl))
    if spot <= 0:
        note = "取不到标的现价，未按虚值带裁剪"
    else:
        band = max(0.0, float(band_pct)) / 100.0
        items = [it for it in items
                 if abs(_f(it.get("strike")) - spot) / spot <= band]

    quotes, qerr = tape.fetch_quotes([str(it.get("code") or "") for it in items],
                                     timeout=timeout)
    qmap = {str(q.get("i") or "").split(".")[0]: q for q in quotes}
    rows = _build_rows(items, qmap, today_d)

    by_exp: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_exp[r["expiry"]].append(
            {"strike": r["strike"], "right": r["right"], "mid": r["full_mid"]})
    forwards = {e: implied_forward(rs) for e, rs in by_exp.items()}
    _enrich(rows, forwards)

    rows.sort(key=lambda r: (r["expiry"], r["strike"], r["right"]))
    asof = ""
    for r in rows:
        if r["quote_ts"]:
            asof = max(asof, r["quote_ts"])
    if not asof:
        asof = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "symbol": str(undl),
        "spot": spot,
        "source": snap.get("source") or "",
        "contracts_error": snap.get("error") or "",
        "asof": asof,
        "in_session": in_sess,
        "cached": False,
        "frozen": False,
        "age_s": 0,
        "expiries": exps,
        "forwards": forwards,
        "rows": rows,
        "errors": qerr,
        "note": note,
    }
    _CACHE[key] = {"_at": time.time(), "payload": payload}
    return payload


def clear_cache() -> None:
    _CACHE.clear()


__all__ = [
    "MULT", "R_FREE", "DEFAULT_EXPIRIES", "DEFAULT_BAND_PCT", "CACHE_TTL_S",
    "median", "mid_of", "full_mid_of", "years_of", "implied_forward",
    "iv_of", "delta_of", "chain_snapshot", "clear_cache",
]


# ── 自测入口（真实数据，只读）───────────────────────────────

def _main() -> None:                                   # pragma: no cover
    import sys
    undl = sys.argv[1] if len(sys.argv) > 1 else "510050.SH"
    storage = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ashare_creds"
    snap = chain_snapshot(undl, storage, force=True)
    print(f"[CHAIN] {snap['symbol']} spot={snap['spot']:.4f} "
          f"source={snap['source']} asof={snap['asof']} "
          f"in_session={snap['in_session']} errors={snap['errors']}")
    for e in snap["expiries"]:
        fw = snap["forwards"].get(e) or {}
        f = fw.get("f")
        print(f"  expiry={e} forward={f if f is None else round(f, 4)} "
              f"pairs={fw.get('n')}/{fw.get('n_pairs')} "
              f"mad%={None if fw.get('mad_pct') is None else round(fw['mad_pct'], 3)}")
    print(f"{'code':<12}{'R':<2}{'strike':>8}{'dte':>5}{'bid':>9}{'ask':>9}"
          f"{'iv':>9}{'delta':>9}{'otm%':>9}")
    for r in snap["rows"]:
        if r["mid"] is None:
            continue
        iv = "—" if r["iv"] is None else f"{r['iv'] * 100:8.2f}%"
        dl = "—" if r["delta"] is None else f"{r['delta']:9.4f}"
        om = "—" if r["otm_pct"] is None else f"{r['otm_pct']:9.2f}"
        print(f"{r['code']:<12}{r['right']:<2}{r['strike']:>8.4f}{int(r['dte']):>5}"
              f"{r['bid'] or 0:>9.4f}{r['ask'] or 0:>9.4f}{iv:>9}{dl:>9}{om:>9}")


if __name__ == "__main__":                             # pragma: no cover
    _main()
