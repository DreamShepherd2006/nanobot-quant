"""期权盘口采集器（A 项：只读采集，2026-09-22 讨论定稿）。

**为什么需要它**：要验证两个假设（见 ``docs/quant-system.md`` §33.37）：

① **realized → implied 的方向性**：美股是期权市场定价未来（implied → realized），
   而 crypto 里高杠杆永续的强平级联可能是波动的第一现场（realized → implied）。
   若成立，则我们的已实现波动类传感器（F1）在这个市场上具有真实信息优势。
② **价差 / 最优档深度作为「流动性代理」**：价差是「定价能力本身」的损耗，
   对卖方尤其致命——我们成交吃的是 bid。

两者都需要**期权盘口的时间序列**，而系统现状是：ticker 只有 8s 内存缓存、即用即弃；
OKX 官方归档只有**成交明细**（无报价）⇒ 历史拿不到，只能从启用之日起自己采。

**只读边界（硬约束）**：

- 只调公开行情端点：一次 ``get_tickers(instType="OPTION")`` 拿到全部在售合约的
  bidPx/askPx/bidSz/askSz/markVol/delta；可选对少量合约额外取 ``get_books`` 多档；
- **不读持仓、不写台账、不下单、不参与任何交易决策**；
- 失败只记账（``last_error``）并打 stderr，绝不阻塞策略循环（同一进程内并行运行）。

**启停**：``option_params.json`` 的 ``tape`` 段（期权页「📼 盘口采集」小节），
默认 ``enabled=false`` —— 不擅自开采集。空间重建后需在页面点一次「保存」才拉起
（与策略循环同一机制，属既定行为）。

**落盘**：``{storage_dir}/option_tape/tape_YYYYMMDD.jsonl``，一天一文件、
每行一次采样（append-only、跨重启保留、可 grep）。行格式::

    {"ts": "2026-09-22T03:10:05Z", "ts_ms": 1758510605000,
     "cfg": {"families": [...], "expiries": 3, "band_pct": 12.0, ...},
     "spot": {"SOL-USD_UM": 231.5},
     "rows": [{"i": "SOL-USD_UM-260926-220-P", "b": 1.2, "a": 1.3,
               "bs": 30.0, "as": 20.0, "mv": 0.72, "dv": -0.21}]}

行字段：``i``=instId、``b``/``a``=bid/ask（缺报价为 null）、``bs``/``as``=最优档量、
``mv``=markVol（OKX 官方口径 IV）、``dv``=delta。报价为 0 一律记 null，
避免下游把「无报价」当成「价格为 0」。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import okx_options_data as od
from . import okx_options_trade as ot
from . import okx_sdk
from .live_runner_base import LiveRunnerBase

TAPE_DIR_NAME = "option_tape"

DEFAULT_INTERVAL_S = 60
INTERVAL_RANGE = (10, 3600)

# 采集参数（option_params.json 的 tape 段；页面可改）
DEFAULT_TAPE: dict = {
    "enabled": False,
    "interval_s": DEFAULT_INTERVAL_S,
    "families": ["SOL-USD_UM"],   # 采集哪些标的家族
    "expiries": 3,                # 最近 N 个未到期档
    "band_pct": 12.0,             # strike 相对现货的窗口（±%）
    "max_per_side": 8,            # 每档每侧最多收录多少行权价（按贴近现货排序）
    "depth": False,               # 是否额外采多档盘口（每家族 1 put + 1 call 的额外请求）
    "depth_levels": 5,
}


# ── 参数 ──────────────────────────────────────────────────

def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _clamp_float(v, lo: float, hi: float, default: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _normalize(raw: dict) -> dict:
    """校验 + 夹取（非法值回退默认，不抛错——采集参数不该让页面 500）。"""
    cfg = dict(DEFAULT_TAPE)
    for k in DEFAULT_TAPE:
        if k in raw:
            cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["interval_s"] = _clamp_int(cfg.get("interval_s"), *INTERVAL_RANGE,
                                   default=DEFAULT_INTERVAL_S)
    fams = cfg.get("families")
    if isinstance(fams, str):
        fams = [f.strip() for f in fams.split(",")]
    if not isinstance(fams, (list, tuple, set)):
        fams = []                             # 非法类型（如 int）→ 回默认家族
    fams = [str(f).strip() for f in fams if str(f).strip()]
    cfg["families"] = [f for f in fams if f in od.FAMILIES] or list(DEFAULT_TAPE["families"])
    cfg["expiries"] = _clamp_int(cfg.get("expiries"), 1, 10, 3)
    cfg["band_pct"] = _clamp_float(cfg.get("band_pct"), 1.0, 50.0, 12.0)
    cfg["max_per_side"] = _clamp_int(cfg.get("max_per_side"), 1, 30, 8)
    cfg["depth"] = bool(cfg.get("depth"))
    cfg["depth_levels"] = _clamp_int(cfg.get("depth_levels"), 1, 10, 5)
    return cfg


def tape_config() -> dict:
    raw = ot.load_option_params().get("tape") or {}
    return _normalize(raw if isinstance(raw, dict) else {})


def save_tape_config(**fields) -> dict:
    """保存采集参数（只写 option_params.json 的 tape 段，不影响 live 段）。"""
    cur = tape_config()
    for k, v in fields.items():
        if k in DEFAULT_TAPE:
            cur[k] = v
    cur = _normalize(cur)
    ot.save_option_params(tape=cur)
    return cur


# ── 落盘 ──────────────────────────────────────────────────

def tape_dir() -> Path:
    from .credential_registry import _get_storage_dir
    return Path(_get_storage_dir()) / TAPE_DIR_NAME


def sample_path(day: str | None = None) -> Path:
    d = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return tape_dir() / f"tape_{d}.jsonl"


def _append_sample(rec: dict) -> Path:
    p = sample_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return p


def load_tape(day: str | None = None, limit: int | None = None) -> list[dict]:
    """读某天（默认今天，UTC）的采样；``limit`` 取**尾部** N 条。"""
    p = sample_path(day)
    if not p.exists():
        return []
    out: list[dict] = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out[-limit:] if limit else out


def tape_stats(day: str | None = None) -> dict:
    """当天采样统计（页面状态行用）。文件缺失/损坏都返回零值，不抛错。"""
    p = sample_path(day)
    st = {"day": day or datetime.now(timezone.utc).strftime("%Y%m%d"),
          "samples": 0, "rows": 0, "first_ts": None, "last_ts": None,
          "bytes": 0, "error": ""}
    if not p.exists():
        return st
    try:
        st["bytes"] = p.stat().st_size
        recs = load_tape(day)
    except OSError as e:
        st["error"] = f"{type(e).__name__}: {e}"
        return st
    st["samples"] = len(recs)
    st["rows"] = sum(len(r.get("rows") or []) for r in recs)
    if recs:
        st["first_ts"] = recs[0].get("ts")
        st["last_ts"] = recs[-1].get("ts")
    return st


def flatten(samples: list[dict]) -> list[dict]:
    """采样记录 → 扁平行（每条报价一行），供分析/导出复用。

    行：``{ts, ts_ms, family, inst, expiry_ms, strike, right, spot,
    bid, ask, bid_sz, ask_sz, mark_vol, delta, mid, spread_pct}``
    """
    out: list[dict] = []
    for rec in samples or []:
        ts = rec.get("ts")
        ts_ms = rec.get("ts_ms")
        spots = rec.get("spot") or {}
        for r in rec.get("rows") or []:
            inst = str(r.get("i") or "")
            try:
                meta = od.parse_inst_id(inst)
            except Exception:  # noqa: BLE001 — 解析不了的旧行跳过，不炸分析
                continue
            fam = meta["instFamily"]
            bid, ask = r.get("b"), r.get("a")
            mid = None
            spread = None
            if bid is not None and ask is not None:
                mid = (bid + ask) / 2.0
                if mid > 0:
                    spread = (ask - bid) / mid
            out.append({
                "ts": ts, "ts_ms": ts_ms, "family": fam, "inst": inst,
                "expiry_ms": int(meta["expTime"]),
                "strike": float(meta["stk"]), "right": meta["optType"],
                "spot": spots.get(fam),
                "bid": bid, "ask": ask,
                "bid_sz": r.get("bs"), "ask_sz": r.get("as"),
                "mark_vol": r.get("mv"), "delta": r.get("dv"),
                "mid": mid, "spread_pct": spread,
            })
    return out


# ── 采样 ──────────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _quote(v):
    """报价：<=0（含 OKX 的 "0"/"" 空报价）记 None —— 别让下游把无报价当 0 价。"""
    f = _num(v)
    return None if (f is None or f <= 0) else f


def _row(inst: str, raw: dict) -> dict:
    return {
        "i": inst,
        "b": _quote(raw.get("bidPx")),
        "a": _quote(raw.get("askPx")),
        "bs": _quote(raw.get("bidSz")),
        "as": _quote(raw.get("askSz")),
        "mv": _num(raw.get("markVol")),
        "dv": _num(raw.get("delta")),
    }


def pick_instruments(rows: list[dict], family: str, spot: float | None,
                     cfg: dict, now_ms: int) -> list[dict]:
    """从全市场 ticker 里挑出该家族的采集对象。

    规则（确定性、可回归）：未到期 → 最近 ``expiries`` 个到期档 →
    strike 落在现货 ``±band_pct`` → 每档每侧按贴近现货取前 ``max_per_side``。
    spot 缺失时只做到期过滤（不按带宽裁），并把行照常记下（分析侧可容错）。
    """
    cand: list[tuple] = []
    for raw in rows or []:
        inst = str(raw.get("instId") or "")
        if not inst:
            continue
        try:
            meta = od.parse_inst_id(inst)
        except Exception:  # noqa: BLE001
            continue
        if meta["instFamily"] != family:
            continue
        try:
            exp_ms = int(meta["expTime"])
        except (TypeError, ValueError):
            continue
        if exp_ms <= now_ms:
            continue
        cand.append((exp_ms, float(meta["stk"]), meta["optType"], inst, raw))
    if not cand:
        return []
    exps = sorted({c[0] for c in cand})[: cfg["expiries"]]
    band = float(cfg["band_pct"]) / 100.0
    per: dict[tuple, list] = {}
    for exp_ms, strike, right, inst, raw in cand:
        if exp_ms not in exps:
            continue
        dist = 0.0
        if spot and spot > 0:
            dist = abs(strike / spot - 1.0)
            if dist > band:
                continue
        per.setdefault((exp_ms, right), []).append((dist, strike, inst, raw))
    picked: list[dict] = []
    for _, items in sorted(per.items()):
        items.sort(key=lambda x: (x[0], x[1]))
        for _dist, _strike, inst, raw in items[: cfg["max_per_side"]]:
            picked.append(_row(inst, raw))
    picked.sort(key=lambda x: x["i"])
    return picked


def _depth_for(family: str, spot: float | None, picked: list[dict],
               levels: int) -> dict:
    """额外采多档盘口：该家族最近到期、最贴近现货的 1 put + 1 call。

    额外请求数 = 2/家族（默认关；开了才付这个配额）。单条失败只跳过该条。
    """
    if not picked:
        return {}
    near = [r for r in picked if r["i"].endswith(("-P", "-C"))]
    if not near:
        return {}
    near.sort(key=lambda r: r["i"])
    exps = []
    for r in near:
        try:
            exps.append((int(od.parse_inst_id(r["i"])["expTime"]), r["i"]))
        except Exception:  # noqa: BLE001
            continue
    if not exps:
        return {}
    first_exp = min(e for e, _ in exps)
    chosen: dict[str, str] = {}
    for right in ("P", "C"):
        group = []
        for exp_ms, inst in exps:
            if exp_ms != first_exp or not inst.endswith("-" + right):
                continue
            try:
                k = float(od.parse_inst_id(inst)["stk"])
            except Exception:  # noqa: BLE001
                continue
            dist = abs(k / spot - 1.0) if (spot and spot > 0) else 0.0
            group.append((dist, inst))
        if group:
            group.sort()
            chosen[right] = group[0][1]
    books: dict[str, dict] = {}
    for inst in chosen.values():
        try:
            book = ot.order_book(inst, levels)
        except Exception as e:  # noqa: BLE001 — 深度是加分项，失败不影响主采样
            books[inst] = {"error": f"{type(e).__name__}: {e}"}
            continue
        books[inst] = book
    return books


def sample_once(cfg: dict | None = None, persist: bool = True) -> dict:
    """采一次。

    一次 bulk ticker 请求覆盖全部在售合约（配额友好），按家族过滤后落盘。
    ``persist=False`` 只返回记录（测试/预览用）。
    """
    cfg = _normalize(cfg or tape_config())
    now = datetime.now(timezone.utc)
    now_ms = int(time.time() * 1000)
    tickers = okx_sdk.check(okx_sdk.market().get_tickers(instType="OPTION")) or []
    spot: dict[str, float | None] = {}
    rows: list[dict] = []
    for fam in cfg["families"]:
        s = None
        try:
            s = od.spot_price(fam)
        except Exception:  # noqa: BLE001
            s = None
        spot[fam] = s
        rows.extend(pick_instruments(tickers, fam, s, cfg, now_ms))
    rec: dict = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_ms": now_ms,
        "cfg": {k: cfg[k] for k in ("families", "expiries", "band_pct",
                                    "max_per_side", "depth", "depth_levels")},
        "spot": spot,
        "rows": rows,
    }
    if cfg["depth"]:
        books: dict[str, dict] = {}
        for fam in cfg["families"]:
            fam_rows = [r for r in rows if r["i"].startswith(fam + "-")]
            books.update(_depth_for(fam, spot.get(fam), fam_rows,
                                    cfg["depth_levels"]))
        if books:
            rec["books"] = books
    if persist:
        _append_sample(rec)
    return rec


# ── Runner（复用 LiveRunnerBase 的机制层，与 TD/期权循环同构）─────

class _OptionTapeRunner(LiveRunnerBase):
    """盘口采集 runner —— 只读副作用，与策略循环并行、互不干扰。"""

    LOG_PREFIX = "[OPT-TAPE]"
    EVENTS_NAME = "option_tape_events.jsonl"   # 采集器不写事件文件（do_round 不调）
    DEFAULT_INTERVAL_S = DEFAULT_INTERVAL_S
    INTERVAL_RANGE = INTERVAL_RANGE

    def __init__(self) -> None:
        super().__init__()
        self._logged_start = False

    def load_config(self) -> dict:
        return tape_config()

    def config_changed(self, old: dict, new: dict) -> bool:
        return dict(old or {}) != dict(new or {})

    def storage_dir(self) -> Path:
        return tape_dir()

    def do_round(self) -> dict:
        cfg = self.load_config()
        if not self._logged_start:
            self._logged_start = True
            self._log(f"采集器启动 · interval={self._interval_s(cfg)}s · "
                      f"families={cfg['families']} · 到期档={cfg['expiries']} · "
                      f"带宽=±{cfg['band_pct']}% · 每侧上限={cfg['max_per_side']} · "
                      f"多档盘口={'开' if cfg['depth'] else '关'}")
        rec = sample_once(cfg, persist=True)
        n = len(rec.get("rows") or [])
        self._bump("samples")
        self._bump("rows", n)
        self._log(f"采样 {rec['ts']} → {n} 行 · spot="
                  f"{ {k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in (rec.get('spot') or {}).items()} }")
        return {"rows": n, "ts": rec["ts"]}


_RUNNER: _OptionTapeRunner | None = None


def _runner() -> _OptionTapeRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = _OptionTapeRunner()
    return _RUNNER


def sample_now(cfg: dict | None = None, persist: bool = False) -> dict:
    """立即采一次（预览/测试/诊断用；默认不落盘）。"""
    return sample_once(cfg, persist=persist)


def sync() -> dict:
    """按 tape 配置启/停/重启采集（幂等）。"""
    return _runner().sync()


def stop() -> dict:
    return _runner().stop()


def state() -> dict:
    """采集器状态 + 当天落盘统计（页面状态行）。"""
    st = _runner().status()
    return {
        "running": bool(st.get("thread_alive")),
        "last_run": st.get("last_run"),
        "last_error": st.get("last_error") or "",
        "total_samples": int((st.get("totals") or {}).get("samples") or 0),
        "total_rows": int((st.get("totals") or {}).get("rows") or 0),
        "stats": tape_stats(),
    }


__all__ = [
    "TAPE_DIR_NAME", "DEFAULT_TAPE", "DEFAULT_INTERVAL_S", "INTERVAL_RANGE",
    "tape_config", "save_tape_config", "tape_dir", "sample_path",
    "load_tape", "tape_stats", "flatten",
    "pick_instruments", "sample_once", "sample_now",
    "sync", "stop", "state",
]
