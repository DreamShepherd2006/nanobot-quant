"""OKX CEX 期权链数据模块（只读，公共端点，免 key）— 期权线批次 B。

基于官方 python-okx SDK（见 :mod:`okx_sdk`）：
- 链结构：Public.get_instruments(instType="OPTION", instFamily=...)
  （strike 字段 ``stk``；每张面值 = ctVal × ctMult；到期 expTime 毫秒）
- IV 与希腊值：Public.get_opt_summary(instFamily=...)（markVol/delta 等）
- 盘口：Market.get_tickers(instType="OPTION")（bidPx/askPx）
- 现货 HV：Market.get_history_candles(instId=BTC-USDT, bar=1D)

价格口径（OKX 币本位反向期权，实证推导）：
- 每张面值币数 = float(ctVal) × float(ctMult)（BTC-USD = 1 × 0.01 = 0.01 BTC）
- 权利金报价单位为「面值比例」（如 ask=0.0046 → 0.46%）
- 每张权利金(币) = askPx × 面值币数；USD 折算 × 现货价
- cash-secured 行权担保 USD = strike × 面值币数 × 现货价（简化口径，
  正式保证金以 OKX 计算为准——C 期下单对接实证）
"""

from __future__ import annotations

import datetime
import math
import time
from typing import Any

from nanobot_quant import okx_sdk
from nanobot_quant.okx_sdk import OkxSdkError

# 页面内多次请求共享的模块级缓存（TTL 秒）
_CACHE_TTL = 8.0
_cache: dict[str, tuple[float, Any]] = {}

_SPOT = {"BTC-USD": "BTC-USDT", "ETH-USD": "ETH-USDT"}
FAMILIES = ("BTC-USD", "ETH-USD")


def _cached(key: str, producer):
    item = _cache.get(key)
    if item and time.time() - item[0] < _CACHE_TTL:
        return item[1]
    value = producer()
    _cache[key] = (time.time(), value)
    return value


def _instruments(family: str) -> list[dict]:
    def _load():
        return okx_sdk.check(okx_sdk.public().get_instruments(
            instType="OPTION", instFamily=family))
    return _cached(f"inst:{family}", _load)


def _tickers() -> dict[str, dict]:
    def _load():
        rows = okx_sdk.check(okx_sdk.market().get_tickers(instType="OPTION"))
        return {r["instId"]: r for r in rows}
    return _cached("tickers:OPTION", _load)


def _opt_summary(family: str) -> dict[str, dict]:
    def _load():
        rows = okx_sdk.check(okx_sdk.public().get_opt_summary(instFamily=family))
        return {r["instId"]: r for r in rows}
    return _cached(f"osum:{family}", _load)


def _spot_price(family: str) -> float | None:
    def _load():
        rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=_SPOT[family]))
        try:
            return float(rows[0]["last"]) if rows and rows[0].get("last") else None
        except (TypeError, ValueError, IndexError):
            return None
    return _cached(f"spot:{family}", _load)


def _spot_hv(family: str, days: int = 30) -> dict:
    """现货日线年化历史波动率（对数收益标准差 × sqrt(365)）。"""
    inst = _SPOT[family]
    limit = min(max(days + 5, 20), 300)

    def _load():
        rows = okx_sdk.check(okx_sdk.market().get_history_candles(
            instId=inst, bar="1D", limit=str(limit)))
        closes = [float(r[4]) for r in rows[: days + 1] if r[4]]
        closes.reverse()
        if len(closes) < 3:
            return {"hv_pct": None, "days": 0, "inst": inst}
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
                if closes[i - 1] > 0]
        if len(rets) < 2:
            return {"hv_pct": None, "days": 0, "inst": inst}
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return {"hv_pct": math.sqrt(var) * math.sqrt(365.0) * 100.0,
                "days": len(rets), "inst": inst}
    return _cached(f"hv:{inst}:{days}", _load)


def _lot_coin(row: dict) -> float:
    """每张合约面值币数 = ctVal × ctMult。"""
    try:
        return float(row.get("ctVal") or 0) * float(row.get("ctMult") or 1)
    except (TypeError, ValueError):
        return 0.0


