"""OKX 期权历史归档数据层（每日 zip，含已到期合约）—— 期权回测的取数底座。

为什么必须走归档（2026-09-17 逐端点实测）
-----------------------------------------
已到期合约的**一切实时端点**都返回 ``51001 Instrument ID doesn't exist``：
``market/candles``、``market/history-candles``、``market/mark-price-candles``、
``market/history-mark-price-candles``、``market/ticker``、``market/history-trades``
—— 全部逐个实测确认（在售合约正常，同一路径换已到期 instId 即 51001）。而回测的
合约**全是已到期的历史合约**，所以实时端点对回测零价值，只服务实盘。

官方归档 ``/api/v5/public/market-data-history`` 是唯一覆盖已到期合约的来源：

===========  ===================================  ===========
``module``   文件                                  大小/天
===========  ===================================  ===========
1            ``alloption-trades-DATE.zip``         0.22 MB
2            ``alloption-candlesticks-DATE.zip``   6 MB
===========  ===================================  ===========

``module=1`` 是**逐笔成交**（列见 :data:`TRADE_COLUMNS`），只含有成交的记录 ——
比 ``module=2`` 的 1m K 线小 27 倍，且没有 ``vol=0`` 的「最后成交价延续」行
（那些行会捏造出假曲面：实测 98-P 显示得比 101-P 还贵）。**本模块以 module=1
为主源**，module=2 只在需要 bar 对齐时作为补充。

口径与坑（全部实测，勿凭直觉）
------------------------------
* **必须带 User-Agent** —— 默认 ``Python-urllib/3.12`` 直接吃 HTTP 403；
  换成任意值（如 ``curl/8.5.0``）即 200。见 :data:`_UA`。
* **「日」按北京时间切** —— ``dateTs`` 是该北京日 00:00 对应的 **UTC 毫秒**
  （即前一日 16:00Z）。例：``dateTs=1789401600000`` ↔ ``2026-09-14T16:00Z``
  ↔ 文件名 ``alloption-trades-2026-09-15.zip``。见 :func:`cn_day_start_ms`。
* **归档滞后 ≥ 1 天** —— 实测 09-17 15:36（北京）时最新可得为 **09-15**，
  09-16 尚未发布。回测区间末端务必留 2 天空档，否则尾部静默缺数据。
* **``begin``/``end`` 边界语义不直观** —— 实测 ``[09-15, 09-17)`` 只回 09-15，
  而 ``[09-10, 09-18)`` 回满 09-10~09-15。本模块统一**两端各放宽 8 小时**
  （对齐北京日边界）后按日去重，宁可多拉不漏拉。
* **单次窗口 ≤ 8 天** —— 超限报 HTTP 400，超出部分由 :func:`list_archives`
  自动分段请求。
* ``dateAggrType`` 只认 ``daily``（``hourly`` 报 51000，``monthly`` 返回空）。

与其他模块的分工
----------------
* :mod:`nanobot_quant.okx_options_data` —— **实时**链/IV/盘口（只在售合约）
* :mod:`nanobot_quant.bs_pricing` —— BS 定价 / IV 反解 / delta
* 本模块 —— **历史**成交（含已到期合约），供回测反解 IV 后重定价

合约元数据反解复用 :func:`nanobot_quant.okx_options_data.parse_inst_id`，
不另写一份 —— 已到期合约的 strike/到期只能从 instId 反解（官方 instruments
只列在售合约）。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, Union

# 注意：此处**不**在模块级 import okx_options_data —— 它会连带拉入 data_sources 全链
# （eastmoney / gate_cex / okx_cex / onchainos / yfinance），而本模块只做归档取数。
# 元数据反解改走下方延迟包装，使本模块「只依赖标准库」（测试容器裸跑、CLI 直调都不炸）。

ARCHIVE_API = "https://www.okx.com/api/v5/public/market-data-history"

# **必须**带 UA：默认 urllib UA 会被 WAF 判 403（2026-09-17 实测）
_UA = "curl/8.5.0"

MAX_WINDOW_DAYS = 8          # 官方单次请求上限（超限 HTTP 400）
DAY_MS = 86_400_000
CN_OFFSET_MS = 8 * 3_600_000  # 归档按 UTC+8 切日

MODULE_TRADES = 1
MODULE_CANDLES = 2

TRADE_COLUMNS = ("instrument_name", "trade_id", "side", "price",
                 "size", "created_time", "source")
CANDLE_COLUMNS = ("instrument_name", "open", "high", "low", "close",
                  "vol", "vol_ccy", "vol_quote", "open_time", "confirm")

_FILENAME_RE = re.compile(r"alloption-(?:trades|candlesticks)-(\d{4}-\d{2}-\d{2})\.zip$")


class OptionsHistoryError(RuntimeError):
    """归档取数失败（网络 / 参数 / 格式）。

    取数**永不静默降级**：拿不到就报错，让调用方看见缺了哪天、为什么。
    """


# ────────────────────────── 北京日 ↔ UTC 毫秒 ──────────────────────────

def cn_day_start_ms(day: Union[str, dt.date]) -> int:
    """北京日 00:00 对应的 UTC 毫秒。

    归档按北京时间切日，``dateTs`` 存的就是这个值（= 前一日 16:00Z）。

    >>> cn_day_start_ms("2026-09-15")
    1789401600000
    """
    if isinstance(day, dt.date) and not isinstance(day, dt.datetime):
        d = day
    else:
        d = dt.datetime.strptime(str(day), "%Y-%m-%d").date()
    naive = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
    return int(naive.timestamp() * 1000) - CN_OFFSET_MS


def cn_day_of(ts_ms: int) -> str:
    """UTC 毫秒 → 北京日字符串（``YYYY-MM-DD``），:func:`cn_day_start_ms` 的逆。"""
    d = dt.datetime.fromtimestamp((int(ts_ms) + CN_OFFSET_MS) / 1000, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%d")


# ────────────────────────────── HTTP ──────────────────────────────

def _http_get(url: str, *, timeout: float, what: str) -> bytes:
    """带 UA 的 GET。失败一律抛 :class:`OptionsHistoryError`。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:                      # noqa: PERF203
        detail = ""
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except Exception:                                       # pragma: no cover
            pass
        raise OptionsHistoryError(
            f"{what}: HTTP {exc.code} {detail}") from exc
    except Exception as exc:
        raise OptionsHistoryError(f"{what}: {type(exc).__name__}: {exc}") from exc


