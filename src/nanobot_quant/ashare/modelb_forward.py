"""模型 B **前向记录器**（只读，2026-11-28 立项）。

**为什么要有它**：模型 B（510500 等 ETF 期权自营卖方）的全部结论目前都建立在
2026-04-23 起的 146–218 个交易日历史样本上。历史样本已经用尽（C176 封存后
结论只到「描述性、未经独立前向样本验证」），而**观测单位是交易日 / 稀疏事件**，
只能靠时间向前积累。本模块就是那条积累通道：每天在决策时刻记一份「状态读数 +
候选合约报价」，到期后把结果**另写**到 outcomes 文件里。

**与回测的区别（关键）**：这里不做撮合、不做选档策略、不写台账、不下单，
只记录「当时能看到什么」。分析脚本（离线、只读）读这三个文件做裁决。

**冻结口径**（``rule_version = modelb_state_v2``，改动必须升版本号）：

- 15m bar = **按交易时段栅格**归并（上午 09:30 起、下午 13:00 起，每 15 分钟一格，
  不足 15 根的格丢弃）：``o`` = 格内首根 close、``h`` = max(close)、``l`` = min(low)、
  ``c`` = 格内末根 close。（v1 是「按当日 1m 序列从头每 15 根切块」，与 C176 脚本
  一致；改用栅格是因为 1m 源按自然分钟对齐、不同源首根不同（QMT 下午从 13:01 起、
  Tencent 从 13:00 起），切片口径会让**同一交易日**在不同源上产出不同的块起点，
  合并时同一根 15m bar 会以两个 ts 各存一份 ⇒ 缓存被污染。栅格口径与源无关、
  可重复；代价是块边界与 v1 略有差异（v1 在 11:30 处有一根跨午休的块），
  故升版本号，且 v1 从未产出过落盘记录。）
- ADX(14) 用 Wilder 平滑，滞回 ``>=25`` 进趋势 / ``<=18`` 出趋势；
- 趋势内用 EMA55 的 5 根斜率定 ``U``（>0）/ ``D``（<=0），趋势外为 ``R``；
- 前 60 根 15m bar 之前状态记 ``None``（预热不足）；
- 决策时刻 ``14:45:00``（CST），只用**已完成**的 15m bar（``bar_ts + 15min <= 决策``）；
- 候选合约 = 距今 4–35 天的 put 中 ``|delta|`` 最接近 0.25 的那个（
  ``dte`` 窗口只用于标记 ``in_dte_window``，**不过滤**——记录要反映市场、不实现策略）。

**落盘**（``{storage}/ashare_tape/research/``，全部 append-only、跨重启保留）：

============================  ===================================================
``state_observations.jsonl``  每个标的每个交易日一行：状态 + ADX/EMA/斜率/spot
``option_observations.jsonl`` 每个标的每个交易日一行：候选 put 的 bid/ask/IV/delta
``forward_outcomes.jsonl``    结算行（按 ``observation_id`` 引用上面的行）
``modelb_forward_events.jsonl`` runner 动作流水（与 tape 同构，落在 ``ashare_tape/``）
============================  ===================================================

**两条硬约束**：

1. **禁止回改历史**：outcomes 单独写入、以 ``observation_id`` 引用原始观察；任何
   已写行都不许编辑（防止未来数据泄漏进「当时的读法」）。
2. **不改写原始行情**：本模块只读 K 线 / 期权链 / tape 文件，全部产出落在
   ``research/`` 下。``bars_15m.json`` 是**可重建的缓存**（不是证据），允许重写。

**只读边界**：不 import 任何 broker / 台账 / 下单模块；不参与决策；不受交易开关
（``td_enabled`` / ``live.*`` / 逐笔确认）影响——它自己只有一个 ``enabled``。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..live_runner_base import LiveRunnerBase
from .. import credential_registry as creg
from . import option_chain as oc
from . import tape
from .td_panel import drop_unclosed, fetch_klines, tx_code

CN = tape.CN
RESEARCH_DIR_NAME = "research"
PARAMS_NAME = "modelb_forward_params.json"
EVENTS_NAME = "modelb_forward_events.jsonl"

STATE_FILE = "state_observations.jsonl"
OPTION_FILE = "option_observations.jsonl"
OUTCOME_FILE = "forward_outcomes.jsonl"
BARS_CACHE = "bars_15m.json"

RULE_VERSION = "modelb_state_v2"
SCHEMA_VERSION = 1

# ── 冻结参数（改动必须升 rule_version，否则历史读法与现读法不可比）──────
DECIDE_HHMMSS = "144500"
ADX_IN, ADX_OUT, EMA_N, SLOPE_K = 25.0, 18.0, 55, 5
MIN_BARS = 60               # 前 60 根 15m bar 之前状态记 None
BAR_SPAN = 900              # 15m = 900 秒
# 交易时段（分钟数，闭区间）：上午 09:30–11:30、下午 13:00–15:00；15m 栅格从段首起算
BAR_SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
BLOCK_TOL = 3               # 一块至少 15-BLOCK_TOL 根：吸收各源 1 分钟标签差异
DELTA_TARGET, DELTA_TOL = 0.25, 0.04
DTE_MIN, DTE_MAX = 4, 35
HORIZON_DAYS = 5            # 前向结算持有交易日数（冻结）
FEE_SELL, FEE_BUY = 1.2, 1.7   # 期权每张手续费（元）；卖出开仓 / 买入平仓

DEFAULT_INTERVAL_S = 300
INTERVAL_RANGE = (60, 3600)

DEFAULT_CFG: dict = {
    "enabled": True,          # 只读记录器：不碰资金/订单，随网关自启（见模块 docstring）
    "interval_s": DEFAULT_INTERVAL_S,
    "underlyings": list(tape.POOL_CODES),
    "decide_hm": 1445,
    "horizon_days": HORIZON_DAYS,
    "delta_target": DELTA_TARGET,
    "expiries": 2,            # 盘口/链快照取最近 N 个到期档
    "band_pct": 12.0,
    "seed_days": 60,          # 本地 QMT CSV 预热天数（没有就靠实时累积）
    "bars_keep": 2000,        # 15m bar 缓存上限（约 6 年）
}


# ══════════════════════ 参数 ══════════════════════

def params_path() -> Path:
    return Path(creg._get_storage_dir()) / PARAMS_NAME


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return min(max(n, lo), hi)


def _clamp_float(v, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(f, lo), hi)


def _normalize(cfg: dict | None) -> dict:
    c = dict(DEFAULT_CFG)
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    c["enabled"] = bool(c.get("enabled"))
    c["interval_s"] = _clamp_int(c.get("interval_s"), *INTERVAL_RANGE, DEFAULT_INTERVAL_S)
    c["decide_hm"] = _clamp_int(c.get("decide_hm"), 0, 2359, 1445)
    c["horizon_days"] = _clamp_int(c.get("horizon_days"), 1, 60, HORIZON_DAYS)
    c["delta_target"] = _clamp_float(c.get("delta_target"), 0.05, 0.60, DELTA_TARGET)
    c["expiries"] = _clamp_int(c.get("expiries"), 1, 6, 2)
    c["band_pct"] = _clamp_float(c.get("band_pct"), 3.0, 30.0, 12.0)
    c["seed_days"] = _clamp_int(c.get("seed_days"), 0, 250, 60)
    c["bars_keep"] = _clamp_int(c.get("bars_keep"), 120, 20000, 2000)
    undls = [u for u in (c.get("underlyings") or []) if u in tape.POOL_CODES]
    c["underlyings"] = undls or list(tape.POOL_CODES)
    return c


def forward_config() -> dict:
    p = params_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return _normalize(d if isinstance(d, dict) else {})
    except (OSError, json.JSONDecodeError):
        return _normalize({})


def save_forward_config(**kw) -> dict:
    cfg = _normalize({**forward_config(), **kw})
    p = params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cfg


# ══════════════════════ 落盘（append-only） ══════════════════════

def research_dir() -> Path:
    return tape.tape_dir() / RESEARCH_DIR_NAME


def state_path() -> Path:
    return research_dir() / STATE_FILE


def option_path() -> Path:
    return research_dir() / OPTION_FILE


def outcome_path() -> Path:
    return research_dir() / OUTCOME_FILE


def bars_cache_path() -> Path:
    return research_dir() / BARS_CACHE


def _append(path: Path, rec: dict) -> None:
    """append-only 追加一行；目录不存在就建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def _load(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


def load_states(limit: int | None = None) -> list[dict]:
    return _load(state_path(), limit)


def load_options(limit: int | None = None) -> list[dict]:
    return _load(option_path(), limit)


def load_outcomes(limit: int | None = None) -> list[dict]:
    return _load(outcome_path(), limit)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ══════════════════════ 冻结状态机（modelb_state_v1） ══════════════════════

def _block_minute(mins: int) -> int:
    """分钟数 → 所属 15m 栅格起点（分钟数）；不在交易时段返回 -1。"""
    for s, e in BAR_SESSIONS:
        if s <= mins <= e:
            return s + (mins - s) // 15 * 15
    return -1


def to_15m(df) -> list[tuple]:
    """1m DataFrame → ``[(ts, o, h, l, c)]``（15m 交易时段栅格，源无关）。

    栅格 = 段首（09:30 / 13:00）起每 15 分钟一格；不满 15 根的允许差 ``BLOCK_TOL`` 根
    （各源 1 分钟标签差异：QMT 标的为 bar 结束时刻、实时源多为开始时刻，同一交易
    日的下午首格会差一根），格内不足 15-BLOCK_TOL 根的丢弃（窗口截断 / 中午 11:30
    与收盘集合竞价 15:00 这类孤立 bar）。这样 QMT CSV 与实时源落在**同一套 ts 栅格**
    上，合并时不会出现「同一根 bar 两个 ts」的缓存污染。``h`` 用 close 近似、``l`` 用
    low，与 C176 同口径。
    """
    out: list[tuple] = []
    if df is None or len(df) == 0 or "dt" not in getattr(df, "columns", []):
        return out
    d = df.copy()
    d["dt"] = d["dt"].astype("datetime64[ns]")
    d = d.dropna(subset=["dt"]).sort_values("dt")
    d = d.drop_duplicates(subset=["dt"], keep="last")
    if d.empty:
        return out
    mins = d["dt"].dt.hour * 60 + d["dt"].dt.minute
    d["_blk"] = [_block_minute(int(m)) for m in mins]
    d = d[d["_blk"] >= 0]
    if d.empty:
        return out
    days = (d["date"].astype(str) if "date" in d.columns
            else d["dt"].dt.strftime("%Y%m%d"))
    d = d.assign(_day=days)
    for (day, blk), g in d.groupby(["_day", "_blk"], sort=True):
        if len(g) < 15 - BLOCK_TOL or len(g) > 15:
            continue
        closes = g["close"].astype(float)
        lows = g["low"].astype(float)
        b = int(blk)
        ts = int(f"{day}{b // 60:02d}{b % 60:02d}00")
        out.append((ts, float(closes.iloc[0]), float(closes.max()),
                    float(lows.min()), float(closes.iloc[-1])))
    out.sort(key=lambda x: x[0])
    return out


def wilder(vals, n: int) -> list:
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    s = sum(vals[:n])
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = s - s / n + vals[i]
        out[i] = s
    return out


def adx14(bars, n: int = 14) -> list:
    tr, pdm, ndm = [], [], []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        ph, pl = bars[i - 1][2], bars[i - 1][3]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    str_, sp, sn = wilder(tr, n), wilder(pdm, n), wilder(ndm, n)
    dx = [None] * len(bars)
    for i in range(len(bars)):
        if i >= n and str_[i - 1] and sp[i - 1] is not None and sn[i - 1] is not None:
            pdi = 100 * sp[i - 1] / str_[i - 1]
            ndi = 100 * sn[i - 1] / str_[i - 1]
            if pdi + ndi > 0:
                dx[i] = 100 * abs(pdi - ndi) / (pdi + ndi)
    vals = [x for x in dx if x is not None]
    sm = wilder(vals, n)
    out, k = [None] * len(bars), 0
    for i in range(len(bars)):
        if dx[i] is not None:
            out[i] = sm[k] / n if sm[k] is not None else None
            k += 1
    return out


def ema(vals, n: int) -> list:
    a = 2 / (n + 1)
    out, prev = [], None
    for v in vals:
        prev = v if prev is None else a * v + (1 - a) * prev
        out.append(prev)
    return out


def state_series(bars) -> list[tuple]:
    """``[(ts, state)]``，state ∈ {U, D, R, None}（None = 预热不足）。"""
    closes = [b[4] for b in bars]
    adx = adx14(bars)
    e55 = ema(closes, EMA_N)
    out: list[tuple] = []
    in_trend = False
    for i, a in enumerate(adx):
        if a is None or i < MIN_BARS:
            out.append((bars[i][0], None))
            continue
        if not in_trend and a >= ADX_IN:
            in_trend = True
        elif in_trend and a <= ADX_OUT:
            in_trend = False
        if in_trend:
            slope = e55[i] - e55[i - SLOPE_K] if i >= SLOPE_K else 0.0
            out.append((bars[i][0], "U" if slope > 0 else "D"))
        else:
            out.append((bars[i][0], "R"))
    return out


def state_at(states, decision_ts: int):
    """决策时刻之前**已完成**的最后一根 15m bar 的状态。"""
    last = None
    for ts, s in states:
        if ts + 1500 <= decision_ts:      # 1500 = 15m 的 HHMMSS 偏移
            last = s
        else:
            break
    return last


def decide_ts(day: datetime, decide_hm: int = 1445) -> int:
    """``20261128`` + 1445 → ``20261128144500``。"""
    hm = f"{int(decide_hm):04d}"
    return int(day.strftime("%Y%m%d") + hm + "00")


# ══════════════════════ 15m bar 缓存 ══════════════════════

def load_bars_cache() -> dict:
    p = bars_cache_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _norm_bars(raw) -> list[tuple]:
    out: list[tuple] = []
    for b in raw or []:
        if not isinstance(b, (list, tuple)) or len(b) < 5:
            continue
        try:
            out.append((int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def _merge_bars(old: list[tuple], new: list[tuple], keep: int = 2000) -> list[tuple]:
    """按 ts 去重合并（新值覆盖旧值），只留最后 ``keep`` 根。"""
    m = {int(b[0]): b for b in old}
    for b in new:
        m[int(b[0])] = b
    return [m[k] for k in sorted(m)][-keep:]


def _local_etf_csvs(symbol: str) -> list[Path]:
    """本地 QMT ETF 1m CSV（预热用），新 run 优先。"""
    root = Path(creg._get_storage_dir()).parent / "ashare_data"
    code = str(symbol or "").split(".")[0]
    cands: list[Path] = []
    for pattern in (f"qmt-etf-1m/*/{code}_*_1m.csv", f"qmt-etf-1m/*/{code}_1m.csv"):
        try:
            cands.extend([p for p in root.glob(pattern) if p.is_file()])
        except OSError:
            continue
    cands.sort(key=lambda p: (p.parent.name, p.name), reverse=True)
    return cands


def _seed_from_local(symbol: str, seed_days: int) -> list[tuple]:
    """用本地 QMT CSV 的最后 ``seed_days`` 个交易日做 15m 预热；拿不到返回 []。"""
    if seed_days <= 0:
        return []
    import pandas as pd

    for p in _local_etf_csvs(symbol):
        try:
            df = pd.read_csv(p)
        except (OSError, ValueError):
            continue
        if "stime" not in df.columns or "close" not in df.columns or "low" not in df.columns:
            continue
        df = df.copy()
        df["dt"] = pd.to_datetime(df["stime"].astype(str), format="%Y%m%d%H%M%S",
                                  errors="coerce")
        for c in ("close", "low"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["dt", "close"]).sort_values("dt")
        if df.empty:
            continue
        days = sorted(df["date"].unique() if "date" in df.columns
                      else df["dt"].dt.strftime("%Y%m%d").unique())[-seed_days:]
        df = df[df["dt"].dt.strftime("%Y%m%d").isin(days)]
        df["date"] = df["dt"].dt.strftime("%Y%m%d")
        return to_15m(df)
    return []


def update_bars(symbol: str, cfg: dict | None = None, force_seed: bool = False) -> dict:
    """刷新某标的的 15m bar 缓存：本地预热（一次）+ 实时 1m 增量。"""
    cfg = cfg or forward_config()
    cache = load_bars_cache()
    entry = cache.get(symbol) if isinstance(cache.get(symbol), dict) else {}
    bars = _norm_bars(entry.get("bars"))
    info = {"symbol": symbol, "before": len(bars), "seeded": False, "appended": 0,
            "source": entry.get("seed_source") or "", "error": ""}

    if force_seed or not bars or not entry.get("seed_source"):
        seeded = _seed_from_local(symbol, cfg["seed_days"])
        if seeded:
            bars = _merge_bars(bars, seeded, cfg["bars_keep"])
            info["seeded"] = True
            cands = _local_etf_csvs(symbol)
            info["source"] = f"{cands[0].parent.name}/{cands[0].name}" if cands else "local"

    try:
        df = fetch_klines("m1", 640, symbol)
        df = drop_unclosed(df, 60)
        fresh = to_15m(df)
        if fresh:
            # 与既有（预热 CSV）bar 在同一 ts 上的相对偏差：数据源口径不一致时
            # 会显著变大 ⇒ 写进记录行当可见信号，不静默合并。
            old = {int(b[0]): float(b[4]) for b in bars if b[4]}
            diffs = sorted(abs(float(c) - old[int(ts)]) / old[int(ts)]
                           for ts, _o, _h, _l, c in fresh if int(ts) in old)
            if diffs:
                info["drift_n"] = len(diffs)
                info["drift_med_pct"] = round(diffs[len(diffs) // 2] * 100.0, 4)
            bars = _merge_bars(bars, fresh, cfg["bars_keep"])
            info["appended"] = len(fresh)
    except Exception as e:                                    # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"

    cache[symbol] = {"bars": [list(b) for b in bars],
                     "seed_source": info["source"],
                     "updated": datetime.now(CN).strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        _write_json(bars_cache_path(), cache)
    except OSError as e:
        info["error"] = info["error"] or f"{type(e).__name__}: {e}"
    info["after"] = len(bars)
    return info


# ══════════════════════ 期权快照 ══════════════════════

def _chain_rows(symbol: str, cfg: dict) -> dict:
    """期权链快照（自算 IV / BS delta 口径，与候选工作台同源）。"""
    try:
        snap = oc.chain_snapshot(symbol, Path(creg._get_storage_dir()),
                                 expiries=cfg["expiries"], band_pct=cfg["band_pct"],
                                 force=True)
    except Exception as e:                                    # noqa: BLE001
        return {"rows": [], "spot": 0.0, "error": f"{type(e).__name__}: {e}"}
    if isinstance(snap, dict):
        return {"rows": snap.get("rows") or [], "spot": snap.get("spot") or 0.0,
                "error": str(snap.get("error") or "")}
    return {"rows": [], "spot": 0.0, "error": "bad_snapshot"}


def pick_target_put(rows: list[dict], delta_target: float) -> dict | None:
    """|delta| 最接近目标的 put（先按容差内挑，挑不到就取最接近的）。"""
    puts = [r for r in rows if str(r.get("r") or r.get("right") or "").upper() == "P"]
    puts = [r for r in puts if r.get("delta") not in (None, "")]
    if not puts:
        return None

    def key(r):
        try:
            return abs(abs(float(r["delta"])) - delta_target)
        except (TypeError, ValueError):
            return 9.9

    puts.sort(key=key)
    return puts[0]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════ 记录观察 ══════════════════════

def _recorded_ids(path: Path) -> set:
    return {str(r.get("observation_id")) for r in _load(path) if r.get("observation_id")}


def record_observations(symbols: list[str] | None = None, now: datetime | None = None,
                        cfg: dict | None = None, force: bool = False) -> dict:
    """记录当日状态 + 候选合约（每标的每交易日一次；``force=True`` 可重算预览）。

    ``force=True`` 仍不会覆盖已有行（append-only）——想重记某天，只能删文件（研究
    目录由人决定），本函数永远只追加。
    """
    cfg = cfg or forward_config()
    now = now or tape.now_cn()
    symbols = symbols or cfg["underlyings"]
    today = now.strftime("%Y%m%d")
    dts = decide_ts(now, cfg["decide_hm"])
    done_states = _recorded_ids(state_path())
    done_opts = _recorded_ids(option_path())
    stamp = datetime.now(CN).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    res = {"day": today, "decision_ts": dts, "states": [], "options": [],
           "skipped": [], "errors": []}

    for sym in symbols:
        oid = f"{sym}-{today}"
        if not force and (oid in done_states or oid in done_opts):
            res["skipped"].append(f"{sym}:recorded")
            continue

        upd = update_bars(sym, cfg)
        if upd.get("error"):
            res["errors"].append(f"{sym}:bars {upd['error']}")
        bars = _norm_bars((load_bars_cache().get(sym) or {}).get("bars"))
        sts = state_series(bars)
        st = state_at(sts, dts)
        last_ts = bars[-1][0] if bars else None
        bar_ts_used = None      # 决策时刻实际生效的最后一根「已完成」15m bar
        adx = ema_v = slope = None
        if bars:
            a_all = adx14(bars)
            e_all = ema([b[4] for b in bars], EMA_N)
            idx = len(bars) - 1
            for i, (ts, _s) in enumerate(sts):
                if ts + 1500 <= dts:
                    idx = i
                    bar_ts_used = ts
            adx = a_all[idx]
            ema_v = e_all[idx]
            slope = (e_all[idx] - e_all[idx - SLOPE_K]) if idx >= SLOPE_K else None

        snap = _chain_rows(sym, cfg)
        if snap.get("error"):
            res["errors"].append(f"{sym}:chain {snap['error']}")
        spot = snap.get("spot") or (bars[-1][4] if bars else None)

        srow = {
            "v": SCHEMA_VERSION, "rule_version": RULE_VERSION, "observation_id": oid,
            "recorded_at": stamp, "decide_at": f"{today[:4]}-{today[4:6]}-{today[6:]}"
                                              f" {int(cfg['decide_hm'])//100:02d}:"
                                              f"{int(cfg['decide_hm'])%100:02d}:00",
            "symbol": sym, "spot": spot, "state": st,
            "adx14": None if adx is None else round(float(adx), 4),
            "ema55": None if ema_v is None else round(float(ema_v), 6),
            "slope5": None if slope is None else round(float(slope), 6),
            "bars_15m": len(bars), "last_bar_ts": last_ts,
            "state_bar_ts": bar_ts_used,
            "bar_source": upd.get("source") or "",
            "bars_appended": upd.get("appended") or 0,
            "bars_drift_n": upd.get("drift_n") or 0,
            "bars_drift_med_pct": upd.get("drift_med_pct"),
            "bars_error": upd.get("error") or "",
            "params": {"adx_in": ADX_IN, "adx_out": ADX_OUT, "ema": EMA_N,
                       "slope_k": SLOPE_K, "min_bars": MIN_BARS},
        }
        if st is None:
            srow["note"] = f"预热不足（{len(bars)}/{MIN_BARS} 根 15m bar）"
        _append(state_path(), srow)
        res["states"].append({"symbol": sym, "state": st, "spot": spot,
                              "bars": len(bars)})

        target = pick_target_put(snap.get("rows") or [], cfg["delta_target"])
        orow = {
            "v": SCHEMA_VERSION, "rule_version": RULE_VERSION, "observation_id": oid,
            "recorded_at": stamp, "symbol": sym, "spot": spot, "state": st,
            "delta_target": cfg["delta_target"], "n_quotes": len(snap.get("rows") or []),
            "contract": None, "reason": "",
        }
        if not target:
            orow["reason"] = "no_put_quote"
        else:
            dte = target.get("dte")
            code = str(target.get("code") or target.get("i") or "")
            units = tape.load_contract_units()
            mult = (units.get("units") or {}).get(code.split(".")[0], {}).get("mult")
            orow["contract"] = {
                "code": code, "strike": _num(target.get("strike")),
                "expiry": target.get("expiry"), "dte": dte,
                "bid": _num(target.get("bid")), "ask": _num(target.get("ask")),
                "mid": _num(target.get("mid")), "iv": _num(target.get("iv")),
                "delta": _num(target.get("delta")),
                "mult": mult, "mult_src": units.get("source") or "",
            }
            orow["in_dte_window"] = bool(dte is not None and DTE_MIN <= float(dte) <= DTE_MAX)
            orow["delta_in_band"] = bool(
                orow["contract"]["delta"] is not None
                and abs(abs(orow["contract"]["delta"]) - cfg["delta_target"]) <= DELTA_TOL)
        _append(option_path(), orow)
        res["options"].append({"symbol": sym, "contract": (orow["contract"] or {}).get("code"),
                               "reason": orow["reason"]})
    return res


# ══════════════════════ 前向结算 ══════════════════════

def _daily_bars(symbol: str, n: int = 320) -> list[tuple]:
    """腾讯日线 → ``[(date, o, c, h, l)]``（原始价，不复权）。"""
    import json as _json
    import urllib.request

    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={tx_code(symbol)},day,,,{int(n)},")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"})
    raw = urllib.request.urlopen(req, timeout=15).read()
    payload = _json.loads(raw.decode("utf-8", "replace"))
    blk = ((payload.get("data") or {}).get(tx_code(symbol)) or {})
    rows = blk.get("day") or blk.get("qfqday") or []
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 5:
            continue
        try:
            out.append((str(r[0]).replace("-", "")[:8], float(r[1]), float(r[2]),
                        float(r[3]), float(r[4])))
        except (TypeError, ValueError):
            continue
    return out


def _tape_quotes_for(code: str, days: list[str], clock: str,
                     max_age_min: int = 25) -> dict | None:
    """在 tape 文件里找该合约最接近 ``clock``（HH:MM）的报价。"""
    key = code.split(".")[0]
    best = None
    for d in days:
        for rec in tape.load_tape(d):
            for row in rec.get("rows") or []:
                if str(row.get("i") or "").split(".")[0] != key:
                    continue
                ts = str(row.get("ts") or "")
                if len(ts) < 16:
                    continue
                try:
                    t = datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                hh, mm = int(clock[:2]), int(clock[2:4])
                ref = t.replace(hour=hh, minute=mm, second=0)
                age = abs((t - ref).total_seconds()) / 60.0
                if age > max_age_min:
                    continue
                if best is None or age < best["age_min"]:
                    best = {"bid": _num(row.get("b")), "ask": _num(row.get("a")),
                            "quote_ts": ts, "age_min": round(age, 1), "day": d}
    return best


def settle_outcomes(now: datetime | None = None, cfg: dict | None = None) -> dict:
    """为已到期（≥ horizon 交易日）的观察追加 outcomes（每 id 只写一次）。"""
    cfg = cfg or forward_config()
    now = now or tape.now_cn()
    horizon = cfg["horizon_days"]
    clock = f"{int(cfg['decide_hm']):04d}"
    states = {r.get("observation_id"): r for r in load_states()}
    options = {r.get("observation_id"): r for r in load_options()}
    done = _recorded_ids(outcome_path())
    stamp = datetime.now(CN).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    res = {"settled": [], "waiting": 0, "skipped": 0, "errors": []}
    daily_cache: dict = {}

    for oid, srow in states.items():
        if not oid or oid in done:
            res["skipped"] += 1
            continue
        sym = srow.get("symbol")
        entry_day = str(oid).split("-")[-1]
        if sym not in daily_cache:
            try:
                daily_cache[sym] = _daily_bars(sym)
            except Exception as e:                            # noqa: BLE001
                daily_cache[sym] = []
                res["errors"].append(f"{sym}:daily {type(e).__name__}: {e}")
        days = daily_cache[sym]
        idx = next((i for i, b in enumerate(days) if b[0] == entry_day), None)
        if idx is None:
            res["waiting"] += 1
            continue
        exit_idx = idx + horizon
        if exit_idx >= len(days):
            res["waiting"] += 1
            continue
        exit_day = days[exit_idx][0]
        if exit_day >= now.strftime("%Y%m%d"):
            res["waiting"] += 1
            continue

        entry_spot = _num(srow.get("spot"))
        exit_spot = days[exit_idx][2]
        lows = [b[4] for b in days[idx + 1:exit_idx + 1]]
        o = options.get(oid) or {}
        con = o.get("contract") or {}
        strike = _num(con.get("strike"))
        out = {
            "v": SCHEMA_VERSION, "rule_version": RULE_VERSION,
            "observation_id": oid, "symbol": sym, "state": srow.get("state"),
            "entry_day": entry_day, "exit_day": exit_day, "horizon_days": horizon,
            "settled_at": stamp,
            "etf": {
                "entry_spot": entry_spot, "exit_close": exit_spot,
                "ret": (None if not entry_spot else round(exit_spot / entry_spot - 1.0, 6)),
                "mae": (None if not entry_spot or not lows else
                        round(min(lows) / entry_spot - 1.0, 6)),
                "strike_touch": (None if strike is None or not lows else
                                 bool(min(lows) <= strike)),
            },
            "put": {"code": con.get("code"), "strike": strike,
                    "expiry": con.get("expiry"), "dte": con.get("dte"),
                    "entry_bid": _num(con.get("bid")), "entry_ask": _num(con.get("ask")),
                    "mult": con.get("mult"), "exit_ask": None, "exit_quote_ts": None,
                    "pnl_yuan": None, "pnl_pct_collateral": None,
                    "collateral_yuan": None},
            "missing": [],
        }
        if not con.get("code"):
            out["missing"].append("no_contract")
        elif out["put"]["entry_bid"] is None:
            out["missing"].append("no_entry_bid")
        else:
            q = _tape_quotes_for(str(con["code"]),
                                 [exit_day, days[exit_idx - 1][0] if exit_idx else exit_day],
                                 clock)
            if not q or q.get("ask") is None:
                out["missing"].append("no_exit_quote")
            else:
                out["put"]["exit_ask"] = q["ask"]
                out["put"]["exit_quote_ts"] = q["quote_ts"]
                mult = _num(con.get("mult"))
                if mult is None:
                    out["missing"].append("no_mult")
                else:
                    pnl = (out["put"]["entry_bid"] - q["ask"]) * mult - (FEE_SELL + FEE_BUY)
                    out["put"]["pnl_yuan"] = round(pnl, 2)
                    collateral = (strike or 0.0) * mult
                    out["put"]["collateral_yuan"] = round(collateral, 2)
                    out["put"]["pnl_pct_collateral"] = (
                        round(pnl / collateral, 6) if collateral else None)
        _append(outcome_path(), out)
        res["settled"].append({"observation_id": oid, "pnl_yuan": out["put"]["pnl_yuan"],
                               "missing": out["missing"]})
    return res


# ══════════════════════ runner ══════════════════════

class _ModelBForwardRunner(LiveRunnerBase):
    LOG_PREFIX = "[MODELB-FWD]"
    EVENTS_NAME = EVENTS_NAME
    DEFAULT_INTERVAL_S = DEFAULT_INTERVAL_S
    INTERVAL_RANGE = INTERVAL_RANGE

    def __init__(self) -> None:
        super().__init__()
        self._logged_start = False
        self._last_settle_day = None

    def load_config(self) -> dict:
        return forward_config()

    def config_changed(self, old: dict, new: dict) -> bool:
        return dict(old or {}) != dict(new or {})

    def storage_dir(self) -> Path:
        return tape.tape_dir()

    def do_round(self) -> dict:
        cfg = self.load_config()
        now = tape.now_cn()
        if not self._logged_start:
            self._logged_start = True
            self._log(f"记录器启动 · interval={self._interval_s(cfg)}s · "
                      f"标的={cfg['underlyings']} · 决策时刻={cfg['decide_hm']:04d} · "
                      f"horizon={cfg['horizon_days']}日 · rule={RULE_VERSION}")

        if not tape.in_session(now):
            return {"skipped": "off_session", "ts": now.strftime("%Y-%m-%dT%H:%M:%S")}

        hm = now.hour * 100 + now.minute
        out: dict = {"ts": now.strftime("%Y-%m-%dT%H:%M:%S"), "recorded": 0, "settled": 0}

        if hm >= cfg["decide_hm"]:
            res = record_observations(cfg=cfg, now=now)
            out["recorded"] = len(res["states"])
            out["skipped"] = res["skipped"]
            if res["errors"]:
                out["errors"] = res["errors"]
            if res["states"]:
                self._log("观察 " + res["day"] + " → " + ", ".join(
                    f"{r['symbol']}:{r['state']}/{r['bars']}bars" for r in res["states"]))

        today = now.strftime("%Y%m%d")
        if self._last_settle_day != today:
            st = settle_outcomes(cfg=cfg, now=now)
            self._last_settle_day = today
            out["settled"] = len(st["settled"])
            out["waiting"] = st["waiting"]
            if st["settled"]:
                self._log(f"结算 {len(st['settled'])} 条 → " + ", ".join(
                    f"{r['observation_id']}={r['pnl_yuan']}" for r in st["settled"]))
            if st["errors"]:
                out["errors"] = (out.get("errors") or []) + st["errors"]
        return out


_RUNNER: _ModelBForwardRunner | None = None


def _runner() -> _ModelBForwardRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = _ModelBForwardRunner()
    return _RUNNER


def sync() -> dict:
    """按配置启/停/重启记录器（幂等）。"""
    return _runner().sync()


def stop() -> dict:
    return _runner().stop()


def record_now(force: bool = True) -> dict:
    """立即记录一次（诊断用）；``force`` 允许同一交易日重记预览。"""
    return record_observations(force=force)


def settle_now() -> dict:
    return settle_outcomes()


def files_summary() -> dict:
    """三个研究文件的覆盖情况（页面/诊断/验收用）。"""
    def _info(p: Path, rows: list[dict], key: str = "observation_id") -> dict:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return {"path": str(p), "rows": len(rows), "bytes": size,
                "first": str(rows[0].get(key) or "") if rows else "",
                "last": str(rows[-1].get(key) or "") if rows else ""}

    st = load_states()
    op = load_options()
    oc_ = load_outcomes()
    return {
        "rule_version": RULE_VERSION,
        "states": _info(state_path(), st),
        "options": _info(option_path(), op),
        "outcomes": _info(outcome_path(), oc_),
        "by_state": {s: sum(1 for r in st if r.get("state") == s)
                     for s in ("U", "D", "R", "None")},
        "outcome_pnl": {
            "n": sum(1 for r in oc_ if (r.get("put") or {}).get("pnl_yuan") is not None),
            "sum_yuan": round(sum((r.get("put") or {}).get("pnl_yuan") or 0 for r in oc_), 2),
        },
    }


def state() -> dict:
    st = _runner().status()
    return {
        "running": bool(st.get("thread_alive")),
        "last_run": st.get("last_run"),
        "last_error": st.get("last_error") or "",
        "last_result": st.get("last_result"),
        "total_rounds": st.get("total_rounds") or 0,
        "in_session": tape.in_session(),
        "files": files_summary(),
        "cfg": forward_config(),
    }


__all__ = [
    "RULE_VERSION", "RESEARCH_DIR_NAME", "PARAMS_NAME", "EVENTS_NAME",
    "STATE_FILE", "OPTION_FILE", "OUTCOME_FILE", "BARS_CACHE",
    "DECIDE_HHMMSS", "DELTA_TARGET", "HORIZON_DAYS", "FEE_SELL", "FEE_BUY",
    "DEFAULT_CFG", "forward_config", "save_forward_config", "params_path",
    "research_dir", "state_path", "option_path", "outcome_path", "bars_cache_path",
    "load_states", "load_options", "load_outcomes", "load_bars_cache",
    "to_15m", "adx14", "ema", "wilder", "state_series", "state_at", "decide_ts",
    "update_bars", "pick_target_put", "record_observations", "settle_outcomes",
    "record_now", "settle_now", "files_summary", "state", "sync", "stop",
]
