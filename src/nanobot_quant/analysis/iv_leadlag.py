"""IV 领先/滞后分析（B 项）：realized → implied 方向性验证（2026-09-22 讨论定稿）。

**要回答的问题**（见 ``docs/quant-system.md`` §33.37）：

- **H1** crypto 里是 **已实现波动领先隐含波动**（realized → implied）吗？
  美股微观结构是期权市场定价未来（implied → realized）；若 crypto 相反，则我们基于
  已实现波动的传感器（F1 / ATR 比）在这个市场上具有**真实信息优势**，而非仅仅后视。
- **H2** 「F1 领先 IV」有多少是**平滑假象**？
  F1 = ATR[t]/ATR[t−lookback] 是对过去窗口的平滑量，按构造不可能"领先"。
  因此同一检验必须用**朴素已实现波动**（close-to-close 滚动标准差）与 F1 各跑一次，
  对比峰值滞后：只有朴素量也领先，才说明信息确实在已实现侧。
- **H3** 外部对照：Deribit **DVOL**（BTC 官方波动率指数、长历史）与已实现波动谁领先？
  用来判断「crypto 的方向性」是不是普遍现象，而不是某一家的报价机制。

**方法与口径**（可复现、可回归）：

- **IV 序列**：OKX 归档逐笔成交 → 反解 IV（``iv_surface``，含虚值侧优先与质量门）→
  按到期建微笑 → 在 ATM 处插值 → 取**最近目标期限**（默认 3 天，恒定 tenor，
  避免期限漂移污染序列）。数据源与期权线其它部分同源（OKX）。
- **已实现波动**：同源 OKX 现货 K 线（``okx_cex_data`` 分页），close-to-close 对数收益
  的滚动标准差 × √(年/周期)。窗口默认 12 根（5m → 1 小时）。
- **领先-滞后**：Δ 序列互相关，``lag > 0`` 表示**左序列领先**。
  显著性用**循环平移零假设**（默认 500 次）：重叠窗口与自相关会让朴素 t 检验
  严重高估显著性，循环平移保留各自的平滑结构、只破坏两者的时间对齐关系。
- **不做任何交易判断**：本模块只输出统计量；是否接入交易路径由回测裁决。

**已知限制**（必须在读结果时一并考虑）：

- OKX 归档只滚动保留约 30 天 → 样本量上限约 14–28 天（5m → 4000–8000 根）；
- 归档成交稀疏（SOL 日均 ~80 笔），5m 桶内常无成交 → IV 序列按「最近可用点」前向填充，
  这会**人为增加 IV 的平滑度、压低其领先能力**（对 H1 是保守方向）；
- 已实现波动用的是**现货**（期权标的），而 IV 来自**期权盘口**——两者同源标的但不同市场层。
"""

from __future__ import annotations

import json
import math
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
import pandas as pd

from nanobot_quant import okx_cex_data
from nanobot_quant import okx_options_data as od
from nanobot_quant import options_history as oh
from nanobot_quant.environment.sensors import compute_f1, f1_lookback_for
from nanobot_quant.iv_surface import IVPoint, fit_smile, iv_points_from_trades

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0

BAR_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
}

DERIBIT_DVOL_URL = ("https://www.deribit.com/api/v2/public/"
                    "get_volatility_index_data")


def _log(msg: str) -> None:
    """诊断一律走 stderr（MCP stdio 通道不能被污染）。"""
    print(f"[IV-LL] {msg}", file=sys.stderr, flush=True)


def bucket_seconds(bucket: str) -> int:
    secs = BAR_SECONDS.get(str(bucket))
    if not secs:
        raise ValueError(f"不支持的周期 {bucket!r}；可选 {sorted(BAR_SECONDS)}")
    return secs


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


# ────────────────────────── 序列构造 ──────────────────────────

def realized_vol(close: pd.Series, *, window: int, bucket: str) -> pd.Series:
    """朴素已实现波动（年化）：close-to-close 对数收益的滚动标准差。

    不做平滑、不做窗口比——就是「这段时间标的价格真实跑了多少」。
    """
    px = pd.Series(close).astype(float)
    ret = np.log(px).diff()
    factor = math.sqrt(SECONDS_PER_YEAR / bucket_seconds(bucket))
    return ret.rolling(int(window)).std() * factor


