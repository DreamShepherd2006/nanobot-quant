"""A 股期权 IV 分位面板：QMT 历史序列 → 分位（磁盘缓存 + 后台计算）。

为什么单独一层
--------------
``iv_series.iv_series_from_bars()`` 单个标的全量跑 12–60 秒（读懂 600 个合约
分片 + 逐分钟反解），不能放在 HTTP 请求里同步等。这一层负责三件事：

1. **定位数据**：在持久卷上找最新的 QMT 期权 1m 导出目录；
2. **算 + 缓存**：后台线程算一次，结果按（标的, 数据指纹）落磁盘缓存，
   进程重启后直接读缓存，不必重算；
3. **组装面板口径**：DTE 分桶分位 + 30d 常熟 IV 序列，以及「用实时链快照
   插值出的当前 30d IV 与它在历史序列里的分位」（``live_cm_iv``）。

只读：不写台账、不下单、不参与决策。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from nanobot_quant.ashare import iv_series as ivs
from nanobot_quant.ashare import option_chain as oc

DATA_ROOT_ENV = "NANOBOT_ASHARE_DATA"
CACHE_TTL_S = 6 * 3600
CACHE_DIRNAME = "ashare_iv_cache"

# 分位分桶：沿用 iv_series 的 5 档，另加一档「41+」接住远月周线（202703）
BUCKETS = tuple(ivs.DTE_BUCKETS) + ((41, 400),)

_QUANTILES = (5, 25, 50, 75, 95)

_STATE: dict[str, dict] = {}
_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


# ── 数据定位 ────────────────────────────────────────────────

def data_roots() -> list[Path]:
    """候选数据根目录（按优先级）：env → 持久卷默认路径 → 凭证目录旁。"""
    out: list[Path] = []
    env = os.environ.get(DATA_ROOT_ENV)
    if env:
        out.append(Path(env))
    out += [Path("/data/legion/ashare_data"),
            Path("/mnt/workspace/legion/ashare_data")]
    try:
        from nanobot_quant import credential_registry as creg
        out.append(Path(creg._get_storage_dir()).parent / "ashare_data")
    except Exception:  # noqa: BLE001 — 定位失败不该让面板崩
        pass
    seen, uniq = set(), []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def find_option_dir() -> Optional[Path]:
    """最新的 ``qmt-option-1m/run_*`` 目录（没有则 None）。"""
    for root in data_roots():
        base = root / "qmt-option-1m"
        if not base.is_dir():
            continue
        runs = sorted((p for p in base.iterdir() if p.is_dir()),
                      key=lambda p: p.name)
        if runs:
            return runs[-1]
    return None


def _fingerprint(src: Path) -> str:
    """数据指纹：文件数 + 最大 mtime + 总字节（变了才重算）。"""
    n = 0
    mtime = 0.0
    size = 0
    try:
        for p in src.iterdir():
            if p.is_file() and (p.name.startswith("opt1m_") or p.name == "contracts_all.csv"):
                st = p.stat()
                n += 1
                mtime = max(mtime, st.st_mtime)
                size += st.st_size
    except OSError:
        return "0-0-0"
    return f"{n}-{int(mtime)}-{size}"


def _cache_path(src: Path, undl: str) -> Path:
    cache_dir = src.parent.parent / CACHE_DIRNAME
    return cache_dir / f"{ivs.compact_undl(undl)}_{src.name}_{_fingerprint(src)[-24:]}.json"


# ── 汇总（纯函数，便于单测）─────────────────────────────────

def _bucket_label(lo: float, hi: float) -> str:
    return f"{lo:g}–{hi:g}" if hi < 400 else f"{lo:g}+"


def quantiles_of(values: list[float]) -> dict:
    """升序列表 → 分位字典（与 iv_series._quantile 同口径，避免跨模块私有依赖）。"""
    vals = sorted(float(v) for v in values if v is not None)
    n = len(vals)
    out: dict[str, Optional[float]] = {}
    for q in _QUANTILES:
        out[str(q)] = ivs._quantile(vals, q) if n else None  # noqa: SLF001 — 同一模块族
    return out


def pctile(value: Optional[float], values: list[float]) -> Optional[float]:
    """``value`` 在 ``values`` 中的分位（≤ 它的比例 × 100）。无样本 → None。"""
    if value is None:
        return None
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    below = sum(1 for v in vals if v <= value)
    return below / len(vals) * 100.0


def summarize(rows: list[dict], stats: dict) -> dict:
    """IV 序列 → 面板载荷（纯计算，不碰磁盘/网络）。"""
    buckets = []
    for lo, hi in BUCKETS:
        p = ivs.iv_percentile(rows, dte=(lo, hi), min_days=1)
        pc = p["percentiles"]
        buckets.append({
            "label": _bucket_label(lo, hi),
            "lo": lo, "hi": hi,
            "days": p["days"], "minutes": p["minutes"],
            "p5": pc.get(5), "p25": pc.get(25), "p50": pc.get(50),
            "p75": pc.get(75), "p95": pc.get(95),
            "current": p["current"],
        })

    cm = ivs.constant_maturity_iv(rows, target_dte=30.0)
    p30 = ivs.iv_percentile(cm["rows"], dte=(29.9, 30.1), min_days=1)
    days_list = p30["days_list"]
    values = [float(v) for v in p30["values"]]
    cm30 = {
        "days": p30["days"], "minutes": p30["minutes"],
        "p5": p30["percentiles"].get(5), "p25": p30["percentiles"].get(25),
        "p50": p30["percentiles"].get(50), "p75": p30["percentiles"].get(75),
        "p95": p30["percentiles"].get(95),
        "values": values, "days_list": days_list,
        "last_day": days_list[-1] if days_list else None,
        "last_iv": values[-1] if values else None,
        "no_below": cm["stats"]["no_below"], "no_above": cm["stats"]["no_above"],
    }
    total_rows = max(1, int(stats.get("rows") or 0))
    return {
        "buckets": buckets,
        "cm30": cm30,
        "days": len({str(r["ts"])[:8] for r in rows}),
        "minutes": int(stats.get("minutes") or 0),
        "rows": int(stats.get("rows") or 0),
        "iv_ok": int(stats.get("iv_ok") or 0),
        "stale_pct": round(int(stats.get("stale_rows") or 0) / total_rows * 100.0, 2),
        "quality": dict(stats.get("quality") or {}),
    }


def compute(undl: str, src: Optional[Path] = None) -> dict:
    """真跑一次（慢，调用方应放在后台线程）。"""
    src = src or find_option_dir()
    if src is None:
        raise FileNotFoundError("找不到 QMT 期权 1m 导出目录（qmt-option-1m/run_*）")
    t0 = time.time()
    res = ivs.iv_series_from_bars(src, undl=undl)
    payload = {
        "ok": True,
        "status": "ready",
        "symbol": str(undl),
        "source": str(src),
        "run": src.name,
        "months": list(res["stats"].get("months") or []),
        **summarize(res["rows"], res["stats"]),
        "elapsed_s": round(time.time() - t0, 1),
    }
    return payload


# ── 状态机（后台算 + 磁盘缓存）───────────────────────────────

def _state_of(undl: str) -> dict:
    with _GUARD:
        return _STATE.setdefault(undl, {"status": "idle", "payload": None,
                                        "error": "", "started": 0.0})


def _lock_of(undl: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(undl, threading.Lock())


def _read_cache(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("ok") else None
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:  # 缓存写失败不影响本次结果，但要可见
        print(f"[ASHARE-IV] 缓存写入失败 {path}: {type(e).__name__}: {e}", flush=True)


def _run_in_thread(undl: str, src: Path, cache: Path) -> None:
    st = _state_of(undl)
    lock = _lock_of(undl)
    try:
        payload = compute(undl, src)
        _write_cache(cache, payload)
        with lock:
            st.update({"status": "ready", "payload": payload, "error": "",
                       "done_at": time.time()})
        print(f"[ASHARE-IV] {undl} 计算完成 {payload.get('elapsed_s', '?')}s "
              f"（{payload.get('run')} · {payload.get('days', 0)} 天 · "
              f"30d 样本 {(payload.get('cm30') or {}).get('days', 0)}）",
              flush=True)
    except Exception as e:  # noqa: BLE001
        with lock:
            st.update({"status": "error", "payload": None,
                       "error": f"{type(e).__name__}: {e}"})
        print(f"[ASHARE-IV] {undl} 计算失败: {type(e).__name__}: {e}", flush=True)


def panel(undl: str, *, force: bool = False) -> dict:
    """IV 分位面板：命中缓存直接返回，否则起后台线程算（返回 computing）。"""
    undl = str(undl)
    src = find_option_dir()
    if src is None:
        return {"ok": False, "status": "no_data",
                "error": "找不到 QMT 期权 1m 导出目录（qmt-option-1m/run_*）",
                "data_roots": [str(p) for p in data_roots()]}

    st = _state_of(undl)
    cache = _cache_path(src, undl)
    with _lock_of(undl):
        if force:
            st.update({"status": "idle", "payload": None, "error": ""})
        if st["status"] == "ready" and st.get("payload"):
            age = time.time() - float(st.get("done_at") or 0.0)
            if age < CACHE_TTL_S and (st["payload"].get("run") == src.name):
                return {**st["payload"], "status": "ready", "cached": True,
                        "age_s": int(age)}
        if st["status"] == "computing":
            return {"ok": True, "status": "computing", "symbol": undl,
                    "run": src.name, "elapsed_s": round(time.time() - st["started"], 1)}

    cached = None if force else _read_cache(cache)
    if cached:
        with _lock_of(undl):
            st.update({"status": "ready", "payload": cached, "error": "",
                       "done_at": time.time()})
        return {**cached, "status": "ready", "cached": True,
                "from_disk": True, "age_s": 0}

    with _lock_of(undl):
        if st["status"] == "computing":
            return {"ok": True, "status": "computing", "symbol": undl,
                    "run": src.name, "elapsed_s": 0}
        st.update({"status": "computing", "payload": None, "error": "",
                   "started": time.time()})
    threading.Thread(target=_run_in_thread, args=(undl, src, cache),
                     name=f"ashare-iv-{ivs.compact_undl(undl)}", daemon=True).start()
    return {"ok": True, "status": "computing", "symbol": undl,
            "run": src.name, "elapsed_s": 0}


def reset() -> None:
    """测试/排障用：清进程内状态（磁盘缓存保留）。"""
    with _GUARD:
        _STATE.clear()


# ── 实时链 → 当前 30d IV ───────────────────────────────────

def live_cm_iv(snap: dict, target_dte: float = 30.0) -> dict:
    """用实时链快照插值出目标 DTE 的 IV（**不外推**）。

    每个到期取「最接近 F 的行权价」的 call/put IV 均值作该档 ATM IV，再按 DTE
    线性插值到 ``target_dte``。返回 ``{iv, dte_lo, dte_hi, legs}``；无法插值
    时 ``iv=None`` 并给出 ``note``。
    """
    rows = snap.get("rows") or []
    by_exp: dict[str, list[dict]] = {}
    for r in rows:
        by_exp.setdefault(str(r.get("expiry") or ""), []).append(r)

    legs: list[dict] = []
    for expiry, fw in (snap.get("forwards") or {}).items():
        f = (fw or {}).get("f")
        rs = by_exp.get(expiry) or []
        if not f or not rs:
            continue
        near = sorted({float(r["strike"]) for r in rs if r.get("strike")},
                      key=lambda k: (abs(k - f), k))[:1]
        if not near:
            continue
        k = near[0]
        ivs_side = {}
        for r in rs:
            if float(r.get("strike") or 0) != k:
                continue
            if r.get("iv") is None:
                continue
            ivs_side[r.get("right")] = float(r["iv"])
        if not ivs_side:
            continue
        atm = sum(ivs_side.values()) / len(ivs_side)
        dtes = [float(r["dte"]) for r in rs if r.get("dte") is not None]
        dte = sorted(dtes)[len(dtes) // 2] if dtes else 0.0
        if dte <= 0:
            continue
        legs.append({"expiry": expiry, "dte": dte, "strike": k, "iv": atm,
                     "atm_c": ivs_side.get("C"), "atm_p": ivs_side.get("P")})

    legs.sort(key=lambda x: x["dte"])
    out = {"target_dte": float(target_dte), "iv": None, "dte_lo": None,
           "dte_hi": None, "legs": legs, "note": ""}
    if not legs:
        out["note"] = "无可用 IV 档（盘口缺失或不交易）"
        return out
    lo = None
    for leg in legs:
        if leg["dte"] <= target_dte + 1e-9:
            lo = leg
    hi = next((leg for leg in legs if leg["dte"] >= target_dte - 1e-9), None)
    if lo is None or hi is None:
        out["note"] = ("最短到期仍长于目标 DTE，不外推" if lo is None
                       else "最长到期仍短于目标 DTE，不外推")
        out["dte_lo"] = lo["dte"] if lo else None
        out["dte_hi"] = hi["dte"] if hi else None
        return out
    if abs(lo["dte"] - hi["dte"]) < 1e-9:
        iv = lo["iv"]
    else:
        w = (target_dte - lo["dte"]) / (hi["dte"] - lo["dte"])
        iv = lo["iv"] + (hi["iv"] - lo["iv"]) * w
    out.update({"iv": iv, "dte_lo": lo["dte"], "dte_hi": hi["dte"]})
    return out


__all__ = [
    "DATA_ROOT_ENV", "CACHE_TTL_S", "BUCKETS", "data_roots", "find_option_dir",
    "quantiles_of", "pctile", "summarize", "compute", "panel", "reset",
    "live_cm_iv",
]
