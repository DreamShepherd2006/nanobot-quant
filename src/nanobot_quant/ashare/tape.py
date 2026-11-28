"""A 股 ETF 期权盘口采集器（只读，2026-11-20 立项）。

**为什么要有它**：QMT 导出的期权 1m 历史只有 OHLC / volume / amount，**没有盘口**，
回测的成交价与滑点只能靠假设。本采集器从启用之日起累积真实买卖五档，用来：

1. 校准回测的滑点假设（按虚值档分层，而不是全局拍一个 1%）；
2. 判断某合约当时是否真的能成交（无买盘 = 卖不出去，无卖盘 = 平不掉）。

**数据源**（两个，各司其职）：

- 合约清单：东方财富 ``fs=m:10`` 全量在售合约（一次 8 页 ≈ 716 个），缓存一天
  —— 与 OKX 线 ``get_tickers(instType="OPTION")`` 等价，**零维护**；
- 盘口：新浪 ``CON_OP_<code>``，单请求批量 40 个合约，含买卖五档、IV、持仓量、
  涨跌停、到期日（字段映射见 :func:`parse_opt_line`）。

**只读边界（硬约束）**：只调公开行情端点；不读持仓、不写台账、不下单、
不参与任何交易决策；失败只记 ``last_error``，绝不抛出到网关。

**落盘**：``{storage_dir}/ashare_tape/tape_YYYYMMDD.jsonl``，一天一文件、每行一次
采样（append-only、跨重启保留、可 grep）::

    {"v": 2, "ts": "2026-11-20T07:21:05Z", "ts_ms": 1795160465000,
     "mult_src": "run_20261127_220003_p1120/contracts_all.csv", "mult_missing": 0,
     "cfg": {"underlyings": [...], "expiries": 3, "band_pct": 12.0, "max_per_side": 8},
     "spot": {"510050.SH": 2.9, ...},
     "rows": [{"i": "10012437.SHO", "u": "510050.SH", "r": "C", "k": 2.8,
               "e": "20261125", "b": 0.0662, "a": 0.0666, "bs": 2, "as": 163,
               "last": 0.0666, "iv": 0.3713, "oi": 24162, "vol": 92178,
               "amt": 44359163.0, "st": "E 00", "ts": "2026-11-20 15:00:00",
               "m": 10000, "ou": 10000.0, "mu": 4091.8, "ms": "contracts_all",
               "bids": [[0.0662, 2], ...5 档], "asks": [[0.0666, 163], ...5 档]}]}

``b``/``a`` 取自五档数组的最优档（新浪的 ``[1]/[2]`` 在个别合约上是旧值，
例如 500ETF 沽 6500 的 ``[2]`` 等于买一，不能用）；无报价记 ``null``。

**v2（2026-11-28）两个字段为什么必须落盘**：

1. ``rows[].ts`` = **合约自身报价时间**（新浪字段 ``[32]``，北京时间），与采样时间
   ``rec.ts`` 是两件事。旧版 ``flatten()`` 只输出采样时刻，于是 300s 采样一次也看不出
   该合约是刚更新还是几分钟前的旧报价；现在按 ``sample_ts`` / ``quote_ts`` /
   ``quote_age_s`` 三个字段分开给出。**解析失败一律记 null，绝不拿采样时间顶替。**
2. ``rows[].m`` = 合约乘数（份/张），来自 ``contracts_all.csv``。510500 同一到期档并存
   10000 与 10180 两种（202612：10000×36 + 10180×28），**不允许按到期月推断、也不允许
   默认 10000**；匹配不到记 ``m=null`` + ``ms="missing"`` 并计入 ``mult_missing``。

旧文件没有 ``v`` 字段 ⇒ 读出来按 ``v=1`` 处理（``mult_src="legacy"``），与 v2 明确区分。
"""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..live_runner_base import LiveRunnerBase
from .. import credential_registry as creg
from . import contracts as ac

CN = timezone(timedelta(hours=8))          # 中国无 DST，固定 +8 避免依赖 tzdata
TAPE_DIR_NAME = "ashare_tape"
PARAMS_NAME = "ashare_params.json"
SCHEMA_VERSION = 2                          # v2 = rows 带合约自身报价时间 + 合约乘数
QUOTE_TS_FMT = "%Y-%m-%d %H:%M:%S"          # 新浪 [32] 的格式（北京时间）