def _http_json(url: str, *, timeout: float, what: str) -> dict:
    raw = _http_get(url, timeout=timeout, what=what)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OptionsHistoryError(
            f"{what}: 响应不是 JSON（{len(raw)} 字节）") from exc
    code = str(payload.get("code", ""))
    if code not in ("0", ""):
        raise OptionsHistoryError(
            f"{what}: OKX code={code} msg={payload.get('msg')}")
    return payload


# ──────────────────────────── 归档发现 ────────────────────────────

def _day_from_filename(filename: str) -> Optional[str]:
    m = _FILENAME_RE.search(filename or "")
    return m.group(1) if m else None


def list_archives(begin_ms: int, end_ms: int, *,
                  module: int = MODULE_TRADES,
                  timeout: float = 60.0) -> list[dict]:
    """列出 ``[begin_ms, end_ms)`` 覆盖到的归档文件（按北京日升序去重）。

    窗口两端各放宽 8 小时对齐北京日边界（见模块 docstring 的实测说明），
    并按 :data:`MAX_WINDOW_DAYS` 自动分段请求。

    返回项：``{"day", "filename", "url", "size_mb", "date_ts_ms"}``。
    某段请求失败即抛错 —— 不做「跳过这段继续」的静默降级。
    """
    begin_ms, end_ms = int(begin_ms), int(end_ms)
    if end_ms <= begin_ms:
        raise OptionsHistoryError(f"时间窗口非法: begin={begin_ms} end={end_ms}")

    lo = begin_ms - CN_OFFSET_MS
    hi = end_ms + CN_OFFSET_MS
    span = MAX_WINDOW_DAYS * DAY_MS

    found: dict[str, dict] = {}
    cur = lo
    while cur < hi:
        chunk_end = min(cur + span, hi)
        url = (f"{ARCHIVE_API}?instType=OPTION&module={int(module)}"
               f"&dateAggrType=daily&begin={cur}&end={chunk_end}")
        payload = _http_json(url, timeout=timeout,
                             what=f"归档列表[{cn_day_of(cur)}~{cn_day_of(chunk_end)}]")
        for blk in payload.get("data") or []:
            for det in blk.get("details") or []:
                for g in det.get("groupDetails") or []:
                    fname = (g.get("filename") or "").strip()
                    day = _day_from_filename(fname)
                    if not day or day in found:
                        continue
                    found[day] = {
                        "day": day,
                        "filename": fname,
                        "url": g.get("url") or "",
                        "size_mb": float(g.get("sizeMB") or 0.0),
                        "date_ts_ms": int(g.get("dateTs") or 0),
                    }
        cur = chunk_end
    return [found[k] for k in sorted(found)]


