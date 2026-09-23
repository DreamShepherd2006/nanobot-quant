"""期权盘口价差画像（只读研究工具）—— C43① 的**测量前置**。

要回答的问题
------------
期权回测里没有真实 bid/ask：已到期合约的 mark 历史取不到，价格一律由
「归档成交反解 IV → 微笑插值 → BS 重定价」得出，于是 ``chain_dict_at`` 只能
用「价格 × ±滑点%」补两条边（``bid = mark×(1−sl)``、``ask = mark×(1+sl)``，
sl 默认 0.5%）。

但真实做市商报的是 **IV 双边**（``bid = BS(σ_bid)``、``ask = BS(σ_ask)``，
OKX ticker 直接给 ``bidVol/askVol``）。按 SOL 3 天、中价 IV 65% 实算：
现模型 ±0.5% 的价格价差只等价 **0.1–0.7 个 IV 点**，而实测 IV 价差是
**10–17 点** 量级 —— 相当于假设「穿越买卖价差几乎免费」；而且偏差随虚值
程度放大（价差 ÷ 权利金：ATM ≈ 23%、−5.7% OTM ≈ 67%、−10% OTM ≈ 127%），
我们卖的正是 5–7% 虚值 put。

本工具用 ``option_tape`` 采集的盘口样本把 Δσ 的**真实分布**量出来，
再决定回测侧该用一个常数（方案 A）还是分层（方案 B）。

口径与边界
----------
* **只读**：只读磁盘上的 tape 文件 + 纯计算 —— 不拉网络、不下单、不改配置、
  不碰回测数字。
* **覆盖率是第一门**：样本不足时**不给结论**（``coverage_ok=False`` + 报告
  横幅），只摆分布；「测不出来」≠「没有关系」。
* σ_bid/σ_ask 用与回测同一条 BS 反解路径（``bs_pricing.implied_vol``，
  二分法），不自造定价参数。
* 采集器的带宽/到期档决定覆盖面：某桶为空多半是**采集范围没覆盖**，
  不是市场没有 —— 报告里会一并给出桶容量。
* 现模型等价 Δσ 是**对照量**（把 ``mark×(1±0.5%)`` 再反解回 IV 的差），
  不是实测值。

本地自测
--------
``python -m nanobot_quant.analysis.option_spread --days 2 --family SOL-USD_UM``
（带 ``--json`` 输出结构化结果；缺 tape 文件时给出明确说明而非空表）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from nanobot_quant import option_tape as tape
from nanobot_quant.bs_pricing import implied_vol

DEFAULT_DAYS = 3
MAX_DAYS = 14
MODEL_SLIP = 0.005          # 回测现模型的价格滑点（±0.5%），仅作对照
MIN_SAMPLES_DEFAULT = 200   # 覆盖率门：低于此值只摆分布、不下结论
MS_PER_DAY = 86_400_000.0
DAYS_PER_YEAR = 365.0

DELTA_BUCKETS = ((0.0, 0.05, "|Δ|≤0.05"), (0.05, 0.15, "0.05–0.15"),
                 (0.15, 0.30, "0.15–0.30"), (0.30, 0.50, "0.30–0.50"),
                 (0.50, 1.01, "|Δ|>0.50"))
DTE_BUCKETS = ((0.0, 1.0, "≤1d"), (1.0, 3.0, "1–3d"), (3.0, 7.0, "3–7d"),
               (7.0, 14.0, "7–14d"), (14.0, 1e9, ">14d"))


def _log(msg: str) -> None:
    """诊断一律走 stderr（MCP stdio 通道不能被污染）。"""
    print(f"[OPT-SPRD] {msg}", file=sys.stderr, flush=True)


def _f(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # NaN → None


def _bucket(value: float, buckets: Iterable[tuple]) -> str:
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return "—"


def delta_bucket(delta: Optional[float]) -> str:
    """按 |delta| 分桶（delta 缺失 → '—'）。"""
    d = _f(delta)
    return "—" if d is None else _bucket(abs(d), DELTA_BUCKETS)


def dte_bucket(days: Optional[float]) -> str:
    """按剩余期限分桶（天）。"""
    d = _f(days)
    return "—" if d is None else _bucket(d, DTE_BUCKETS)


def _pct(vals: list, q: float) -> Optional[float]:
    """线性插值分位（``q`` ∈ [0,1]）；空列表 → None。"""
    xs = sorted(x for x in vals if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _stats(vals: list) -> dict:
    xs = [x for x in vals if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "median": _pct(xs, 0.5), "p25": _pct(xs, 0.25),
            "p75": _pct(xs, 0.75), "mean": sum(xs) / len(xs),
            "min": min(xs), "max": max(xs)}


def row_metrics(row: dict) -> tuple[Optional[dict], str]:
    """一条盘口报价 → 指标；不可用则返回 ``(None, 原因)``。

    指标：``iv_spread_pts``（σ_ask − σ_bid，单位：波动率点 = 百分点）、
    ``price_spread_pct``（(ask−bid)/mid，%）、``model_iv_spread_pts``
    （把 mid 上下 0.5% 反解回 IV 的差 —— 回测现模型的等价量）、
    ``mid``（每名义币价格）、``delta``、``dte_days``。
    """
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask <= bid:
        return None, "no_quote"
    spot, strike = _f(row.get("spot")), _f(row.get("strike"))
    if spot is None or spot <= 0:
        return None, "no_spot"
    if strike is None or strike <= 0:
        return None, "no_strike"
    exp_ms, ts_ms = _f(row.get("expiry_ms")), _f(row.get("ts_ms"))
    if not exp_ms or not ts_ms:
        return None, "no_expiry"
    t = (exp_ms - ts_ms) / MS_PER_DAY / DAYS_PER_YEAR
    if t <= 0:
        return None, "expired"
    right = str(row.get("right") or "P").upper()[:1]
    s_bid = implied_vol(bid, spot, strike, t, right=right)
    s_ask = implied_vol(ask, spot, strike, t, right=right)
    mid = (bid + ask) / 2.0
    s_mid = implied_vol(mid, spot, strike, t, right=right) if mid > 0 else None
    s_lo = implied_vol(mid * (1 - MODEL_SLIP), spot, strike, t, right=right)
    s_hi = implied_vol(mid * (1 + MODEL_SLIP), spot, strike, t, right=right)
    if s_bid is None or s_ask is None:
        return None, "no_iv"
    return {
        "family": str(row.get("family") or "—"),
        "inst": str(row.get("inst") or ""),
        "right": right,
        "ts": row.get("ts"),
        "iv_spread_pts": (s_ask - s_bid) * 100.0,
        "price_spread_pct": (ask - bid) / mid * 100.0 if mid > 0 else None,
        "model_iv_spread_pts": ((s_hi - s_lo) * 100.0
                                if s_lo is not None and s_hi is not None else None),
        "mid": mid,
        "mid_iv_pts": s_mid * 100.0 if s_mid is not None else None,
        "delta": _f(row.get("delta")),
        "mark_vol_pts": (_f(row.get("mark_vol")) or 0) * 100.0 or None,
        "dte_days": (exp_ms - ts_ms) / MS_PER_DAY,
    }, ""


def collect(days: int = DEFAULT_DAYS, families: Optional[list] = None,
            progress: Callable[[str], None] = _log) -> tuple[list, dict]:
    """读最近 ``days`` 天的 tape 采样并算出逐行指标（含覆盖率统计）。"""
    days = max(1, min(int(days), MAX_DAYS))
    fam_filter = {str(f).strip() for f in (families or []) if str(f).strip()}
    today = datetime.now(timezone.utc).date()
    day_list = [(today - timedelta(days=i)).strftime("%Y%m%d")
                for i in range(days - 1, -1, -1)]
    cov = {"days": day_list, "files": [], "missing_files": [], "rows": 0,
           "used": 0, "skipped": {}, "families_seen": {},
           "families_filter": sorted(fam_filter)}
    out: list = []
    for day in day_list:
        recs = tape.load_tape(day)
        if recs:
            cov["files"].append(day)
        else:
            cov["missing_files"].append(day)
        flat = tape.flatten(recs)
        cov["rows"] += len(flat)
        for r in flat:
            fam = str(r.get("family") or "—")
            if fam_filter and fam not in fam_filter:
                continue
            cov["families_seen"][fam] = cov["families_seen"].get(fam, 0) + 1
            m, reason = row_metrics(r)
            if m is None:
                cov["skipped"][reason] = cov["skipped"].get(reason, 0) + 1
                continue
            out.append(m)
        progress(f"{day}: 采样 {len(recs)} 条 / 报价 {len(flat)} 行")
    cov["used"] = len(out)
    return out, cov


def _group(label: str, items: list) -> dict:
    return {
        "label": label,
        "n": len(items),
        "iv_spread_pts": _stats([m["iv_spread_pts"] for m in items]),
        "price_spread_pct": _stats([m["price_spread_pct"] for m in items]),
        "model_iv_spread_pts": _stats([m["model_iv_spread_pts"] for m in items]),
        "mid_px_median": _pct([m["mid"] for m in items], 0.5),
        "mid_iv_pts_median": _pct([m["mid_iv_pts"] for m in items], 0.5),
        "put_share": (sum(1 for m in items if m["right"] == "P") / len(items)
                      if items else None),
    }


def _group_by(items: list, key_fn) -> list:
    buckets: dict = {}
    for m in items:
        buckets.setdefault(key_fn(m), []).append(m)
    return [_group(k, v) for k, v in sorted(buckets.items())]


def summarize(days: int = DEFAULT_DAYS, families: Optional[list] = None,
              min_samples: int = MIN_SAMPLES_DEFAULT,
              progress: Callable[[str], None] = _log) -> dict:
    """跑一次价差画像（只读）。

    Args:
        days: 回看天数（≤14，一天一文件）；文件缺失会在覆盖率里列明。
        families: 家族白名单（空 = 全部）。
        min_samples: 覆盖率门；可用样本低于此值 → ``coverage_ok=False``
            （只摆分布、不下结论）。

    Returns:
        dict：``ok`` / ``coverage`` / ``coverage_ok`` / ``overall`` /
        ``by_family`` / ``by_delta`` / ``by_dte`` / ``by_family_delta`` /
        ``notes`` / ``markdown``。参数或读取异常 → ``ok=False`` + ``error``。
    """
    rows, cov = collect(days=days, families=families, progress=progress)
    res = {
        "ok": True, "days": max(1, min(int(days), MAX_DAYS)),
        "families": sorted({m["family"] for m in rows}),
        "min_samples": int(min_samples),
        "coverage": cov,
        "coverage_ok": len(rows) >= max(1, int(min_samples)),
        "overall": _group("全部", rows),
        "by_family": _group_by(rows, lambda m: m["family"]),
        "by_delta": _group_by(rows, lambda m: delta_bucket(m["delta"])),
        "by_dte": _group_by(rows, lambda m: dte_bucket(m["dte_days"])),
        "by_family_delta": _group_by(rows, lambda m: f'{m["family"]} × {delta_bucket(m["delta"])}'),
    }
    notes = []
    if not rows:
        notes.append("没有任何可用样本：先确认 📼 盘口采集在跑（tape 一天一文件），"
                     "并检查上面的 missing_files / skipped。")
    flt = cov.get("families_filter") or []
    if flt and not rows:
        notes.append(f"筛选条件 {', '.join(flt)} 未命中任何样本；tape 实际覆盖家族 = "
                     f"{', '.join(sorted(cov.get('families_seen') or {})) or '（无报价行）'}。")
    if not res["coverage_ok"]:
        notes.append(f"样本 {len(rows)} < 门槛 {int(min_samples)}：**只摆分布、不下结论**"
                     "（覆盖率是第一门；「测不出来」≠「没有关系」）。")
    notes.append("采集带宽/到期档决定覆盖面：某桶为空多半是采集范围没覆盖，"
                 "不是市场没有。")
    notes.append("Δσ 单位 = 波动率点（百分点）；现模型等价 Δσ 是把 mid 上下 0.5% "
                 "反解回 IV 的差，仅作对照、不是实测。")
    res["notes"] = notes
    res["markdown"] = markdown(res)
    return res


def _fmt(v, nd: int = 2, dash: str = "—") -> str:
    if v is None:
        return dash
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return dash


def _tbl(rows: list, cols: list) -> list:
    out = ["| " + " | ".join(c[0] for c in cols) + " |",
           "|" + "|".join(":--" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join(c[1](r) for c in cols) + " |")
    return out


def _gcols(first: str) -> list:
    return [
        (first, lambda g: g["label"]),
        ("样本", lambda g: str(g["n"])),
        ("Δσ 中位(点)", lambda g: _fmt(g["iv_spread_pts"].get("median"))),
        ("Δσ p25–p75", lambda g: f'{_fmt(g["iv_spread_pts"].get("p25"))}–'
                                f'{_fmt(g["iv_spread_pts"].get("p75"))}'),
        ("价差/权利金 中位%", lambda g: _fmt(g["price_spread_pct"].get("median"))),
        ("现模型等价 Δσ(点)", lambda g: _fmt(g["model_iv_spread_pts"].get("median"))),
        ("权利金中位", lambda g: _fmt(g["mid_px_median"], 4)),
        ("中价 IV(点)", lambda g: _fmt(g["mid_iv_pts_median"], 1)),
    ]


def markdown(res: dict) -> str:
    """把结果渲染成可直接粘贴的报告。"""
    cov = res.get("coverage") or {}
    ov = res.get("overall") or {}
    L: list = []
    L.append("# 期权盘口价差画像（只读）")
    L.append("")
    flt = cov.get("families_filter") or []
    L.append(f"区间：最近 {res.get('days')} 天 tape 采样（一天一文件） ｜ "
             f"覆盖家族：{', '.join(res.get('families') or []) or '—'} ｜ "
             f"可用样本：**{cov.get('used', 0)}**"
             f"（报价行 {cov.get('rows', 0)}）"
             + (f" ｜ 筛选：{', '.join(flt)}" if flt else ""))
    if not res.get("coverage_ok"):
        L.append("")
        L.append(f"> ⚠️ **覆盖率不足**（样本 {cov.get('used', 0)} < 门槛 "
                 f"{res.get('min_samples')}）：以下只摆分布，**不构成结论**。")
    L.append("")
    L.append("## 一句话")
    iv_med = (ov.get("iv_spread_pts") or {}).get("median")
    mdl_med = (ov.get("model_iv_spread_pts") or {}).get("median")
    ps_med = (ov.get("price_spread_pct") or {}).get("median")
    if res.get("coverage_ok") and iv_med is not None:
        L.append(f"- 实测 IV 价差（σ_ask − σ_bid）中位 **{_fmt(iv_med)} 点**；"
                 f"回测现模型（价格 ±0.5%）的等价量仅 **{_fmt(mdl_med)} 点**"
                 f"（≈ {_fmt((mdl_med / iv_med) if iv_med else None, 3)}×）。")
        L.append(f"- 实测价差占权利金中位 **{_fmt(ps_med)}%**，而现模型恒为 1.0% "
                 f"（±0.5% 两侧）→ 回测开仓一侧的权利金收入偏乐观。")
    else:
        L.append("- 样本不足，本报告不给结论（先攒 tape 样本）。")
    L.append("")
    L.append("## 覆盖率")
    L.append("")
    L.append(f"- 有数据的文件：{', '.join(cov.get('files') or []) or '—'}")
    L.append(f"- 缺文件的日期：{', '.join(cov.get('missing_files') or []) or '无'}")
    sk = cov.get("skipped") or {}
    L.append("- 跳过原因：" + (", ".join(f"{k}={v}" for k, v in sorted(sk.items()))
                              if sk else "无"))
    seen = cov.get("families_seen") or {}
    if seen:
        L.append("- 家族报价行：" + ", ".join(f"{k} {v}" for k, v in sorted(seen.items())))
    L.append("")
    L.append("## 实测 Δσ（波动率点）")
    L.append("")
    L.append("**按家族**")
    L.append("")
    L += _tbl(res.get("by_family") or [], _gcols("家族"))
    L.append("")
    L.append("**按 |delta| 桶**（决策依据：Δσ 是否随虚值程度变化 → 决定常数还是分层）")
    L.append("")
    L += _tbl(res.get("by_delta") or [], _gcols("|Δ| 桶"))
    L.append("")
    L.append("**按剩余期限桶**")
    L.append("")
    L += _tbl(res.get("by_dte") or [], _gcols("期限桶"))
    L.append("")
    L.append("**家族 × |delta|**")
    L.append("")
    L += _tbl(res.get("by_family_delta") or [], _gcols("家族 × 桶"))
    L.append("")
    L.append("## 读法与局限")
    L.append("")
    for n in res.get("notes") or []:
        L.append(f"- {n}")
    L.append("- 现模型对照：`bid = mark×(1−0.5%)`、`ask = mark×(1+0.5%)`"
             "（`chain_dict_at`），开仓吃 bid、平仓吃 ask。")
    return "\n".join(L)


def main(argv: Optional[list] = None) -> int:
    """CLI 入口（本地自测）：默认打印 markdown，``--json`` 打印结构化结果。"""
    ap = argparse.ArgumentParser(description="期权盘口价差画像（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--family", action="append", default=[],
                    help="家族白名单，可重复（如 --family SOL-USD_UM）")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT)
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    a = ap.parse_args(argv)
    res = summarize(days=a.days, families=a.family, min_samples=a.min_samples)
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "markdown"},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(res["markdown"])
    return 0 if res.get("ok") else 1


if __name__ == "__main__":          # pragma: no cover - CLI
    sys.exit(main())