DEFAULT_INTERVAL_S = 60
INTERVAL_RANGE = (10, 1800)
BATCH = 60                                  # 新浪单请求合约数（实测 174 个也能一次返回，留余量）

# 交易时段（北京时间）：上午 9:30–11:30、下午 13:00–15:00（收盘留 2 分钟尾巴）
SESSIONS = (((9, 30), (11, 30)), ((13, 0), (15, 2)))
STALE_TOLERANCE_S = 300                     # 快照时间落后超过这个值视为非交易时段

# 标的池（与回测/分析口径一致）：只有这四个有流动性。588080（易方达科创50）
# 日成交额比 588000 低约 10 倍、且有整天零成交的月份 ⇒ **不入池**（仍可从 QMT 拿历史
# 做对照，但不采集盘口，否则只是无意义的空行）。
POOL_UNDERLYINGS: tuple[tuple[str, str], ...] = (
    ("510050.SH", "上证50ETF"),
    ("510300.SH", "沪深300ETF"),
    ("510500.SH", "中证500ETF"),
    ("588000.SH", "科创50ETF"),
)
POOL_CODES = tuple(c for c, _ in POOL_UNDERLYINGS)

DEFAULT_TAPE: dict = {
    "enabled": False,
    "interval_s": DEFAULT_INTERVAL_S,
    "underlyings": list(POOL_CODES),
    "expiries": 3,          # 最近 N 个未到期档
    "band_pct": 12.0,       # 行权价相对现价的窗口（±%）
    "max_per_side": 8,      # 每档每方向最多收录多少个行权价（按贴近现价排序）
}


# ── 参数 ────────────────────────────────────────────────────

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


def params_path() -> Path:
    return Path(creg._get_storage_dir()) / PARAMS_NAME


def _normalize(cfg: dict | None) -> dict:
    c = dict(DEFAULT_TAPE)
    c.update({k: v for k, v in (cfg or {}).items() if v is not None})
    c["enabled"] = bool(c.get("enabled"))
    c["interval_s"] = _clamp_int(c.get("interval_s"), *INTERVAL_RANGE,
                                 DEFAULT_INTERVAL_S)
    c["expiries"] = _clamp_int(c.get("expiries"), 1, 6, DEFAULT_TAPE["expiries"])
    c["band_pct"] = _clamp_float(c.get("band_pct"), 3.0, 30.0, 12.0)
    c["max_per_side"] = _clamp_int(c.get("max_per_side"), 1, 20, 8)
    undls = [u for u in (c.get("underlyings") or []) if u in POOL_CODES]
    c["underlyings"] = undls or list(POOL_CODES)
    return c


def tape_config() -> dict:
    p = params_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return _normalize(d if isinstance(d, dict) else {})
    except (OSError, json.JSONDecodeError):
        return _normalize({})


def save_tape_config(**kw) -> dict:
    cfg = _normalize({**tape_config(), **kw})
    p = params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return cfg


# ── 落盘 ────────────────────────────────────────────────────

def tape_dir() -> Path:
    return Path(creg._get_storage_dir()) / TAPE_DIR_NAME


def sample_path(day: str | None = None) -> Path:
    day = day or datetime.now(CN).strftime("%Y%m%d")
    return tape_dir() / f"tape_{day}.jsonl"