# ─────────────────────────── 下载与缓存 ───────────────────────────

def default_cache_dir() -> Path:
    """默认缓存目录：``{data_root}/legion/backtests/opt_data/``。

    与回测结果目录同级（``backtests_dir()``），复用既有路径解析，
    Factory Rebuild 不丢 —— 归档是只读历史数据，重复下载纯属浪费。
    """
    from nanobot_quant.onchainos_cli import backtests_dir
    return Path(backtests_dir()) / "opt_data"


def fetch_archive(entry: dict, *, dest_dir: Optional[Union[str, Path]] = None,
                  force: bool = False, timeout: float = 180.0) -> Path:
    """下载一个归档到缓存目录并返回本地路径；已存在则直接命中缓存（零网络）。

    落盘走 ``.part`` 临时文件 + 原子替换，并在替换前用 ``zipfile`` 打开验证 ——
    半截文件永远不会被当成有效缓存（否则后续所有回测都会静默读到坏数据）。
    """
    root = Path(dest_dir) if dest_dir else default_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / entry["filename"]
    if dest.exists() and not force:
        return dest

    url = entry.get("url") or ""
    if not url:
        raise OptionsHistoryError(f"{entry.get('filename')}: 归档条目缺少 url")
    blob = _http_get(url, timeout=timeout, what=f"下载 {entry['filename']}")

    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(blob)
    try:
        with zipfile.ZipFile(tmp) as zf:
            bad = zf.testzip()
            if bad:
                raise OptionsHistoryError(f"{entry['filename']}: zip 内容损坏（{bad}）")
    except zipfile.BadZipFile as exc:
        tmp.unlink(missing_ok=True)
        raise OptionsHistoryError(
            f"{entry['filename']}: 下载内容不是有效 zip（{len(blob)} 字节）") from exc
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def fetch_range(begin_ms: int, end_ms: int, *,
                module: int = MODULE_TRADES,
                dest_dir: Optional[Union[str, Path]] = None,
                force: bool = False,
                list_timeout: float = 60.0,
                timeout: float = 180.0,
                progress: Optional[Callable[[str], None]] = None) -> list[Path]:
    """把区间内所有归档拉到本地缓存，返回本地路径列表（按北京日升序）。

    ``progress`` 用于把进度写到调用方可观测的地方（日志/事件文件）——
    取数过程必须留痕，不能安静地跑几分钟。
    """
    entries = list_archives(begin_ms, end_ms, module=module, timeout=list_timeout)
    if progress:
        progress(f"归档共 {len(entries)} 天: "
                 f"{entries[0]['day'] if entries else '—'} ~ "
                 f"{entries[-1]['day'] if entries else '—'}")
    out: list[Path] = []
    for i, e in enumerate(entries, 1):
        p = fetch_archive(e, dest_dir=dest_dir, force=force, timeout=timeout)
        out.append(p)
        if progress:
            progress(f"[{i}/{len(entries)}] {e['day']} → {p.name} ({e['size_mb']}MB)")
    return out


