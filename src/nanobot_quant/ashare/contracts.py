"""A 股 ETF 期权合约清单（新浪为主源、东财为备源；本地缓存 + 失败退避）。

**为什么不要静态快照**：OKX 线是 ``get_tickers(instType="OPTION")`` 一次拿全市场
在售合约、本地过滤，零维护（新挂牌自动进池、到期自动出池）。A 股这边靠两个公开源：

1. **新浪（主源，2026-11-23 新增）**：``OP_UP_<标的><YYMM>`` / ``OP_DOWN_…`` 返回
   该标的该到期月的认购/认沽**全部合约代码**；再按代码批量查 ``CON_OP_<code>``
   取真实规格。优点：到期日、行权价、方向全部来自交易所行情（比「第四个星期三」
   推算更准），不走东财；共约 9 个请求（列表 2 批 + 规格 6–7 批）。
2. **东财（备源）**：``fs=m:10`` 板块列表（2026-11-20 实测 total=716），分页 100/页。
   名称里没有年份，到期日按「该月第四个星期三」推算（已验证 202611/202612/202703 吻合）。

**2026-11-23 实测**：东财 ``clist`` 接口对云 IP 整体 502（含非期权的 ``fs=m:1``，
换域名/参数均无效），但新浪 ``OP_UP_``/``CON_OP_`` 完全正常——这是把新浪升为主源的直接动因。

**失败退避**：缓存过期后拉取失败只重试一次，之后 ``FAIL_BACKOFF_S`` 内不再打外网
（否则每轮采样——约 78s——都会重试 4 个 host × 多页）。页面手动「刷新合约清单」
不受退避限制（``force=True``），但失败文案会提示还有多少秒。
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

# 失败退避：缓存过期后拉取失败，这段时间内不再打外网（force=True 不受限）
FAIL_BACKOFF_S = 600
_BACKOFF: dict = {"until": 0.0, "msg": ""}
LAST_SOURCE = ""           # 最近一次成功的源名（sina / eastmoney），供返回体标注

# ── 新浪合约清单（主源） ──────────────────────────────────────
SINA_URL = "http://hq.sinajs.cn/list="
SINA_HEADERS = {"User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn"}
# OP_UP_/OP_DOWN_ 里用的标的短码
SINA_UNDL_SHORT: dict[str, str] = {
    "510050.SH": "510050",
    "510300.SH": "510300",
    "510500.SH": "510500",
    "588000.SH": "588000",
    "588080.SH": "588080",
}
_SINA_SHORT_TO_UNDL = {v: k for k, v in SINA_UNDL_SHORT.items()}
SINA_MONTHS_AHEAD = 8      # 枚举当月起的 8 个月（ETF 期权最长 3 个季月，留足余量）
SINA_LIST_BATCH = 40       # 一次请求多少个 OP_UP_/OP_DOWN_ 名字
SINA_SPEC_BATCH = 60       # 一次请求多少个 CON_OP_ 代码

# CON_OP_ 字段索引（2026-11-23 实测，51 字段；同一行里规格与盘口混在一起）
_F_STRIKE = 7
_F_UNDL = 36
_F_RIGHT = 45
_F_EXPIRE = 46


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


def month_seq(today: date, n: int = SINA_MONTHS_AHEAD) -> list[str]:
    """当月起的 ``YYMM`` 序列（新浪 ``OP_UP_<标的><YYMM>`` 用）。"""
    out: list[str] = []
    y, m = today.year, today.month
    for _ in range(max(1, int(n))):
        out.append(f"{y % 100:02d}{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def parse_sina_codes(text: str) -> list[str]:
    """``OP_UP_/OP_DOWN_`` 响应 → 合约代码列表（去 ``CON_OP_`` 前缀）。"""
    codes: list[str] = []
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        body = line.split("=", 1)[1].strip().rstrip(";").strip('"')
        for tok in body.split(","):
            tok = tok.strip()
            if tok.startswith("CON_OP_"):
                codes.append(tok[len("CON_OP_"):])
    return codes


def parse_sina_spec(line: str) -> dict | None:
    """``CON_OP_<code>`` 行 → ``{code, name, undl, right, strike, expire}``。

    字段索引实测（2026-11-23，51 字段）：``[7]`` 行权价、``[36]`` 标的短码、
    ``[37]`` 名称、``[45]`` C/P、``[46]`` 到期日（``2026-12-23``）。
    比东财名称解析可靠：到期日来自交易所行情，不靠「第四个星期三」推算。
    """
    key, _, rest = (line or "").partition("=")
    if not rest or "CON_OP_" not in key:
        return None
    code = key.split("CON_OP_", 1)[1].strip()
    body = rest.strip().rstrip(";").strip('"')
    f = body.split(",")
    if not code or len(f) <= _F_EXPIRE:
        return None
    undl = _SINA_SHORT_TO_UNDL.get(f[_F_UNDL].strip())
    right = f[_F_RIGHT].strip().upper()
    expire = f[_F_EXPIRE].strip().replace("-", "")
    try:
        strike = float(f[_F_STRIKE])
    except (TypeError, ValueError):
        return None
    if not undl or right not in ("C", "P") or strike <= 0 or len(expire) != 8:
        return None
    return {"code": code, "name": f[37].strip(), "undl": undl,
            "right": right, "strike": strike, "expire": expire}


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


def _sina_get(names: list[str], timeout: float = 8.0) -> str:
    """一次取多个新浪 name（GBK 编码）。"""
    url = SINA_URL + ",".join(names)
    req = urllib.request.Request(url, headers=SINA_HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "replace")


def fetch_contracts_sina(today: date | None = None, timeout: float = 8.0) -> list[dict]:
    """新浪：``OP_UP_/OP_DOWN_`` 枚举代码 → 批量 ``CON_OP_`` 取规格。

    只返回未到期合约；任一环节失败直接抛异常（由 ``fetch_contracts`` 换源）。
    """
    today = today or _cn_today()
    names: list[str] = []
    for ym in month_seq(today):
        for short in SINA_UNDL_SHORT.values():
            for side in ("OP_UP", "OP_DOWN"):
                names.append(f"{side}_{short}{ym}")

    codes: list[str] = []
    seen: set[str] = set()
    for i in range(0, len(names), SINA_LIST_BATCH):
        for c in parse_sina_codes(_sina_get(names[i:i + SINA_LIST_BATCH], timeout)):
            if c not in seen:
                seen.add(c)
                codes.append(c)
    if not codes:
        raise RuntimeError("OP_UP_/OP_DOWN_ 返回 0 个合约代码")

    floor = today.strftime("%Y%m%d")
    items: list[dict] = []
    seen.clear()
    for i in range(0, len(codes), SINA_SPEC_BATCH):
        chunk = codes[i:i + SINA_SPEC_BATCH]
        txt = _sina_get([f"CON_OP_{c}" for c in chunk], timeout)
        for line in txt.splitlines():
            spec = parse_sina_spec(line)
            if spec and spec["code"] not in seen and spec["expire"] >= floor:
                seen.add(spec["code"])
                items.append(spec)
    return items


def fetch_contracts_eastmoney(today: date | None = None, timeout: float = 8.0) -> list[dict]:
    """从东财分页拉全部在售合约并解析（备源；失败抛异常，由调用方兜缓存）。"""
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


def fetch_contracts(today: date | None = None, timeout: float = 8.0) -> list[dict]:
    """多源拉取：新浪（主）→ 东财（备），全部失败才抛异常。"""
    global LAST_SOURCE
    today = today or _cn_today()
    errors: list[str] = []
    for name, fn in (("sina", fetch_contracts_sina),
                     ("eastmoney", fetch_contracts_eastmoney)):
        try:
            items = fn(today, timeout)
            if items:
                LAST_SOURCE = name
                return items
            errors.append(f"{name}: 返回 0 个合约")
        except Exception as e:  # noqa: BLE001 — 换下一个源
            errors.append(f"{name}: {type(e).__name__}: {e}")
    raise RuntimeError(" | ".join(errors) or "no source available")


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
    """取合约清单（优先缓存；拉取失败后退避 ``FAIL_BACKOFF_S``）。

    返回 ``{"items", "fetched_at", "source", "error", "age_s", "retry_in_s"}``：

    - ``source``：``cache`` / ``live:sina`` / ``live:eastmoney``
    - ``age_s``：缓存年龄（秒），没有缓存为 ``None``
    - ``retry_in_s``：退避中还有多少秒才重试（不在退避中为 0）

    网络失败时回退上次缓存并把异常写进 ``error``（不抛），页面据此提示。
    ``force=True``（页面手动刷新）不受退避限制。
    """
    today = today or _cn_today()
    cached = load_cached(storage_dir)
    fetched_at = cached.get("fetched_at")
    age = time.time() - int(fetched_at or 0)
    age_s = int(age) if fetched_at else None

    def _hit(source: str, error: str = "", retry_in_s: int = 0) -> dict:
        return {"items": cached.get("items") or [], "fetched_at": fetched_at,
                "source": source, "error": error,
                "age_s": age_s, "retry_in_s": retry_in_s}

    if not force and cached.get("items") and age < CACHE_TTL_S:
        return {**_hit("cache"), "items": cached["items"]}

    now = time.time()
    if not force and now < _BACKOFF["until"]:
        left = int(_BACKOFF["until"] - now)
        return _hit("cache", f"{_BACKOFF['msg']}（退避中，{left}s 后重试）", left)

    try:
        items = fetch_contracts(today)
        if not items:
            raise RuntimeError("返回 0 个合约")
        save_cached(storage_dir, items)
        _BACKOFF["until"], _BACKOFF["msg"] = 0.0, ""
        return {"items": items, "fetched_at": int(time.time()),
                "source": f"live:{LAST_SOURCE or 'unknown'}", "error": "",
                "age_s": 0, "retry_in_s": 0}
    except Exception as e:  # noqa: BLE001 — 清单取不到就用旧缓存，绝不阻断采集
        _BACKOFF["msg"] = f"{type(e).__name__}: {e}"
        _BACKOFF["until"] = time.time() + FAIL_BACKOFF_S
        return _hit("cache", _BACKOFF["msg"], FAIL_BACKOFF_S)


__all__ = [
    "UNDERLYINGS", "expiry_of", "resolve_expiry", "parse_name",
    "month_seq", "parse_sina_codes", "parse_sina_spec",
    "fetch_contracts", "fetch_contracts_sina", "fetch_contracts_eastmoney",
    "contracts", "cache_path", "load_cached", "save_cached",
    "FAIL_BACKOFF_S", "LAST_SOURCE",
]