def _append_sample(rec: dict) -> None:
    """落盘文件名跟随记录自身的时间，不用真实时钟。

    旧实现恒用 ``sample_path()``（= ``datetime.now()``），两个问题：
    ① 跨零点采样会把 ``23:59`` 的记录写进次日文件（人工翻旧数据会错位）；
    ② 单测 patch ``now_cn`` 后落盘仍用真实日期——测试于是变成时间炸弹（2026-11-23 实测已红）。
    """
    day = str(rec.get("ts") or "")[:10].replace("-", "") or None
    p = sample_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_tape(day: str | None = None, limit: int | None = None) -> list[dict]:
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
    day = day or datetime.now(CN).strftime("%Y%m%d")
    p = sample_path(day)
    st = {"day": day, "samples": 0, "rows": 0, "first_ts": None, "last_ts": None,
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


# ── 合约乘数（v2） ───────────────────────────────────────

_UNIT_CACHE: dict = {"key": None, "units": None, "source": ""}


def contract_unit_sources() -> list[Path]:
    """乘数来源候选，按优先级：

    1. ``{storage_dir}/ashare_tape/contracts_all.csv``（手工抽查/临时补齐用）；
    2. ``{data_root}/ashare_data/qmt-option-1m/<run>/contracts_all.csv``，新的 run 优先。

    都拿不到就返回空表——调用方必须标 ``missing``，**不得默认 10000**。
    """
    out: list[Path] = []
    local = tape_dir() / "contracts_all.csv"
    if local.is_file():
        out.append(local)
    root = Path(creg._get_storage_dir()).parent / "ashare_data"
    try:
        runs = [p for p in root.glob("qmt-option-1m/*/contracts_all.csv") if p.is_file()]
    except OSError:
        runs = []
    runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out.extend(runs)
    return out


def load_contract_units() -> dict:
    """合约代码 → ``{mult, opt_unit, margin_unit}``；返回 ``{units, source, error}``。

    key 用不带 ``.SHO`` 后缀的代码（与行情行的 ``i`` 同源）。``mult`` 非正的条目直接跳过，
    宁可让下游看到 ``missing``，也不拿一个不可信的乘数去算名义金额。
    """
    src = contract_unit_sources()
    if not src:
        return {"units": {}, "source": "", "error": "no_contracts_all"}
    p = src[0]
    try:
        st = p.stat()
    except OSError as e:
        return {"units": {}, "source": "", "error": f"{type(e).__name__}: {e}"}
    label = f"{p.parent.name}/contracts_all.csv"
    key = (str(p), st.st_mtime, st.st_size)
    if _UNIT_CACHE.get("key") == key and _UNIT_CACHE.get("units") is not None:
        return {"units": _UNIT_CACHE["units"], "source": _UNIT_CACHE["source"],
                "error": ""}
    units: dict = {}
    try:
        with open(p, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = str(row.get("code") or "").strip().split(".")[0]
                if not code:
                    continue
                try:
                    mult = int(float(row.get("mult") or 0))
                except (TypeError, ValueError):
                    mult = 0
                if mult <= 0:
                    continue
                units[code] = {
                    "mult": mult,
                    "opt_unit": _opt_float(row.get("opt_unit")),
                    "margin_unit": _opt_float(row.get("margin_unit")),
                }
    except (OSError, csv.Error) as e:
        return {"units": {}, "source": "", "error": f"{type(e).__name__}: {e}"}
    _UNIT_CACHE.update({"key": key, "units": units, "source": label})
    return {"units": units, "source": label, "error": ""}


def _opt_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def attach_contract_units(rows: list[dict], units: dict) -> int:
    """给行情行挂上乘数/原始合约单位/保证金快照；返回未匹配条数。

    匹配不到时 ``m/ou/mu`` 全写 ``None`` + ``ms="missing"``——**不写默认 10000**。
    """
    missing = 0
    for r in rows:
        code = str(r.get("i") or "").split(".")[0]
        u = units.get(code) if units else None
        if u:
            r["m"] = u.get("mult")
            r["ou"] = u.get("opt_unit")
            r["mu"] = u.get("margin_unit")
            r["ms"] = "contracts_all"
        else:
            r["m"] = None
            r["ou"] = None
            r["mu"] = None
            r["ms"] = "missing"
            missing += 1
    return missing


def parse_quote_ts(raw) -> datetime | None:
    """新浪 ``[32]`` → 带 CN 时区的 datetime；解析不了返回 ``None``（不做回退）。"""
    try:
        return datetime.strptime(str(raw or "").strip(), QUOTE_TS_FMT).replace(tzinfo=CN)
    except (TypeError, ValueError):
        return None


# ── 展开 ────────────────────────────────────

def flatten(samples: list[dict]) -> list[dict]:
    """采样记录 → 每条报价一行（分析/导出复用）。

    v2 字段：``sample_ts``（采样时刻）/ ``quote_ts``（合约自身报价时刻）/ ``quote_age_s``
    （两者差，秒）/ ``mult`` / ``mult_src`` / ``opt_unit`` / ``margin_unit``。
    ``ts`` 保留 = ``sample_ts``（旧调用方向后兼容）。
    """
    out: list[dict] = []
    for rec in samples or []:
        spots = rec.get("spot") or {}
        sample_ts = rec.get("ts")
        sample_ms = rec.get("ts_ms")
        ver = int(rec.get("v") or 1)
        for r in rec.get("rows") or []:
            bid, ask = r.get("b"), r.get("a")
            mid = spread = None
            if bid and ask:
                mid = round((bid + ask) / 2.0, 8)
                if mid > 0:
                    spread = round((ask - bid) / mid, 6)
            q = parse_quote_ts(r.get("ts"))
            quote_ts = (
                q.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if q else None
            )
            quote_age_s = None
            if q is not None and sample_ms:
                try:
                    quote_age_s = int(int(sample_ms) / 1000 - q.timestamp())
                except (TypeError, ValueError):
                    quote_age_s = None
            out.append({
                "v": ver,
                "ts": sample_ts, "sample_ts": sample_ts, "ts_ms": sample_ms,
                "quote_ts": quote_ts, "quote_ts_raw": (r.get("ts") or None),
                "quote_age_s": quote_age_s,
                "code": r.get("i"), "undl": r.get("u"), "right": r.get("r"),
                "strike": r.get("k"), "expire": r.get("e"),
                "spot": spots.get(r.get("u")),
                "bid": bid, "ask": ask, "bid_sz": r.get("bs"), "ask_sz": r.get("as"),
                "iv": r.get("iv"), "oi": r.get("oi"), "vol": r.get("vol"),
                "mid": mid, "spread_pct": spread,
                "mult": r.get("m"),
                "mult_src": r.get("ms") or ("legacy" if ver < 2 else "missing"),
                "opt_unit": r.get("ou"), "margin_unit": r.get("mu"),
                "bids": r.get("bids"), "asks": r.get("asks"),
            })
    return out


# ── 时段 ────────────────────────────────────────────────────

def now_cn() -> datetime:
    return datetime.now(CN)


def in_session(now: datetime | None = None) -> bool:
    now = now or now_cn()
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return any(a[0] * 60 + a[1] <= m <= b[0] * 60 + b[1] for a, b in SESSIONS)


# ── 网络 ────────────────────────────────────────────────────

def _get(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    })
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    try:
        return raw.decode("gbk")
    except Exception:  # noqa: BLE001
        return raw.decode("utf-8", "replace")


def _sina_sym(undl: str) -> str:
    return "sh" + undl.split(".")[0]


def fetch_spot(underlyings: list[str]) -> dict[str, float]:
    """ETF 现货现价（用于按虚值带选合约）。"""
    if not underlyings:
        return {}
    txt = _get("http://hq.sinajs.cn/list=" + ",".join(_sina_sym(u) for u in underlyings))
    out: dict[str, float] = {}
    for line in txt.strip().split("\n"):
        try:
            key, _, rest = line.partition("=")
            code = key.strip().split("_")[-1]          # sh510050
            fields = rest.strip().strip(";").strip('"').split(",")
            px = float(fields[3])
            if 0.05 < px < 5000:
                out[f"{code[2:]}.SH"] = px
        except Exception:  # noqa: BLE001
            continue
    return out


def _num(fields: list[str], i: int, default: float = 0.0) -> float:
    try:
        v = fields[i].strip()
        return float(v) if v and v not in ("-", "--") else default
    except (IndexError, ValueError):
        return default


def _quote(v: float) -> float | None:
    return v if v and v > 0 else None


def parse_opt_line(line: str) -> dict | None:
    """解析一行新浪期权行情（字段映射见模块 docstring）。"""
    try:
        key, _, rest = line.partition("=")
        code = key.strip().split("CON_OP_")[-1]
        body = rest.strip().strip(";").strip('"')
        if not body or not code:
            return None
        f = body.split(",")
        if len(f) < 47:
            return None
        bids = [[round(_num(f, 22 + 2 * i), 6), int(_num(f, 23 + 2 * i))] for i in range(5)]
        asks = [[round(_num(f, 20 - 2 * i), 6), int(_num(f, 21 - 2 * i))] for i in range(5)]
        return {
            "i": code + ".SHO",
            "ts": f[32].strip(),
            "b": _quote(bids[0][0]),
            "a": _quote(asks[0][0]),
            "bs": bids[0][1],
            "as": asks[0][1],
            "last": _quote(_num(f, 3)),
            "iv": _num(f, 10),
            "oi": int(_num(f, 5)),
            "vol": int(_num(f, 41)),
            "amt": _num(f, 42),
            "st": f[34].strip(),
            "e": (f[46].strip().replace("-", "") or None),
            "bids": bids,
            "asks": asks,
        }
    except Exception:  # noqa: BLE001
        return None


def fetch_quotes(codes: list[str], timeout: float = 12.0) -> tuple[list[dict], int]:
    """批量拉盘口。返回 ``(rows, errors)``——单批失败只计数，不影响其余批次。"""
    rows: list[dict] = []
    errors = 0
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            txt = _get("http://hq.sinajs.cn/list=" +
                       ",".join("CON_OP_" + c.split(".")[0] for c in chunk), timeout)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        for line in txt.strip().split("\n"):
            rec = parse_opt_line(line)
            if rec:
                rows.append(rec)
    return rows, errors


# ── 选池 ────────────────────────────────────────────────────

def pick_contracts(items: list[dict], cfg: dict, spot: dict[str, float],
                   today=None) -> list[dict]:
    """从全量清单里挑采集对象（确定性、可回归）。

    未到期 → 只留配置里的标的 → 最近 ``expiries`` 个到期档 →
    strike 落在现价 ``±band_pct`` → 每档每方向按贴近现价取前 ``max_per_side``。
    现货缺失时该标的只做到期过滤（不按带宽裁），照常采、分析侧容错。
    """
    today = today or now_cn().date()
    band = float(cfg["band_pct"]) / 100.0
    cand = ac.filter_active(items, today, cfg["underlyings"])
    exps = sorted({c["expire"] for c in cand})[: int(cfg["expiries"])]
    per: dict[tuple, list] = {}
    for c in cand:
        if c["expire"] not in exps:
            continue
        px = spot.get(c["undl"])
        dist = 0.0
        if px and px > 0:
            dist = abs(c["strike"] / px - 1.0)
            if dist > band:
                continue
        per.setdefault((c["undl"], c["expire"], c["right"]), []).append((dist, c))
    picked: list[dict] = []
    for _, items_ in sorted(per.items()):
        items_.sort(key=lambda x: (x[0], x[1]["strike"]))
        picked.extend(c for _d, c in items_[: int(cfg["max_per_side"])])
    picked.sort(key=lambda c: c["code"])
    return picked


# ── 采样 ────────────────────────────────────────────────────

def _fresh(rows: list[dict], now: datetime) -> bool:
    """快照时间是否够新（节假日/周末新浪会返回上一交易日快照）。"""
    newest = None
    for r in rows:
        try:
            t = datetime.strptime(r.get("ts") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN)
        except ValueError:
            continue
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return False
    return (now - newest).total_seconds() <= STALE_TOLERANCE_S


def sample_once(cfg: dict | None = None, persist: bool = True,
                force: bool = False) -> dict:
    """采一次。``force=True`` 忽略交易时段与陈旧检查（页面「立即采样」调试用）。"""
    cfg = _normalize(cfg or tape_config())
    now = now_cn()
    res: dict = {"v": SCHEMA_VERSION,
                 "ts": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "ts_ms": int(now.timestamp() * 1000),
                 "cfg": {k: cfg[k] for k in ("underlyings", "expiries",
                                             "band_pct", "max_per_side")},
                 "spot": {}, "rows": [], "skipped": "", "errors": 0}

    if not force and not in_session(now):
        res["skipped"] = "off_session"
        return res

    try:
        spot = fetch_spot(cfg["underlyings"])
    except Exception as e:  # noqa: BLE001
        res["error"] = f"spot: {type(e).__name__}: {e}"
        spot = {}
    res["spot"] = spot
    if not spot:
        res["skipped"] = res.get("skipped") or "no_spot"
        return res

    c = ac.contracts(creg._get_storage_dir())
    res["contracts_source"] = c.get("source")
    if c.get("error"):
        res["contracts_error"] = c["error"]
    picks = pick_contracts(c.get("items") or [], cfg, spot, now.date())
    res["picked"] = len(picks)
    if not picks:
        res["skipped"] = res.get("skipped") or "no_contracts"
        return res

    rows, errors = fetch_quotes([p["code"] for p in picks])
    res["errors"] = errors
    meta = {p["code"]: p for p in picks}
    for r in rows:
        m = meta.get(r["i"].split(".")[0])
        if m:
            r["u"] = m["undl"]
            r["r"] = m["right"]
            r["k"] = m["strike"]
    res["rows"] = [r for r in rows if "u" in r]

    # v2：把合约乘数/原始合约单位/保证金快照落进每条行情行。匹配不到宁可标 missing，
    # 也不默认 10000——510500 同一到期档并存 10000 与 10180（名义差 1.8%）。
    units_res = load_contract_units()
    res["mult_src"] = units_res.get("source") or ""
    if units_res.get("error"):
        res["mult_error"] = units_res["error"]
    res["mult_missing"] = attach_contract_units(res["rows"], units_res.get("units") or {})

    if not force and not _fresh(res["rows"], now):
        res["skipped"] = "stale"
        res["rows"] = []
        return res

    if persist and res["rows"]:
        try:
            _append_sample(res)
        except OSError as e:
            res["error"] = f"write: {type(e).__name__}: {e}"
    return res


# ── Runner ──────────────────────────────────────────────────

class _AshareTapeRunner(LiveRunnerBase):
    """A 股期权盘口采集 runner —— 只读副作用，与策略循环并行、互不干扰。"""

    LOG_PREFIX = "[ASHARE-TAPE]"
    EVENTS_NAME = "ashare_tape_events.jsonl"
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
                      f"标的={cfg['underlyings']} · 到期档={cfg['expiries']} · "
                      f"带宽=±{cfg['band_pct']}% · 每侧={cfg['max_per_side']}")
        rec = sample_once(cfg, persist=True)
        if rec.get("skipped"):
            return {"skipped": rec["skipped"], "ts": rec["ts"]}
        n = len(rec.get("rows") or [])
        self._bump("samples")
        self._bump("rows", n)
        spot_txt = {k: round(v, 4) for k, v in (rec.get("spot") or {}).items()}
        self._log(f"采样 {rec['ts']} → {n} 行 · 合约 {rec.get('picked')} · "
                  f"清单={rec.get('contracts_source')} · spot={spot_txt}")
        return {"rows": n, "picked": rec.get("picked"), "ts": rec["ts"]}


