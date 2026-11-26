"""QMT 1m 历史行情 → IV 时间序列 + 分位统计（只读）。

为什么从 K 线自算
-----------------
QMT 导出的 1m 分片只有 ``ts,code,open,high,low,close,volume,amount``，没有 IV
列；新浪行内的 IV 字段实测失真（见 ``option_chain`` 模块 docstring）。但每个
到期的 C/P 收盘价已足够：先用 put-call parity 求隐含远期 F，再以 F 为标的价
BS 反解 IV —— 与实盘 ``option_chain`` 同一套口径，故历史分位与当前快照可直接
比较。

用途（只读，不进交易路径）
--------------------------
1. ``iv_series_from_bars()``：分片 → 每 (分钟, 到期) 一行的 IV 序列
   （ATM IV、25-delta skew、F、配对质量、是否陈旧）。
2. ``iv_percentile()`` / ``bucket_table()``：按 DTE 分桶的 IV 分位表。
   **有效独立样本按「天」计** —— 分钟级 IV 高度自相关，52 个交易日就是 52 个
   独立单元，不能拿分钟数当样本量。

口径要点
--------
- 期权报价单位 = 元/份（同 ``option_chain``）；``MULT`` 只用于金额换算，
  parity 与 BS 反解都不除 MULT。
- r = 0；``dte`` = 到期日 15:00 − bar 时刻（自然日，可为小数）。
- ``volume == 0`` 的分钟是「无成交、价格延续」，IV 照样能算但不代表市场，
  由 ``stale`` 字段标出、交给调用方决定是否剔除。
- 深实值/深虚值档 IV 不可靠（|delta| 大时 1 tick 能摆动数 pp）⇒ ATM IV 只取
  |K−F| 在 ``band_pct`` 内的档，25-delta 档只取虚值侧。
"""

from __future__ import annotations

import csv
import glob
import gzip
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Iterator, Optional

from nanobot_quant import bs_pricing
from nanobot_quant.ashare import option_chain as oc

MULT = oc.MULT
DAYS_PER_YEAR = oc.DAYS_PER_YEAR

DEFAULT_BAND_PCT = 12.0          # ATM/25d 取档带宽（|K/F−1| 百分比）
DEFAULT_MIN_PAIRS = 3            # parity 求 F 的最少配对数
DEFAULT_MAX_IV = 3.0             # IV 上限（300%），超出视为反解失败
F_NOISY_MAD_PCT = 0.5            # parity 离散度上限（%），超出标 f_noisy
DTE_BUCKETS = ((0, 3), (4, 7), (8, 15), (16, 25), (26, 40))
EXPIRY_HOUR = 15                 # 到期日按 15:00 收盘结算

_SHARD_RE = re.compile(r"opt1m_([A-Z0-9]+)_(\d{6})_p(\d+)\.csv(\.gz)?$")

QUALITY_OK = "ok"
# 失败原因（互斥、按优先级）：过期 → 无配对 → 无 F → F 噪声大 → IV 反解失败 → IV 越界
QUALITY_FAILS = ("expired", "no_pair", "no_f", "f_noisy", "iv_fail", "iv_extreme")


# ── 小工具 ──────────────────────────────────────────────────

def compact_undl(undl: str) -> str:
    """``510050.SH`` → ``510050SH``（QMT 分片文件名用的紧凑写法）。"""
    return str(undl).replace(".", "").replace("-", "").upper()


def parse_ts(ts: str) -> Optional[datetime]:
    """QMT 的 ``YYYYMMDDHHMMSS`` → datetime；格式不对返回 None。"""
    try:
        return datetime.strptime(str(ts)[:14], "%Y%m%d%H%M%S")
    except (TypeError, ValueError):
        return None


def dte_at(ts: str, expire: str) -> float:
    """bar 时刻到到期日 15:00 的自然日数（小数）；解析失败 → 0.0。"""
    t0 = parse_ts(ts)
    if t0 is None:
        return 0.0
    try:
        exp = datetime.strptime(str(expire)[:8], "%Y%m%d").replace(hour=EXPIRY_HOUR)
    except (TypeError, ValueError):
        return 0.0
    return (exp - t0).total_seconds() / 86400.0