def spot_frame(ref: str, *, begin_ms: int, end_ms: int,
               bucket: str = "5m") -> pd.DataFrame:
    """按 instId 直接取 OKX 现货 K 线（列名小写、UTC 索引、时间升序）。"""
    start = datetime.fromtimestamp(begin_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    df = okx_cex_data.fetch_kline_range(ref, start, end, bar=bucket)
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    if "close" not in df.columns:
        raise RuntimeError(f"现货 K 线缺少 close 列（{ref}）：{list(df.columns)}")
    return df.sort_index()


def spot_klines(family: str, *, begin_ms: int, end_ms: int,
                bucket: str = "5m") -> pd.DataFrame:
    """标的现货 K 线（OKX，与期权线同源）。

    家族 → 现货对的映射**复用** ``okx_options_data.spot_ref_of``，不另写一份。
    """
    ref = od.spot_ref_of(family)
    if not ref:
        raise ValueError(f"家族 {family} 没有现货参考对（spot_ref_of → None）")
    return spot_frame(ref, begin_ms=begin_ms, end_ms=end_ms, bucket=bucket)


def spot_lookup(klines: pd.DataFrame) -> Callable[[int], Optional[float]]:
    """ts_ms → 该时刻现货价（最近一根前值，绝不外推）。"""
    if klines is None or klines.empty:
        return lambda _ts: None
    ts = np.asarray(klines.index.asi8, dtype="int64") // 1_000_000
    close = klines["close"].astype(float).to_numpy()

    def _at(ts_ms: int) -> Optional[float]:
        i = int(np.searchsorted(ts, int(ts_ms), side="right")) - 1
        if i < 0:
            return None
        return float(close[i])

    return _at


def _pick_by_smile(group: list[IVPoint], bkt: int, target_tenor_days: float,
                   min_strikes: int) -> Optional[dict]:
    """``smile`` 口径：桶内选最接近目标期限、可建微笑的到期 → ATM 插值。"""
    target_ms = bkt + int(target_tenor_days * 86400_000)
    by_exp: dict[int, list[IVPoint]] = {}
    for p in group:
        by_exp.setdefault(p.exp_ms, []).append(p)
    best: Optional[dict] = None
    for exp_ms, g in by_exp.items():
        if len({p.strike for p in g}) < min_strikes:
            continue
        spot = float(np.median([p.spot for p in g]))
        smile = fit_smile(g, forward=spot)
        if smile is None or smile.empty:
            continue
        iv = smile.iv(spot)                        # ATM（forward 处）插值
        if iv is None or iv <= 0:
            continue
        cost = abs(exp_ms - target_ms)
        if best is None or cost < best["cost"]:
            best = {"cost": cost, "exp_ms": exp_ms, "iv": float(iv),
                    "n_points": len(g),
                    "n_strikes": len({p.strike for p in g}), "spot": spot}
    if not best:
        return None
    return {
        "ts_ms": bkt,
        "ts": datetime.fromtimestamp(bkt / 1000.0, tz=timezone.utc),
        "iv_atm": best["iv"],     # 小数（0.52 = 52%），与 iv_surface 同口径
        "n_points": best["n_points"],
        "n_strikes": best["n_strikes"],
        "tenor_days": (best["exp_ms"] - bkt) / 86400_000.0,
        "spot": best["spot"],
    }


def atm_iv_series(family: str, *, begin_ms: int, end_ms: int,
                  bucket: str = "5m", target_tenor_days: float = 3.0,
                  cache_dir: Optional[str] = None,
                  spot_df: Optional[pd.DataFrame] = None,
                  min_strikes: int = 2,
                  mode: str = "atm_median",
                  band_pct: float = 10.0,
                  progress: Optional[Callable[[str], None]] = None) -> dict:
    """归档成交 → 每个 bucket 的 **近 ATM IV** 序列（两种口径，默认 ``atm_median``）。

    数据现实：OKX 单家族日均成交几十笔，5m 桶大多没成交。所以序列密度取决于口径：

    - ``atm_median``（默认）：桶内所有「|K/S−1| ≤ ``band_pct`` 且期限在 0.5–14 天」的
      成交，取其反解 IV 的**中位数**。不需建微笑、桶覆盖率高，代价是跨 strike/期限
      混一点（近 ATM + 近月，实际离散度小）。
    - ``smile``：逐桶逐到期建微笑 → 选最接近 ``target_tenor_days`` 的到期 →
      在 forward 处插值。口径最干净（恒定 tenor），但需每桶 ≥ ``min_strikes`` 个
      strike，覆盖稀疏（SOL 实测 255 笔 → 仅 16 桶）。

    两种口径都用 OKX 归档成交、同一 IV 反解与质量门，差异仅在聚合方式。
    """
    secs = bucket_seconds(bucket)
    bkt_ms = secs * 1000
    notes: list[str] = []
    rejects: Counter = Counter()

    paths = oh.fetch_range(begin_ms, end_ms, dest_dir=cache_dir, progress=progress)
    trades = list(oh.iter_trades(paths, family=family))
    notes.append(f"归档 {len(paths)} 天 / 成交 {len(trades)} 笔（{family}）")
    if not trades:
        return {"frame": pd.DataFrame(), "notes": notes, "trades": 0,
                "rejects": dict(rejects)}

    contracts: dict[str, dict] = {}
    for tr in trades:
        inst = tr.get("instrument_name") or ""
        if inst in contracts:
            continue
        meta = oh.contract_meta(inst, family=family)
        if meta is None:
            rejects["meta"] += 1
            continue
        contracts[inst] = meta
    if not contracts:
        notes.append("没有可解析的合约（归档内容与家族不匹配？）")
        return {"frame": pd.DataFrame(), "notes": notes,
                "trades": len(trades), "rejects": dict(rejects)}

    kl = spot_df if spot_df is not None else spot_klines(
        family, begin_ms=begin_ms - secs * 1000, end_ms=end_ms + secs * 1000,
        bucket=bucket)
    spot_at = spot_lookup(kl)

    pts = iv_points_from_trades(
        trades, spot_at=spot_at, contracts=contracts,
        on_reject=lambda kind, _inst: rejects.update([kind]))
    notes.append(f"IV 观测 {len(pts)} 个（拒绝：{dict(rejects)}）")
    if not pts:
        return {"frame": pd.DataFrame(), "notes": notes, "trades": len(trades),
                "rejects": dict(rejects)}

    rows: list[dict] = []
    by_bucket: dict[int, list[IVPoint]] = {}
    for p in pts:
        by_bucket.setdefault(p.ts_ms // bkt_ms * bkt_ms, []).append(p)

    mode = (mode or "atm_median").lower()
    for bkt, group in sorted(by_bucket.items()):
        if mode == "smile":
            best = _pick_by_smile(group, bkt, target_tenor_days, min_strikes)
            if best:
                rows.append(best)
            continue
        # atm_median（默认）：近 ATM + 近月成交的 IV 中位数
        cand = []
        for p in group:
            if not p.spot or p.spot <= 0:
                continue
            if abs(p.strike / p.spot - 1.0) * 100.0 > band_pct:
                continue
            tenor = (p.exp_ms - bkt) / 86400_000.0
            if not (0.5 <= tenor <= 14.0):
                continue
            cand.append((p, tenor))
        if not cand:
            continue
        ivs = sorted(p.iv for p, _ in cand)
        mid = ivs[len(ivs) // 2] if len(ivs) % 2 else 0.5 * (ivs[len(ivs) // 2 - 1] + ivs[len(ivs) // 2])
        rows.append({
            "ts_ms": bkt,
            "ts": datetime.fromtimestamp(bkt / 1000.0, tz=timezone.utc),
            "iv_atm": float(mid),
            "n_points": len(cand),
            "n_strikes": len({p.strike for p, _ in cand}),
            "tenor_days": float(np.median([t for _, t in cand])),
            "spot": float(np.median([p.spot for p, _ in cand])),
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.set_index("ts").sort_index()
        # 补全时间网格（无成交的桶 → 前向填充）；填充比例显式记录——
        # 填充会人为增加 IV 平滑度（对 H1 是保守方向）。
        grid = pd.date_range(frame.index.min(), frame.index.max(),
                            freq=f"{secs}s", tz="UTC")
        observed = int(len(frame))
        frame = frame.reindex(grid)
        frame["iv_atm"] = frame["iv_atm"].ffill()
        still_nan = int(frame["iv_atm"].isna().sum())
        frame["iv_atm"] = frame["iv_atm"].astype(float)
        filled = len(frame) - observed - still_nan
        notes.append(f"IV 桶 {observed} 个 → 网格 {len(frame)} 根（前向填充 {filled} 根）")
    else:
        notes.append("没有可用的 IV 桶（放宽 band_pct / 改 smile 口径？检查区间与家族）")

    return {"frame": frame, "notes": notes, "trades": len(trades),
            "rejects": dict(rejects)}


# ────────────────────────── 领先-滞后 ──────────────────────────

def leadlag_table(a: pd.Series, b: pd.Series, *, max_lag: int = 6,
                  diff: bool = True) -> pd.DataFrame:
    """互相关表：``corr(a[t−k], b[t])``；**k > 0 表示 a 领先 b**。"""
    aa = pd.Series(a).astype(float)
    bb = pd.Series(b).astype(float)
    if diff:
        aa = aa.diff()
        bb = bb.diff()
    df = pd.concat([aa.rename("a"), bb.rename("b")], axis=1).dropna()
    out = []
    for k in range(-int(max_lag), int(max_lag) + 1):
        sub = pd.concat([df["a"].shift(k), df["b"]], axis=1).dropna()
        if len(sub) < 5 or sub["a"].std() == 0 or sub["b"].std() == 0:
            corr, n = float("nan"), len(sub)
        else:
            corr = float(sub["a"].corr(sub["b"]))
            n = len(sub)
        out.append({"lag": k, "corr": corr, "n": n})
    return pd.DataFrame(out)


def shift_test(a: pd.Series, b: pd.Series, *, max_lag: int = 6,
               n_iter: int = 500, seed: int = 0, diff: bool = True) -> dict:
    """领先-滞后的显著性：**循环平移零假设**。

    统计量 = 「a 领先侧（k ≥ 1）互相关的最大值」。零假设用把 b **循环平移**随机
    偏移量来构造——保留 b 自身的自相关/平滑结构，只破坏与 a 的时间对齐。
    重叠窗口与强自相关下，朴素 t 检验会严重高估显著性，这里不做那个假设。
    """
    table = leadlag_table(a, b, max_lag=max_lag, diff=diff)
    lead = table[table["lag"] >= 1].dropna(subset=["corr"])
    obs = float(lead["corr"].max()) if len(lead) else float("nan")
    best_lag = int(lead.loc[lead["corr"].idxmax(), "lag"]) if len(lead) else 0

    aa = pd.Series(a).astype(float)
    bb = pd.Series(b).astype(float)
    if diff:
        aa, bb = aa.diff(), bb.diff()
    df = pd.concat([aa.rename("a"), bb.rename("b")], axis=1).dropna()
    n = len(df)
    rng = np.random.default_rng(seed)
    null = []
    if n > max_lag + 5:
        av = df["a"].to_numpy()
        bv = df["b"].to_numpy()
        for _ in range(int(n_iter)):
            s = int(rng.integers(1, n))          # 循环平移量
            shifted = np.roll(bv, s)
            vals = []
            for k in range(1, int(max_lag) + 1):
                x = av[k:]
                y = shifted[:-k]
                if len(x) < 5 or np.std(x) == 0 or np.std(y) == 0:
                    continue
                vals.append(float(np.corrcoef(x, y)[0, 1]))
            if vals:
                null.append(max(vals))
    null_arr = np.asarray(null, dtype=float)
    p = float((null_arr >= obs).mean()) if (len(null_arr) and np.isfinite(obs)) else float("nan")
    return {
        "best_lag": best_lag,
        "best_corr": float(lead.loc[lead["lag"] == best_lag, "corr"].iloc[0])
        if len(lead) else float("nan"),
        "max_lead_corr": obs,
        "p_value": p,
        "n": n,
        "n_iter": int(len(null_arr)),
        "null_q95": float(np.quantile(null_arr, 0.95)) if len(null_arr) else float("nan"),
        "table": table.to_dict("records"),
    }


# Deribit DVOL 允许的分辨率（秒）——端点不接受其它值
DVOL_RESOLUTIONS = (1, 60, 3600, 43200, 86400)
DVOL_MAX_POINTS = 900          # 端点每请求返回上限约 1000 点，留点余量


# Deribit 官方波动率指数只覆盖少数币种（官方 DVOL：BTC、ETH）。其余家族
# （如 SOL）没有官方指数 —— 只能借 BTC 作市场基准，且**必须显式标注**：
# 否则读者会把 BTC DVOL 当成该标的的外部对照（2026-09-22 复测中发现
# ETH 家族一直在用 BTC DVOL、而 ETH 自己就有官方指数，等于白丢对照）。
DVOL_OFFICIAL_CURRENCIES = ("BTC", "ETH")


def dvol_currency_for(family: str) -> tuple[str, Optional[str]]:
    """家族 → ``(DVOL 币种, 借用的原始家族)``。

    ``ETH-USD_UM`` → ``("ETH", None)``（有官方指数，是本标的的对照）；
    ``SOL-USD_UM`` → ``("BTC", "SOL")``（无官方指数，借 BTC 作市场基准）。
    第二项非空即表示「这不是本标的自己的指数」，展示层必须标注。
    """
    base = (family or "").split("-")[0].strip().upper()
    if base in DVOL_OFFICIAL_CURRENCIES:
        return base, None
    return "BTC", (base or None)


def dvol_resolution(window_ms: int, *, max_points: int = DVOL_MAX_POINTS) -> int:
    """按窗口长度选 DVOL 分辨率——**必须粗于「窗口/上限」否则会被端点截断**。

    踩过的坑：14 天窗口却请求 60s → 端点只回最近 ~1000 点（≈ 17 小时），
    再重采样到小时就只剩十几次观测，看不出任何东西。故取「能满足点数上限的
    最细分辨率」，宁粗勿细。
    """
    need = max(1, math.ceil(int(window_ms) / 1000.0 / max(1, int(max_points))))
    finer_or_equal = [r for r in DVOL_RESOLUTIONS if r >= need]
    return min(finer_or_equal) if finer_or_equal else DVOL_RESOLUTIONS[-1]


def dvol_series(currency: str = "BTC", *, begin_ms: int, end_ms: int,
                resolution: Optional[int] = None,
                max_points: int = DVOL_MAX_POINTS) -> pd.DataFrame:
    """Deribit 官方波动率指数（DVOL）历史（公开端点，无需 key）。

    返回列 ``dvol``（小数，如 0.52 = 52%），索引 UTC。
    ``resolution=None`` 时按窗口自动选（见 :func:`dvol_resolution`）；显式传入时
    会被抬高到「不被截断」的最小分辨率，保证不会静默少数据。
    """
    auto = dvol_resolution(int(end_ms) - int(begin_ms), max_points=max_points)
    res = auto if not resolution else max(auto, int(resolution))
    url = (f"{DERIBIT_DVOL_URL}?currency={currency}"
           f"&start_timestamp={int(begin_ms)}&end_timestamp={int(end_ms)}"
           f"&resolution={res}")
    req = urllib.request.Request(url, headers={"User-Agent": "nanobot-quant/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    data = (payload.get("result") or {}).get("data") or []
    if not data:
        raise RuntimeError(f"DVOL 返回空（{currency}）: {payload.get('error') or payload}")
    df = pd.DataFrame(data, columns=["ts_ms", "open", "high", "low", "close"])
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df["dvol"] = df["close"].astype(float) / 100.0
    return df[["dvol"]]


# ────────────────────────── 总入口 ──────────────────────────

def analyze_iv_leadlag(family: str = "SOL-USD_UM", *, days: int = 14,
                       bucket: str = "5m", window: int = 12, max_lag: int = 6,
                       target_tenor_days: float = 3.0,
                       mode: str = "atm_median", band_pct: float = 10.0,
                       include_dvol: bool = True,
                       dvol_currency: Optional[str] = None,
                       n_iter: int = 500, cache_dir: Optional[str] = None,
                       progress: Optional[Callable[[str], None]] = None) -> dict:
    """跑一次完整的领先-滞后诊断（H1 / H2 / H3）。

    只读：拉归档成交 + 公开 K 线 + DVOL，不触碰任何交易路径。
    """
    secs = bucket_seconds(bucket)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    begin_ms = end_ms - int(days) * 86400_000
    notes: list[str] = []

    kl = spot_klines(family, begin_ms=begin_ms - secs * 1000,
                     end_ms=end_ms + secs * 1000, bucket=bucket)
    notes.append(f"现货 K 线 {len(kl)} 根（{od.spot_ref_of(family)}）")

    iv = atm_iv_series(family, begin_ms=begin_ms, end_ms=end_ms, bucket=bucket,
                       target_tenor_days=target_tenor_days, cache_dir=cache_dir,
                       spot_df=kl, mode=mode, band_pct=band_pct, progress=progress)
    notes.extend(iv["notes"])
    ivf = iv["frame"]
    if ivf is None or ivf.empty:
        return {"ok": False, "family": family, "bucket": bucket,
                "error": "IV 序列为空（归档区间无成交 / 反解全部被质量门拒绝）",
                "notes": notes}

    rv = realized_vol(kl["close"], window=window, bucket=bucket)
    f1 = compute_f1(kl, atr_n=20, lookback=f1_lookback_for(bucket))

    idx = ivf.index
    tests: dict[str, dict] = {}

    def _series_on_grid(s: pd.Series) -> pd.Series:
        return s.reindex(idx, method="ffill")

    tests["realized_vs_iv"] = shift_test(_series_on_grid(rv), ivf["iv_atm"],
                                        max_lag=max_lag, n_iter=n_iter)
    tests["f1_vs_iv"] = shift_test(_series_on_grid(f1), ivf["iv_atm"],
                                  max_lag=max_lag, n_iter=n_iter)
    tests["realized_vs_f1"] = shift_test(_series_on_grid(rv), _series_on_grid(f1),
                                        max_lag=max_lag, n_iter=n_iter)

    dvol_block: dict = {}
    if include_dvol:
        # 留空 = 按家族推导（有官方指数就用自己，否则借 BTC 并标注）
        if dvol_currency:
            ccy = str(dvol_currency).strip().upper()
            fam_base = (family or "").split("-")[0].strip().upper()
            proxy_for = None if ccy == fam_base else (fam_base or None)
        else:
            ccy, proxy_for = dvol_currency_for(family)
        try:
            dv = dvol_series(ccy, begin_ms=begin_ms - 3600_000,
                             end_ms=end_ms)
            ref = ccy + "-USDT"
            btc = spot_frame(ref, begin_ms=begin_ms, end_ms=end_ms, bucket=bucket)
            rv_btc = realized_vol(btc["close"], window=window, bucket=bucket)
            joined = pd.concat([rv_btc.rename("rv"), dv["dvol"].rename("dvol")],
                               axis=1).dropna()
            coarse = joined.resample(f"{max(secs, 3600)}s").last().dropna()
            if proxy_for:
                notes.append(
                    f"⚠️ {proxy_for} 无官方 DVOL，H3 借用 {ccy} DVOL 作市场基准"
                    "（不是本标的自己的指数）"
                )
            dvol_block = {
                "currency": ccy,
                "proxy_for": proxy_for,
                "points": int(len(coarse)),
                "test": shift_test(coarse["rv"], coarse["dvol"],
                                   max_lag=max_lag, n_iter=n_iter),
                "read": "lag>0 = 已实现波动领先 DVOL（crypto 型）；lag<0 = DVOL 领先（SPX 型）",
            }
        except Exception as e:  # noqa: BLE001 —— 外部对照失败必须显式留痕
            dvol_block = {"error": f"{type(e).__name__}: {e}"}
            _log(f"DVOL 对照失败：{type(e).__name__}: {e}")

    result = {
        "ok": True,
        "family": family,
        "bucket": bucket,
        "window_bars": int(window),
        "days": int(days),
        "span": {"begin": _iso_ms(begin_ms), "end": _iso_ms(end_ms)},
        "series": {
            "iv_buckets": int(len(ivf)),
            "iv_observed": int(ivf["n_points"].notna().sum()) if "n_points" in ivf else None,
            "iv_filled_pct": round(
                100.0 * (1 - float(ivf["n_points"].notna().mean())), 1)
            if "n_points" in ivf else None,
            "mode": mode,
            "band_pct": float(band_pct),
            "iv_first": float(ivf["iv_atm"].dropna().iloc[0]) if ivf["iv_atm"].notna().any() else None,
            "iv_last": float(ivf["iv_atm"].dropna().iloc[-1]) if ivf["iv_atm"].notna().any() else None,
            "iv_median": float(ivf["iv_atm"].median()),
            "rv_median": float(rv.median()) if rv.notna().any() else None,
            "f1_median": float(f1.median()) if f1.notna().any() else None,
            "target_tenor_days": float(target_tenor_days),
        },
        "tests": tests,
        "dvol": dvol_block,
        "notes": notes,
        "trades": iv.get("trades"),
        "rejects": iv.get("rejects"),
    }
    result["markdown"] = markdown(result)
    return result


# ────────────────────────── markdown ──────────────────────────

def _lag_table_md(test: dict) -> str:
    rows = ["| lag（根） | 相关 | n | 读法 |", "|--:|--:|--:|:--|"]
    for r in test.get("table") or []:
        lag = int(r["lag"])
        corr = r["corr"]
        arrow = "← 左序列领先" if lag > 0 else ("→ 右序列领先" if lag < 0 else "同期")
        cs = "—" if corr is None or (isinstance(corr, float) and math.isnan(corr)) else f"{corr:+.3f}"
        rows.append(f"| {lag:+d} | {cs} | {int(r['n'])} | {arrow} |")
    return "\n".join(rows)


def _verdict(test: dict, left: str, right: str) -> str:
    p = test.get("p_value")
    lag = test.get("best_lag")
    corr = test.get("best_corr")
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return f"{left} vs {right}：样本不足，无法判定"
    sig = "显著" if p < 0.05 else "不显著"
    side = f"{left} 领先 {lag} 根" if lag and lag > 0 else (
        f"{right} 领先 {-lag} 根" if lag and lag < 0 else "同期最佳")
    return (f"{left} vs {right}：峰值 lag={lag:+d}（{side}，corr={corr:+.3f}），"
            f"循环平移 p={p:.3f} → **{sig}**")


def markdown(result: dict) -> str:
    if not result.get("ok"):
        return f"## 📉 IV 领先-滞后诊断\n\n- 标的：{result.get('family')}\n- ❌ {result.get('error')}\n"
    s = result["series"]
    t = result["tests"]
    lines = [
        f"## 📉 IV 领先-滞后诊断 · {result['family']}",
        "",
        f"- 周期：{result['bucket']} · 已实现波动窗口 {result['window_bars']} 根"
        f" · 区间 {result['span']['begin']} ~ {result['span']['end']}（UTC，{result['days']} 天）",
        f"- IV 序列：{s['iv_buckets']} 个桶（口径 {s.get('mode')}，带宽 ±{s.get('band_pct')}%，"
        f"有成交桶占比 {None if s.get('iv_filled_pct') is None else round(100 - s['iv_filled_pct'], 1)}%）"
        f" · 中位 {s['iv_median'] * 100:.1f}%"
        f"（首 {None if s['iv_first'] is None else round(s['iv_first'] * 100, 1)}%"
        f" → 末 {None if s['iv_last'] is None else round(s['iv_last'] * 100, 1)}%）"
        f" · 恒定 tenor {s['target_tenor_days']} 天",
        f"- 已实现波动中位：{None if s['rv_median'] is None else round(s['rv_median'] * 100, 1)}%"
        f" · F1 中位：{None if s['f1_median'] is None else round(s['f1_median'], 3)}",
        "",
        "### 判定（lag > 0 = 左侧领先右侧）",
        "",
        f"- {_verdict(t['realized_vs_iv'], '已实现波动', 'IV')}",
        f"- {_verdict(t['f1_vs_iv'], 'F1', 'IV')}",
        f"- {_verdict(t['realized_vs_f1'], '已实现波动', 'F1')}",
        "",
        "### 已实现波动 vs IV（H1）",
        "",
        _lag_table_md(t["realized_vs_iv"]),
        "",
        "### F1 vs IV（H2：平滑假象对照）",
        "",
        _lag_table_md(t["f1_vs_iv"]),
    ]
    dv = result.get("dvol") or {}
    lines += ["", "### 外部对照：DVOL（H3）", ""]
    if dv.get("error"):
        lines.append(f"- ❌ DVOL 拉取失败：{dv['error']}")
    elif dv:
        label = f"{dv['currency']} DVOL · {dv['points']} 点（1h 采样）"
        if dv.get("proxy_for"):
            label += (f"　⚠️ 借用作市场基准（{dv['proxy_for']} 无官方 DVOL，"
                      f"这不是 {dv['proxy_for']} 自己的指数）")
        lines.append(f"- {label}")
        lines.append(f"- {_verdict(dv['test'], '已实现波动', 'DVOL')}")
        if dv.get("read"):
            lines.append(f"- 读法：{dv['read']}")
    else:
        lines.append("- 未跑（include_dvol=false）")
    lines += [
        "",
        "### 口径与局限",
        "",
        "- IV 来自 OKX 归档成交反解（虚值侧优先 + 质量门），ATM 处插值；"
        "无成交的桶前向填充——这会**人为增加 IV 平滑度、压低其领先能力**（对 H1 保守）。",
        "- 显著性用**循环平移零假设**（不假设独立同分布）：重叠窗口 + 强自相关下，"
        "朴素 t 检验会把显著性抬得很虚。",
        "- 归档只滚动保留约 30 天 → 样本上限约 14–28 天；"
        "结论应随盘口采集器（option_tape）积累后再复核。",
    ]
    if result.get("notes"):
        lines += ["", "### 运行留痕", ""] + [f"- {n}" for n in result["notes"]]
    return "\n".join(lines) + "\n"


# ────────────────────────── CLI（本地自测入口）──────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="IV 领先-滞后诊断（只读）")
    ap.add_argument("--family", default="SOL-USD_UM")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--bucket", default="5m")
    ap.add_argument("--window", type=int, default=12)
    ap.add_argument("--max-lag", type=int, default=6)
    ap.add_argument("--tenor", type=float, default=3.0)
    ap.add_argument("--mode", default="atm_median", choices=["atm_median", "smile"])
    ap.add_argument("--band", type=float, default=10.0)
    ap.add_argument("--iter", type=int, default=500)
    ap.add_argument("--no-dvol", action="store_true")
    ap.add_argument("--dvol-ccy", default="",
                    help="DVOL 币种；留空=按家族推导（有官方指数就用自己，"
                         "否则借 BTC 并在报告里标注）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（默认打印 markdown）")
    args = ap.parse_args(argv)

    res = analyze_iv_leadlag(
        args.family, days=args.days, bucket=args.bucket, window=args.window,
        max_lag=args.max_lag, target_tenor_days=args.tenor, n_iter=args.iter,
        mode=args.mode, band_pct=args.band,
        include_dvol=not args.no_dvol, progress=_log,
        dvol_currency=(args.dvol_ccy or None))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res.get("markdown") or json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