_RUNNER: _AshareTapeRunner | None = None


def _runner() -> _AshareTapeRunner:
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = _AshareTapeRunner()
    return _RUNNER


def sample_now(cfg: dict | None = None, persist: bool = False) -> dict:
    """立即采一次（页面预览/诊断；默认不落盘）。"""
    return sample_once(cfg, persist=persist, force=True)


def sync() -> dict:
    """按配置启/停/重启采集（幂等）。"""
    return _runner().sync()


def stop() -> dict:
    return _runner().stop()


def state() -> dict:
    st = _runner().status()
    return {
        "running": bool(st.get("thread_alive")),
        "last_run": st.get("last_run"),
        "last_error": st.get("last_error") or "",
        "total_samples": int((st.get("totals") or {}).get("samples") or 0),
        "total_rows": int((st.get("totals") or {}).get("rows") or 0),
        "stats": tape_stats(),
        "in_session": in_session(),
    }


__all__ = [
    "TAPE_DIR_NAME", "DEFAULT_TAPE", "DEFAULT_INTERVAL_S", "INTERVAL_RANGE",
    "SCHEMA_VERSION",
    "tape_config", "save_tape_config", "tape_dir", "sample_path", "load_tape",
    "tape_stats", "flatten", "in_session", "now_cn", "parse_opt_line",
    "parse_quote_ts", "contract_unit_sources", "load_contract_units",
    "attach_contract_units",
    "fetch_spot", "fetch_quotes", "pick_contracts", "sample_once", "sample_now",
    "sync", "stop", "state",
]
