"""510050 TD 计数看板（只读，2026-11-25 立项）。

**为什么要有它**：A 股 ETF 期权线的入场信号来自**ETF 现货**的 TD 衰竭点
（低9 卖 put / 高9 平仓），但 QMT 策略跑在券商沙箱里、云端看不到实时计数，
人只能自己数 K 线。本模块把「当前数到几、还差几根」直接搬到 WebUI。

**只算现货、不算期权**：期权价受 IV 与到期时间污染、每月换合约序列不连续，
TD 计数必须在 ETF 现货序列上算（与 QMT 策略同一口径）。

**按交易日切分**：A 股日内 TD 每天从 0 重数（隔夜不参与计数），与云端
td-table 的 ``_session_split_enabled`` 判据一致；演示层与执行层必须同源，
故这里直接调策略同一个引擎 ``FutuDeMarkEngine``，不另写一套计数。

**数据源**：腾讯 ``ifzq.gtimg.cn``（1m/m5/m15 各 640 根，免 key）。新浪 1m 返回
null（只有 5m 起），故 1m 只能走腾讯。只读：不读持仓、不写台账、不下单。
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

CN = timezone(timedelta(hours=8))

TENCENT_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},{bar},,{n}"
DEFAULT_SYMBOL = "sh510050"
DEFAULT_CMP = 4
SETUP_N = 9
CACHE_TTL_S = 10.0

# 周期 -> (腾讯 bar 名, 单根秒数)
PERIODS: dict[str, tuple[str, int]] = {
    "1m": ("m1", 60),
    "5m": ("m5", 300),
    "15m": ("m15", 900),
}

# 腾讯 m1 数组顺序：时间, 开, 收, 高, 低, 量, {}, 额
_COLS = ["dt_raw", "open", "close", "high", "low", "volume"]

_cache: dict[str, tuple[float, dict]] = {}


# ── 取数 ────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com",
    })
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(raw.decode("utf-8", "replace"))


def fetch_klines(bar: str = "m1", n: int = 640, symbol: str = DEFAULT_SYMBOL) -> pd.DataFrame:
    """拉腾讯分钟线 → DataFrame(dt, open, high, low, close, volume)，按时间升序。"""
    payload = _get_json(TENCENT_URL.format(sym=symbol, bar=bar, n=n))
    blk = ((payload.get("data") or {}).get(symbol) or {})
    rows = blk.get(bar) or []
    if not rows:
        return pd.DataFrame(columns=["dt", "open", "high", "low", "close", "volume"])
    recs = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            continue
        recs.append(r[:6])
    df = pd.DataFrame(recs, columns=_COLS)
    df["dt"] = pd.to_datetime(df["dt_raw"].astype(str), format="%Y%m%d%H%M", errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["dt", "close"]).drop(columns=["dt_raw"])
    df["date"] = df["dt"].dt.strftime("%Y%m%d")
    return df.sort_values("dt").reset_index(drop=True)


def drop_unclosed(df: pd.DataFrame, span_sec: int, now: datetime | None = None) -> pd.DataFrame:
    """只保留已收盘 bar（收盘后全部保留）。"""
    if df.empty:
        return df
    now = now or datetime.now(CN)
    naive = now.replace(tzinfo=None)
    if naive.hour * 60 + naive.minute >= 15 * 60 + 1:      # 15:01 之后当天已收盘
        return df
    cutoff = naive - timedelta(seconds=span_sec)
    return df[df["dt"] <= cutoff].reset_index(drop=True)


# ── TD ────────────────────────────────────────────────────

def setup_by_session(df: pd.DataFrame, cmp_length: int = DEFAULT_CMP) -> pd.DataFrame:
    """按交易日切分算 setup 计数 —— 直接复用策略引擎，保证与执行层同源。"""
    if df.empty:
        return df
    from ..strategies.td_sequential_futu import FutuDeMarkEngine   # 延迟导入，避免循环

    out: list[pd.DataFrame] = []
    for _, g in df.groupby("date", sort=True):
        g = g.copy()
        if len(g) <= cmp_length:
            g["buy_setup_count"] = 0
            g["sell_setup_count"] = 0
            out.append(g)
            continue
        work = g.rename(columns={"open": "Open", "high": "High",
                                 "low": "Low", "close": "Close", "volume": "Volume"})
        try:
            res = FutuDeMarkEngine(work, {"compare_length": cmp_length}).calculate_setup()
        except Exception:      # noqa: BLE001 —— 单日算不出来也不能拖垮整块看板
            g["buy_setup_count"] = 0
            g["sell_setup_count"] = 0
            out.append(g)
            continue
        for col in ("buy_setup_count", "sell_setup_count"):
            g[col] = res[col].to_numpy() if col in res.columns else 0
        out.append(g)
    return pd.concat(out, ignore_index=True).sort_values("dt").reset_index(drop=True)


def _last_signal(df: pd.DataFrame, col: str) -> dict | None:
    """最近一次「首穿 9」（与策略 first-cross 同口径）。"""
    if df.empty or col not in df.columns:
        return None
    c = df[col].to_numpy()
    prev = pd.Series(c).shift(1).fillna(0).to_numpy()
    hits = df[(c >= SETUP_N) & (prev < SETUP_N)]
    if hits.empty:
        return None
    r = hits.iloc[-1]
    return {"dt": r["dt"].strftime("%m-%d %H:%M"), "close": round(float(r["close"]), 3)}


def _session_state(now: datetime) -> str:
    """A 股交易时段状态（北京时间）。"""
    n = now.replace(tzinfo=None)
    hm = n.hour * 60 + n.minute
    if hm < 9 * 60 + 30:
        return "pre"
    if hm <= 11 * 60 + 30:
        return "am"
    if hm < 13 * 60:
        return "noon"
    if hm <= 15 * 60:
        return "pm"
    return "closed"


def _remaining_bars(label: str, now: datetime) -> int:
    """今日收盘前还能走出几根该周期 bar。"""
    span_min = PERIODS[label][1] // 60
    n = now.replace(tzinfo=None)
    cur = n.hour * 60 + n.minute
    remaining_min = 0
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        if cur < start:
            remaining_min += end - start
        elif start <= cur < end:
            remaining_min += end - cur
    return int(remaining_min // span_min)


def describe_period(df: pd.DataFrame, label: str,
                    now: datetime | None = None) -> dict:
    """单周期 → 看板用的描述块。"""
    if df.empty:
        return {"period": label, "ok": False, "error": "无数据"}

    now = now or datetime.now(CN)
    session = _session_state(now)
    last = df.iloc[-1]
    buy_cnt = int(last.get("buy_setup_count", 0) or 0)
    sell_cnt = int(last.get("sell_setup_count", 0) or 0)
    remaining = _remaining_bars(label, now)

    def _side(cnt: int, col: str) -> dict:
        need = max(0, SETUP_N - cnt)
        sig = _last_signal(df, col)
        return {
            "count": cnt,
            "need": need,
            "state": "triggered" if cnt >= SETUP_N else ("near" if cnt >= SETUP_N - 2 else "counting"),
            "can_fire_today": bool(need and remaining >= need),
            "last_signal": sig["dt"] if sig else None,
            "last_signal_close": sig["close"] if sig else None,
        }

    return {
        "period": label,
        "ok": True,
        "bars": int(len(df)),
        "last_time": last["dt"].strftime("%m-%d %H:%M"),
        "last_close": round(float(last["close"]), 3),
        "session": session,
        "remaining_bars": remaining,
        "buy": _side(buy_cnt, "buy_setup_count"),
        "sell": _side(sell_cnt, "sell_setup_count"),
    }


# ── 面板 ────────────────────────────────────────────────────

def _in_session(now: datetime | None = None) -> bool:
    """交易时段判据与采集器同源（``ashare.tape.in_session``），不在此复制一份。

    延迟导入：看板只借一个判据，不该把采集器的运行时依赖链
    （LiveRunnerBase / credential_registry 等）拉进来。
    """
    from .tape import in_session
    return in_session(now)


def panel(symbol: str = DEFAULT_SYMBOL, ticker: str = "510050",
          force: bool = False) -> dict:
    """看板完整载荷（带 10 秒缓存，避免页面轮询把腾讯打爆）。

    非交易时段（含收盘后 / 午休 / 周末）数据不会再变，直接复用缓存、
    不发任何外部请求——与采集器「交易时段外自动跳过」同一判据。
    """
    key = f"{symbol}:panel"
    now_m = time.monotonic()
    if not force:
        hit = _cache.get(key)
        if hit:
            age = now_m - hit[0]
            if age < CACHE_TTL_S:
                payload = dict(hit[1])
                payload["cached_s"] = round(age, 1)
                return payload
            if not _in_session():
                payload = dict(hit[1])
                payload["cached_s"] = round(age, 1)
                payload["frozen"] = True
                return payload

    periods: dict[str, dict] = {}
    errors: list[str] = []
    now = datetime.now(CN)
    for label, (bar, span) in PERIODS.items():
        try:
            raw = fetch_klines(bar=bar, n=640, symbol=symbol)
            closed = drop_unclosed(raw, span, now)
            with_td = setup_by_session(closed)
            periods[label] = describe_period(with_td, label, now)
        except Exception as exc:      # noqa: BLE001 —— 单周期失败不影响其它周期
            periods[label] = {"period": label, "ok": False, "error": str(exc)[:200]}
            errors.append(f"{label}: {exc}")

    spot = None
    one = periods.get("1m") or {}
    if one.get("ok"):
        spot = one.get("last_close")

    # 最新已收盘 bar 的时刻（按最高频可用周期取）——与 asof（本次拉取时刻）
    # 是两个概念：收盘后 asof 仍会随轮询前进，data_asof 恒停在同一根 bar。
    data_asof = None
    for label in ("1m", "5m", "15m"):
        p = periods.get(label) or {}
        if p.get("ok"):
            data_asof = p.get("last_time")
            break

    payload = {
        "ok": any(p.get("ok") for p in periods.values()),
        "ticker": ticker,
        "symbol": symbol,
        "spot": spot,
        "asof": now.strftime("%Y-%m-%d %H:%M:%S"),
        "data_asof": data_asof,
        "frozen": False,
        "session": _session_state(now),
        "periods": periods,
        "source": "tencent",
        "note": "按交易日切分（隔夜从 0 重数）· 计数与策略同源（FutuDeMarkEngine）",
        "cached_s": 0.0,
    }
    if errors:
        payload["errors"] = errors
    _cache[key] = (now_m, payload)
    return payload


def clear_cache() -> None:
    _cache.clear()


__all__ = ["panel", "fetch_klines", "setup_by_session", "describe_period",
           "drop_unclosed", "clear_cache", "PERIODS", "SETUP_N"]