# ────────────────────────────── 解析 ──────────────────────────────

def iter_trades(paths: Union[str, Path, Sequence[Union[str, Path]]], *,
                family: Optional[str] = None,
                limit: Optional[int] = None) -> Iterator[dict]:
    """流式产出归档里的逐笔成交行（``dict``，键见 :data:`TRADE_COLUMNS`）。

    只吐 ``vol``/``size`` > 0 的行 —— ``module=1`` 归档本身就只含成交，
    这里再兜一道是为了防上游格式变化时把「延续行」混进来。

    ``family`` 过滤按 instId 反解（``SOL-USD_UM-260918-94-P`` → ``SOL-USD_UM``），
    与实时侧 :func:`nanobot_quant.okx_options_data.parse_inst_id` 同一套规则。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    fam = family.upper() if family else None
    emitted = 0
    for p in paths:
        with zipfile.ZipFile(p) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                with zf.open(name) as raw:
                    reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))
                    for row in reader:
                        inst = (row.get("instrument_name") or "").strip().upper()
                        if not inst:
                            continue
                        try:
                            if float(row.get("size") or 0) <= 0:
                                continue
                        except (TypeError, ValueError):
                            continue
                        if fam and _family_of(inst) != fam:
                            continue
                        row["instrument_name"] = inst
                        row["_source_file"] = Path(p).name
                        yield row
                        emitted += 1
                        if limit is not None and emitted >= limit:
                            return


def _family_of(inst_id: str) -> str:
    """instId → 家族名。尾部固定 3 段（exp/strike/type），从右侧切。

    不引 ``parse_inst_id`` 只为一件事：它会对非 FAMILIES 家族抛错，而解析时
    我们只想**过滤**，不想因为归档里混入别的家族就整体失败。
    """
    parts = (inst_id or "").split("-")
    return "-".join(parts[:-3]) if len(parts) >= 5 else ""


def load_trades(paths: Union[str, Path, Sequence[Union[str, Path]]], *,
                family: Optional[str] = None,
                limit: Optional[int] = None) -> list[dict]:
    """:func:`iter_trades` 的物化版本。"""
    return list(iter_trades(paths, family=family, limit=limit))


def group_by_contract(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """按 instId 分组，组内按 ``created_time`` 升序（反解 IV 要按时间推进）。"""
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.get("instrument_name") or "", []).append(r)
    for k in out:
        out[k].sort(key=lambda r: _as_int(r.get("created_time")))
    return out


def parse_inst_id(inst_id: str) -> dict:
    """反解 instId → 合约元数据（family / expTime / stk / optType）。

    复用 :mod:`nanobot_quant.okx_options_data` 的实现，**不另写一份** —— 已到期
    合约不在官方 in-sale 列表里，strike/到期只能从 instId 反解，两处实现必然漂移。

    延迟导入的理由见文件顶部：okx_options_data 会拉入整条 data_sources 依赖链。
    """
    from nanobot_quant.okx_options_data import parse_inst_id as _impl
    return _impl(inst_id)


def _as_int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ARCHIVE_API", "MAX_WINDOW_DAYS", "MODULE_TRADES", "MODULE_CANDLES",
    "TRADE_COLUMNS", "CANDLE_COLUMNS", "OptionsHistoryError",
    "cn_day_start_ms", "cn_day_of", "list_archives", "default_cache_dir",
    "fetch_archive", "fetch_range", "iter_trades", "load_trades",
    "group_by_contract", "parse_inst_id",
]