def _f(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _expiry_info(exp_ms: int, now_ms: int) -> dict:
    exp_dt = datetime.datetime.fromtimestamp(exp_ms / 1000, datetime.timezone.utc)
    days = max(int(math.ceil((exp_ms - now_ms) / 86400000.0)), 0)
    return {"exp_ms": exp_ms, "date": exp_dt.strftime("%Y-%m-%d"), "days": days}


# ── 公开入口（同步；调用方在 async handler 内经 asyncio.to_thread）─────

def list_expiries(family: str) -> list[dict]:
    """该 family 全部 live 到期（升序，仅未到期）。"""
    if family not in FAMILIES:
        raise OkxSdkError(f"未知标的 {family}，可选 {FAMILIES}")
    insts = [i for i in _instruments(family) if i.get("state") == "live"]
    now = int(time.time() * 1000)
    out = []
    for exp in sorted({int(i["expTime"]) for i in insts}):
        if exp > now:
            out.append(_expiry_info(exp, now))
    return out


def fetch_chain(family: str, expiries: list[int] | None = None,
                spot_pct_range: float | None = 20.0,
                hv_days: int = 30) -> dict:
    """组装期权链：选定期到期的 strike × Call/Put 定价表 + 现货 HV。

    expiries=None → 默认最近 3 个未到期；spot_pct_range=±20% 过滤。
    """
    insts = [i for i in _instruments(family) if i.get("state") == "live"]
    now = int(time.time() * 1000)
    exp_set = sorted({int(i["expTime"]) for i in insts if int(i["expTime"]) > now})
    if not exp_set:
        raise OkxSdkError(f"{family} 无未到期合约")
    chosen = expiries or exp_set[:3]

    lot = _lot_coin(insts[0]) if insts else 0.0
    spot = _spot_price(family)
    hv = _spot_hv(family, hv_days)
    tickers = _tickers()
    summary = _opt_summary(family)

    groups = []
    for exp in chosen:
        contracts = [i for i in insts if i.get("expTime") == str(exp)]
        if not contracts:
            continue
        by_strike: dict[float, dict] = {}
        for c in contracts:
            st = float(c["stk"])
            side = "C" if c.get("optType") == "C" else "P"
            by_strike.setdefault(st, {"strike": st})[side] = c["instId"]
        rows = []
        for st in sorted(by_strike):
            if spot and spot_pct_range and spot > 0:
                if st < spot * (1 - spot_pct_range / 100.0) or st > spot * (1 + spot_pct_range / 100.0):
                    continue
            pair = by_strike[st]
            row: dict = {"strike": st}
            for side in ("C", "P"):
                inst_id = pair.get(side)
                cell = {"inst_id": inst_id or "", "bid": None, "ask": None,
                        "iv": None, "delta": None, "prem_usd": None,
                        "prem_pct": None, "apr_pct": None}
                if inst_id:
                    tk = tickers.get(inst_id, {})
                    cell["bid"] = _f(tk.get("bidPx"))
                    cell["ask"] = _f(tk.get("askPx"))
                    os_ = summary.get(inst_id, {})
                    iv = _f(os_.get("markVol"))
                    cell["iv"] = iv * 100 if iv is not None else None
                    cell["delta"] = _f(os_.get("delta"))
                    if side == "P" and cell["ask"] is not None and lot and spot:
                        ask_pct = cell["ask"]
                        prem_coin = ask_pct * lot
                        cell["prem_usd"] = prem_coin * spot
                        cell["prem_pct"] = ask_pct * 100.0
                        exp_days = max((exp - now) / 86400000.0, 1 / 365.0)
                        cell["apr_pct"] = ask_pct * 100.0 * 365.0 / exp_days
                row[side] = cell
            rows.append(row)
        if rows:
            info = _expiry_info(exp, now)
            groups.append({
                "exp_ms": exp, "date": info["date"], "days": info["days"],
                "rows": rows, "contracts": len(contracts),
            })

    return {
        "family": family,
        "spot": spot,
        "spot_inst": _SPOT[family],
        "lot_coin": lot,
        "hv": hv,
        "groups": groups,
        "chosen": chosen,
        "expiries": exp_set,
        "price_note": "权利金报价=面值比例（BTC-USD 每张面值 0.01 BTC）；"
                      "年化=面值比例×365/剩余天数；行权担保=strike×面值×现货价（简化口径，"
                      "正式保证金以 OKX 计算为准）",
    }