def _years(dte: float) -> float:
    """小数 DTE → 年化期限。**不走 ``oc.years_of``**（那个按整天 int 截断，
    会静默改变 t、把 IV 抬高几成）。"""
    try:
        d = float(dte)
    except (TypeError, ValueError):
        return 0.0
    return (d / DAYS_PER_YEAR) if d > 0 else 0.0


def _float(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _open_text(path):
    p = str(path)
    if p.endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8-sig", newline="")
    return open(p, "r", encoding="utf-8-sig", newline="")


# ── 读数据 ──────────────────────────────────────────────────

def load_contracts(source) -> dict:
    """读 ``contracts_all.csv``（支持目录 / 直给文件 / .gz）。

    返回 ``{code: {undl, month, right, strike, expire, mult, tick}}``。
    """
    p = str(source)
    if os.path.isdir(p):
        cand = os.path.join(p, "contracts_all.csv")
        if not os.path.exists(cand) and os.path.exists(cand + ".gz"):
            cand += ".gz"
        p = cand
    out: dict[str, dict] = {}
    with _open_text(p) as fh:
        for r in csv.DictReader(fh):
            code = (r.get("code") or "").strip()
            if not code:
                continue
            out[code] = {
                "undl": compact_undl(r.get("undl") or ""),
                "month": (r.get("month") or "").strip(),
                "right": (r.get("type") or "").strip().upper(),
                "strike": _float(r.get("strike")),
                "expire": (r.get("expire") or "").strip(),
                "mult": _int(r.get("mult"), MULT),
                "tick": _float(r.get("tick")),
            }
    return out


def shard_paths(run_dir, undl: str, months=None) -> list[str]:
    """某标的的 1m 分片路径（``opt1m_<UNDL>_<month>_pNN.csv[.gz]``，按名字排序）。"""
    want = compact_undl(undl)
    out = []
    for path in sorted(glob.glob(os.path.join(str(run_dir), "opt1m_*_p*.csv*"))):
        m = _SHARD_RE.search(os.path.basename(path))
        if not m or m.group(1) != want:
            continue
        if months and m.group(2) not in months:
            continue
        out.append(path)
    return out


def _iter_one(path, days=None) -> Iterator[tuple]:
    with _open_text(path) as fh:
        for r in csv.DictReader(fh):
            ts = (r.get("ts") or "").strip()
            if not ts:
                continue
            if days is not None and ts[:8] not in days:
                continue
            close = _float(r.get("close"))
            if close <= 0:
                continue
            yield ts, (r.get("code") or "").strip(), close, _float(r.get("volume"))


def load_bar_matrix(paths, *, days=None) -> tuple:
    """读全部分片 → ``(ts_sorted, {code: {ts: (close, vol)}})``。

    **为什么不能流式归并**：QMT 导出是按**合约分块**写入的（一个合约连续写完
    再写下个），文件内部并不按 ts 排序 —— 曾用 ``heapq.merge`` 按 ts 归并，
    结果同一分钟的行被拆到 26 个 bucket 里，每行 ``n_pairs=0``。
    """
    ts_set: set[str] = set()
    series: dict[str, dict] = {}
    for path in paths:
        for ts, code, close, vol in _iter_one(path, days):
            series.setdefault(code, {})[ts] = (close, vol)
            ts_set.add(ts)
    return sorted(ts_set), series


# ── 单分钟求值 ──────────────────────────────────────────────

def _pick_atm(calls_iv: dict, puts_iv: dict, f: float):
    """最接近 F 的档（最多试两档）；call/put 双侧有效时取均值。"""
    strikes = sorted(set(calls_iv) | set(puts_iv), key=lambda k: (abs(k - f), k))
    for k in strikes[:2]:
        vals = [v for v in (calls_iv.get(k), puts_iv.get(k)) if v]
        if vals:
            return k, sum(vals) / len(vals), len(vals),
    return None, None, 0


def _pick_otm_by_delta(prices: dict, f: float, t: float, sigma: float,
                       right: str, target: float, band_pct: float = 100.0):
    """用 sigma 估算各档 |delta|，取虚值侧最接近 target 的档（返回 (k, px)）。"""
    best = None
    for k, px in prices.items():
        if abs(k / f - 1.0) * 100.0 > band_pct:
            continue
        if right == "C" and k < f:
            continue
        if right == "P" and k > f:
            continue
        d = bs_pricing.bs_delta(f, k, t, sigma, r=0.0, right=right)
        if d is None:
            continue
        err = abs(abs(d) - target)
        if best is None or err < best[0]:
            best = (err, k, px)
    return (best[1], best[2]) if best else (None, None)


def _eval_minute(ts: str, bars: dict, meta: dict, *, band_pct: float,
                 min_pairs: int, max_iv: float, skew_delta: float) -> list[dict]:
    """一个分钟 → 每个到期一行 IV 摘要。"""
    by_exp: dict[str, dict] = defaultdict(lambda: {"C": {}, "P": {}, "vol": 0.0})
    for code, (close, vol) in bars.items():
        m = meta.get(code)
        if not m or m["strike"] <= 0:
            continue
        slot = by_exp[m["expire"]]
        slot[m["right"]][m["strike"]] = close
        slot["vol"] += vol

    out = []
    for expiry in sorted(by_exp):
        e = by_exp[expiry]
        dte = dte_at(ts, expiry)
        t = _years(dte)
        row = {
            "ts": ts, "expire": expiry, "dte": dte,
            "f": None, "mad_pct": None, "n_pairs": 0,
            "atm_strike": None, "atm_iv": None, "atm_iv_c": None, "atm_iv_p": None,
            "atm_n": 0, "atm_vol": 0.0,
            "skew25_c": None, "skew25_p": None, "skew": None,
            "stale": e["vol"] <= 0,
            "quality": QUALITY_OK,
        }
        if dte <= 0:
            row["quality"] = "expired"
            out.append(row)
            continue

        pair_rows = ([{"strike": k, "right": "C", "mid": v} for k, v in e["C"].items()]
                     + [{"strike": k, "right": "P", "mid": v} for k, v in e["P"].items()])
        fw = oc.implied_forward(pair_rows)
        row["n_pairs"] = fw["n_pairs"]
        row["mad_pct"] = fw["mad_pct"]
        if fw["n_pairs"] < min_pairs:
            row["quality"] = "no_pair"
            out.append(row)
            continue
        f = fw["f"]
        row["f"] = f
        if not f:
            row["quality"] = "no_f"
            out.append(row)
            continue
        if row["mad_pct"] is not None and row["mad_pct"] > F_NOISY_MAD_PCT:
            row["quality"] = "f_noisy"
            out.append(row)
            continue

        # ATM：只反解最接近 F 的两档（全档反解会把单月耗时推高 5 倍且无必要）
        calls_iv, puts_iv = {}, {}
        near = sorted(set(e["C"]) | set(e["P"]), key=lambda k: (abs(k - f), k))[:2]
        for k in near:
            if abs(k / f - 1.0) * 100.0 > band_pct:
                continue
            if k in e["C"]:
                iv = oc.iv_of(e["C"][k], f, k, t, "C")
                if iv and iv <= max_iv:
                    calls_iv[k] = iv
            if k in e["P"]:
                iv = oc.iv_of(e["P"][k], f, k, t, "P")
                if iv and iv <= max_iv:
                    puts_iv[k] = iv

        atm_k, atm_iv, atm_n = _pick_atm(calls_iv, puts_iv, f)
        row["atm_strike"] = atm_k
        row["atm_iv"] = atm_iv
        row["atm_n"] = atm_n
        row["atm_iv_c"] = calls_iv.get(atm_k) if atm_k is not None else None
        row["atm_iv_p"] = puts_iv.get(atm_k) if atm_k is not None else None
        row["atm_vol"] = e["vol"]
        if atm_iv is None:
            row["quality"] = "iv_fail"
            out.append(row)
            continue
        if atm_iv > max_iv:
            row["quality"] = "iv_extreme"
            out.append(row)
            continue

        # 25-delta 两侧（用 ATM IV 估 delta 定位档，再只对这两档反解）
        sigma = atm_iv
        kc, pc = _pick_otm_by_delta(e["C"], f, t, sigma, "C", skew_delta, band_pct)
        kp, pp = _pick_otm_by_delta(e["P"], f, t, sigma, "P", skew_delta, band_pct)
        if kc is not None:
            iv = oc.iv_of(pc, f, kc, t, "C")
            row["skew25_c"] = iv if (iv and iv <= max_iv) else None
        if kp is not None:
            iv = oc.iv_of(pp, f, kp, t, "P")
            row["skew25_p"] = iv if (iv and iv <= max_iv) else None
        if row["skew25_c"] and row["skew25_p"]:
            row["skew"] = row["skew25_c"] - row["skew25_p"]
        out.append(row)
    return out


# ── 序列 ────────────────────────────────────────────────────

def iv_series_from_bars(source, *, undl: str = "510050.SH", months=None,
                        days=None, band_pct: float = DEFAULT_BAND_PCT,
                        min_pairs: int = DEFAULT_MIN_PAIRS,
                        max_iv: float = DEFAULT_MAX_IV,
                        skew_delta: float = 0.25,
                        progress=None) -> dict:
    """QMT 1m 分片 → IV 序列（每 (分钟, 到期) 一行）。

    ``source``：导出目录（含 ``contracts_all.csv`` 与 ``opt1m_*.csv[.gz]``）。
    ``days``：只算这些交易日（``{"20261119", ...}``），None = 全部。
    ``progress``：可选回调 ``progress(minutes_done)``，便于长任务打点。

    返回 ``{undl, source, paths, rows, stats}``；``rows`` 字段见 ``_eval_minute``。
    """
    want = compact_undl(undl)
    contracts = load_contracts(source)
    meta = {c: v for c, v in contracts.items()
            if v["undl"] == want and (not months or v["month"] in months)}
    paths = shard_paths(source, undl, months)
    if not paths:
        raise FileNotFoundError(f"{source} 下没有 {want} 的 1m 分片")

    rows: list[dict] = []
    stats = {"minutes": 0, "rows": 0, "iv_ok": 0, "stale_rows": 0,
             "quality": {k: 0 for k in QUALITY_FAILS}}
    stats["quality"][QUALITY_OK] = 0

    ts_list, series = load_bar_matrix(paths, days=days)
    for ts in ts_list:
        bars = {code: v[ts] for code, v in series.items() if ts in v}
        if not bars:
            continue
        got = _eval_minute(ts, bars, meta, band_pct=band_pct, min_pairs=min_pairs,
                           max_iv=max_iv, skew_delta=skew_delta)
        rows.extend(got)
        stats["minutes"] += 1
        stats["rows"] += len(got)
        for r in got:
            stats["quality"][r["quality"]] = stats["quality"].get(r["quality"], 0) + 1
            if r["quality"] == QUALITY_OK:
                stats["iv_ok"] += 1
            if r["stale"]:
                stats["stale_rows"] += 1
        if progress and stats["minutes"] % 2000 == 0:
            progress(stats["minutes"])

    mads = [r["mad_pct"] for r in rows if r["mad_pct"] is not None]
    stats["mad_pct_median"] = oc.median(mads) if mads else None
    stats["months"] = sorted({meta[c]["month"] for c in meta})
    return {"undl": undl, "source": str(source), "paths": paths,
            "rows": rows, "stats": stats}


# ── 分位 ────────────────────────────────────────────────────

def _quantile(sorted_vals: list, q: float) -> Optional[float]:
    """线性插值分位（与 numpy.percentile 默认口径一致）。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 100:
        return sorted_vals[-1]
    pos = (n - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def constant_maturity_iv(rows, *, target_dte: float = 30.0, value_key: str = "atm_iv",
                         drop_quality=QUALITY_FAILS) -> dict:
    """把每个时刻的期限结构线性插值到固定 DTE（**不外推**）。

    为什么需要：按 DTE 分桶时每个桶只有「覆盖期 ÷ 到期档数」天样本 —— 52 天
    数据只有 3 个到期月（202611/202612/202703），实测每桶仅 2~6 天，撑不住分位。
    插值到固定期限后每天一个值，样本 = 全部交易日。

    返回 ``{target_dte, rows, stats}``；``rows`` 可直接喂 ``iv_percentile``（dte 固定）。
    """
    by_ts: dict[str, dict] = defaultdict(dict)
    for r in rows:
        if r.get("quality") in drop_quality:
            continue
        v = r.get(value_key)
        d = r.get("dte")
        if v is None or d is None:
            continue
        by_ts[str(r["ts"])][round(float(d), 6)] = v

    out, stats = [], {"minutes": 0, "no_below": 0, "no_above": 0}
    for ts in sorted(by_ts):
        pts = sorted(by_ts[ts].items())
        stats["minutes"] += 1
        lo = None
        for d, v in pts:
            if d <= target_dte + 1e-9:
                lo = (d, v)
        hi = None
        for d, v in pts:
            if d >= target_dte - 1e-9:
                hi = (d, v)
                break
        if lo is None:
            stats["no_below"] += 1          # 全比目标短 → 不向上外推
            continue
        if hi is None:
            stats["no_above"] += 1          # 全比目标长 → 不向下外推
            continue
        if abs(lo[0] - hi[0]) < 1e-9:
            iv = lo[1]
        else:
            w = (target_dte - lo[0]) / (hi[0] - lo[0])
            iv = lo[1] + (hi[1] - lo[1]) * w
        out.append({"ts": ts, "dte": float(target_dte), "atm_iv": iv,
                    "dte_lo": lo[0], "dte_hi": hi[0],
                    "quality": QUALITY_OK, "stale": False})
    return {"target_dte": float(target_dte), "rows": out, "stats": stats}


def _daily_values(rows: list[dict], value_key: str, daily: str) -> list[tuple]:
    """按交易日聚合成一个代表值 → ``[(YYYYMMDD, value)]``（升序）。"""
    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        v = r.get(value_key)
        if v is None:
            continue
        by_day[str(r["ts"])[:8]].append(v)
    out = []
    for day in sorted(by_day):
        vals = by_day[day]
        if daily == "first":
            pick = vals[0]
        elif daily == "median":
            pick = oc.median(vals)
        else:
            pick = vals[-1]
        if pick is not None:
            out.append((day, pick))
    return out


def iv_percentile(rows, *, dte=(8, 15), value_key: str = "atm_iv",
                  daily: str = "last", min_days: int = 5,
                  quantiles=(5, 10, 25, 50, 75, 90, 95),
                  drop_quality=QUALITY_FAILS, drop_stale: bool = False,
                  current: Optional[float] = None) -> dict:
    """某 DTE 区间的 IV 分位（**按日取代表值，样本量 = 天数**）。

    ``dte``：闭区间 ``(lo, hi)``。``daily``：当日代表值取 ``last``（默认，收盘
    IV）/ ``first`` / ``median``。``drop_stale``：是否剔除无成交分钟。
    返回含 ``percentiles`` / ``current_pct`` / ``markdown`` 的 dict。
    """
    lo, hi = float(dte[0]), float(dte[1])
    used, skipped = [], defaultdict(int)
    for r in rows:
        if r.get("quality") in drop_quality:
            skipped[r.get("quality")] += 1
            continue
        if drop_stale and r.get("stale"):
            skipped["stale"] += 1
            continue
        d = r.get("dte")
        if d is None or not (lo <= float(d) <= hi):
            continue
        if r.get(value_key) is None:
            continue
        used.append(r)

    pairs = _daily_values(used, value_key, daily)
    vals = sorted(v for _, v in pairs)
    n_days = len(vals)
    res = {
        "dte_range": [lo, hi], "value_key": value_key, "daily": daily,
        "minutes": len(used), "days": n_days,
        "values": [round(v, 6) for v in vals],
        "days_list": [d for d, _ in pairs],
        "percentiles": {}, "current": None, "current_pct": None,
        "skipped": dict(skipped), "sufficient": n_days >= int(min_days),
        "caveat": ("分钟级 IV 高度自相关，有效独立样本按「天」计"
                   if n_days else "无可用样本"),
    }
    for q in quantiles:
        res["percentiles"][int(q)] = _quantile(vals, q)

    cur = current if current is not None else (vals[-1] if vals else None)
    res["current_source"] = "explicit" if current is not None else "last"
    if cur is not None and n_days:
        below = sum(1 for v in vals if v <= cur)
        res["current"] = cur
        res["current_pct"] = below / n_days * 100.0
    res["markdown"] = _pct_markdown(res)
    return res


def _sample_tier(n: int) -> str:
    """样本天数分级：分位表在 10 天以下只能当参考。"""
    if n >= 20:
        return "足够"
    if n >= 10:
        return "初步"
    return "偏少"


def _pct_markdown(res: dict) -> str:
    lo, hi = res["dte_range"]
    span = f"DTE {lo:g} 天" if abs(lo - hi) < 1e-9 else f"DTE {lo:g}–{hi:g} 天"
    if not res["days"]:
        return f"**IV 分位（{span}）**：无可用样本（{res['caveat']}）"
    lines = [f"**IV 分位（{span} · {res['value_key']}）**", "",
             "| 分位 | IV |", "|---|---:|"]
    for q, v in res["percentiles"].items():
        if v is not None:
            lines.append(f"| p{q} | {v * 100:.2f}% |")
    cur = res.get("current")
    if cur is not None:
        tag = "当前" if res.get("current_source") == "explicit" else "当前（序列最后一日）"
        lines.append(f"| **{tag}** | **{cur * 100:.2f}%** (p{res['current_pct']:.0f}) |")
    lines.append("")
    tier = _sample_tier(res["days"])
    lines.append(f"样本：{res['days']} 个交易日 / {res['minutes']} 个分钟点（{tier}）· "
                 f"分钟级 IV 自相关，分位以「日」为单位统计")
    if res["skipped"]:
        sk = " / ".join(f"{k}={v}" for k, v in sorted(res["skipped"].items()))
        lines.append(f"剔除：{sk}")
    return "\n".join(lines)


def bucket_table(rows, *, buckets=DTE_BUCKETS, **kw) -> dict:
    """按 DTE 分桶批量算分位 → ``{buckets: [...], markdown}``。"""
    out = []
    for lo, hi in buckets:
        out.append(iv_percentile(rows, dte=(lo, hi), **kw))
    return {"buckets": out, "markdown": "\n\n".join(b["markdown"] for b in out)}


# ── CLI（本地自测用）────────────────────────────────────────
def _main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="QMT 1m → IV 序列 / 分位（只读）")
    ap.add_argument("source", help="QMT 1m 导出目录（含 contracts_all.csv）")
    ap.add_argument("--undl", default="510050.SH")
    ap.add_argument("--months", default="", help="逗号分隔，如 202612")
    ap.add_argument("--days", default="", help="逗号分隔交易日，如 20261119")
    ap.add_argument("--out", default="", help="序列 CSV 输出路径")
    ap.add_argument("--cm-dte", type=float, default=30.0,
                    help="常数期限插值目标（天），0=关闭")
    args = ap.parse_args(argv)

    months = [m.strip() for m in args.months.split(",") if m.strip()] or None
    days = {d.strip() for d in args.days.split(",") if d.strip()} or None
    res = iv_series_from_bars(args.source, undl=args.undl, months=months, days=days,
                              progress=lambda n: print(f"  … {n} 分钟", flush=True))
    st = res["stats"]
    print(f"[{args.undl}] 分片 {len(res['paths'])} 个 · 分钟 {st['minutes']} · "
          f"行 {st['rows']} · IV 可用 {st['iv_ok']} · stale {st['stale_rows']} · "
          f"parity MAD 中位 {st['mad_pct_median']}")
    print("质量: " + " / ".join(f"{k}={v}" for k, v in st["quality"].items() if v))
    print(bucket_table(res["rows"])["markdown"])
    if args.cm_dte:
        cm = constant_maturity_iv(res["rows"], target_dte=args.cm_dte)
        cs = cm["stats"]
        print(f"\n—— 常数期限插值 {args.cm_dte:g} 天（不外推）——")
        print(f"分钟 {cs['minutes']} · 无短档 {cs['no_below']} · 无长档 {cs['no_above']}")
        print(iv_percentile(cm["rows"], dte=(args.cm_dte, args.cm_dte))["markdown"])
    if args.out:
        cols = ["ts", "expire", "dte", "f", "mad_pct", "n_pairs", "atm_strike",
                "atm_iv", "atm_iv_c", "atm_iv_p", "skew25_c", "skew25_p", "skew",
                "stale", "quality"]
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(res["rows"])
        print(f"序列已写 {args.out}（{len(res['rows'])} 行）")
    return res


if __name__ == "__main__":
    _main()
