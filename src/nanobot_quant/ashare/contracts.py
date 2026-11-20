"""A 股 ETF 期权合约清单（东财全量列表 + 到期日推算 + 本地缓存）。

**为什么不要静态快照**：OKX 线是 ``get_tickers(instType="OPTION")`` 一次拿全市场
在售合约、本地过滤，零维护（新挂牌自动进池、到期自动出池）。A 股这边等价能力来自
东方财富的板块列表：``fs=m:10`` 返回全部在售合约（2026-11-20 实测 total=716，
与券商「上证期权」板块数量一致），分页 100/页，共 8 页。

字段：``f12``=合约代码（如 10012437）、``f14``=名称（``50ETF购11月2800``）。
名称里**没有年份**，只能按月推断：取「未来最近的该月份」，若算出的到期日已过则 +1 年。

到期日规则：到期月**第四个星期三**（遇法定节假日顺延，本项目只用于选池，
错一天不影响采集）。已验证 20261125 / 20261223 / 20270324 三个月均吻合。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 东财同一接口有多个入口，实测 TLS 握手在部分环境/时段会超时，按顺序回退。
# （2026-11-20：https://push2 超时，http://push2 与 82/push2delay 均正常）
LIST_HOSTS = (
    "https://push2.eastmoney.com",
    "https://82.push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
    "http://push2.eastmoney.com",
)
LIST_FS = "m:10"          # 东财板块：期权（沪市 ETF 期权全量）
PAGE_SIZE = 100           # 服务端上限
MAX_PAGES = 12
CACHE_TTL_S = 86400       # 合约清单一天刷新一次足够

# 名称前缀 → 标的代码（只认我们池子里的 5 只，其余忽略）
UNDERLYINGS: dict[str, str] = {
    "50ETF": "510050.SH",
    "300ETF": "510300.SH",
    "500ETF": "510500.SH",
    "科创50": "588000.SH",
    "科创板50": "588080.SH",
}

NAME_RE = re.compile(r"^(?P<u>.+?)(?P<right>[购沽])(?P<month>\d{1,2})月(?P<strike>\d+)$")
_RIGHT = {"购": "C", "沽": "P"}

CACHE_NAME = "contracts_cache.json"
_HOST_OK = ""              # 记住本次进程内可用的东财入口（避免每页都重试超时域名）


# ── 纯函数（可单测，不碰网络） ──────────────────────────────

def expiry_of(year: int, month: int) -> date:
    """到期日 = 该月第四个星期三。"""
    d = date(year, month, 1)
    first_wed = d + timedelta(days=(2 - d.weekday()) % 7)   # Monday=0, Wednesday=2
    return first_wed + timedelta(days=21)


def resolve_expiry(month: int, today: date) -> date | None:
    """名称里的月份 → 具体到期日；取未来最近的该月，已过则顺延一年。"""
    for year in (today.year, today.year + 1):
        e = expiry_of(year, month)
        if e >= today:
            return e
    return None


def parse_name(name: str, today: date) -> dict | None:
    """``50ETF购11月2800`` → ``{undl, right, month, strike, expire}``。"""
    m = NAME_RE.match((name or "").strip())
    if not m:
        return None
    undl = UNDERLYINGS.get(m.group("u"))
    if not undl:
        return None
    try:
        strike = int(m.group("strike")) / 1000.0
        month = int(m.group("month"))
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12) or strike <= 0:
        return None
    exp = resolve_expiry(month, today)
    if exp is None:
        return None
    return {"undl": undl, "right": _RIGHT[m.group("right")], "month": month,
            "strike": strike, "expire": exp.strftime("%Y%m%d")}


def filter_active(items: list[dict], today: date,
                  underlyings: list[str] | None = None) -> list[dict]:
    """只留未到期 + 指定标的的合约。"""
    want = set(underlyings or UNDERLYINGS.values())
    out = []
    for it in items or []:
        if it.get("undl") not in want:
            continue
        if (it.get("expire") or "") < today.strftime("%Y%m%d"):
            continue
        out.append(it)
    return out


# ── 网络 ────────────────────────────────────────────────────

def _fetch_page(pn: int, timeout: float = 8.0, host: str | None = None) -> dict:
    global _HOST_OK
    q = (f"/api/qt/clist/get?pn={pn}&pz={PAGE_SIZE}&po=1&np=1&fltt=2&invt=2"
         f"&fid=f12&fs={LIST_FS}&fields=f12,f13,f14")
    if host:
        hosts = [host]
    elif _HOST_OK:
        hosts = [_HOST_OK] + [h for h in LIST_HOSTS if h != _HOST_OK]
    else:
        hosts = list(LIST_HOSTS)
    last_err: Exception | None = None
    for h in hosts:
        try:
            req = urllib.request.Request(h + q, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://quote.eastmoney.com/",
            })
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            _HOST_OK = h                      # 记住可用入口，后续页直接用它
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001 — 换下一个入口
            last_err = e
            if _HOST_OK == h:
                _HOST_OK = ""
            continue
    raise last_err or RuntimeError("no host available")


def fetch_contracts(today: date | None = None, timeout: float = 8.0) -> list[dict]:
    """从东财分页拉全部在售合约并解析（失败抛异常，由调用方兜缓存）。"""
    today = today or _cn_today()
    items: list[dict] = []
    seen: set[str] = set()
    total = None
    for pn in range(1, MAX_PAGES + 1):
        data = (_fetch_page(pn, timeout).get("data") or {})
        diff = data.get("diff") or []
        total = data.get("total", total)
        if not diff:
            break
        for row in diff:
            code = str(row.get("f12") or "").strip()
            name = str(row.get("f14") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            parsed = parse_name(name, today)
            if not parsed:
                continue
            items.append({"code": code, "name": name, **parsed})
        if total is not None and len(seen) >= int(total):
            break
        if len(diff) < PAGE_SIZE:
            break
    return items


# ── 缓存 ────────────────────────────────────────────────────

def _cn_today() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date()


def cache_path(storage_dir: str | os.PathLike) -> str:
    return os.path.join(str(storage_dir), "ashare_tape", CACHE_NAME)


def load_cached(storage_dir: str | os.PathLike) -> dict:
    p = cache_path(storage_dir)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cached(storage_dir: str | os.PathLike, items: list[dict]) -> None:
    p = cache_path(storage_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": int(time.time()), "items": items}, f, ensure_ascii=False)
    os.replace(tmp, p)


def contracts(storage_dir: str | os.PathLike, force: bool = False,
              today: date | None = None) -> dict:
    """取合约清单（优先缓存）。

    返回 ``{"items": [...], "fetched_at": int|None, "source": "cache"|"live",
    "error": ""}`` —— 网络失败时回退上次缓存并把异常写进 ``error``（不抛），
    页面据此提示「清单未更新」。
    """
    today = today or _cn_today()
    cached = load_cached(storage_dir)
    age = time.time() - int(cached.get("fetched_at") or 0)
    if not force and cached.get("items") and age < CACHE_TTL_S:
        return {"items": cached["items"], "fetched_at": cached.get("fetched_at"),
                "source": "cache", "error": ""}
    try:
        items = fetch_contracts(today)
        if not items:
            raise RuntimeError("返回 0 个合约")
        save_cached(storage_dir, items)
        return {"items": items, "fetched_at": int(time.time()),
                "source": "live", "error": ""}
    except Exception as e:  # noqa: BLE001 — 清单取不到就用旧缓存，绝不阻断采集
        return {"items": cached.get("items") or [],
                "fetched_at": cached.get("fetched_at"),
                "source": "cache", "error": f"{type(e).__name__}: {e}"}


__all__ = [
    "UNDERLYINGS", "expiry_of", "resolve_expiry", "parse_name",
    "filter_active", "fetch_contracts", "contracts", "cache_path",
    "load_cached", "save_cached",
]
