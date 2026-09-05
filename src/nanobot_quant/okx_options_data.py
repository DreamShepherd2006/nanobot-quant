"""OKX U 本位期权链数据模块（USDSⓈ-M 线性期权，只读，公共端点，免 key）— 期权线批次 B。

基于官方 python-okx SDK（见 :mod:`okx_sdk`）：
- 链结构：Public.get_instruments(instType="OPTION", instFamily=...)
  （strike 字段 ``stk``；每张名义面值 = ctVal × ctMult；到期 expTime 毫秒）
- IV 与希腊值：Public.get_opt_summary(instFamily=...)（markVol/delta 等）
- 盘口：Market.get_tickers(instType="OPTION")（bidPx/askPx）
- 现货 HV：Market.get_history_candles(instId=BTC-USDT, bar=1D)

价格口径（OKX U 本位线性期权，2026-09-04 实测）：
- 家族 instFamily = BTC-USD_UM / ETH-USD_UM（带 _UM 后缀；USDT/USDC 家族格式不存在）
- ctType=linear、settleCcy=USD（美元结算，稳定币担保）；ctVal=1 × ctMult=0.01 = 0.01 BTC 名义/张
- 盘口 bidPx/askPx 单位 = USD / 1 单位名义币（如 BTC-USD_UM ATM put ask≈890 → 每张 ≈ 890×0.01 = $8.9）
- 每张权利金 USD = askPx × lot（直接美元，不再 ×现货价——币本位才需 ×spot）
- 面值比例 % = askPx / 现货价 × 100（权利金占 1 名义币现货价值比，与币本位 prem_pct 同语义）
- cash-secured 名义担保 = strike × lot × 张数（USD；正式保证金以 OKX 计算为准——C 期对接实证）
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

# 参考价来源：现货盘（_SPOT）或指数盘（_INDEX，无现货盘的家族如 XAU）
_SPOT = {"BTC-USD_UM": "BTC-USDT", "ETH-USD_UM": "ETH-USDT", "SOL-USD_UM": "SOL-USDT"}
_INDEX = {"XAU-USD_UM": "XAU-USD"}
FAMILIES = ("BTC-USD_UM", "ETH-USD_UM", "SOL-USD_UM", "XAU-USD_UM")


def _ref_inst(family: str) -> tuple[str | None, str]:
    """返回 (参考价 instId, 类型 spot|index)；未知家族 → (None, '')."""
    if family in _SPOT:
        return _SPOT[family], "spot"
    if family in _INDEX:
        return _INDEX[family], "index"
    return None, ""


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


def get_ticker_bid_ask(inst_id: str) -> dict:
    """单合约实时盘口（bid/ask，USD / 1 名义币）。

    供平仓/卖 put 弹窗预填 px——不依赖期权链 tab 是否刷过（持仓 tab
    直接平仓时无需先回链页刷新）。走 _tickers() 8s 缓存与链页同源。
    """
    tk = _tickers().get(inst_id.upper())
    if not tk:
        return {"found": False, "bid": None, "ask": None}
    return {"found": True, "bid": _f(tk.get("bidPx")), "ask": _f(tk.get("askPx"))}


def _opt_summary(family: str) -> dict[str, dict]:
    def _load():
        rows = okx_sdk.check(okx_sdk.public().get_opt_summary(instFamily=family))
        return {r["instId"]: r for r in rows}
    return _cached(f"osum:{family}", _load)


def _spot_price(family: str) -> float | None:
    ref, kind = _ref_inst(family)
    if not ref:
        return None

    def _load():
        if kind == "index":
            # XAU 等无现货盘的家族：直接取指数价（idxPx）；okx 1.0.9 方法名复数 get_index_tickers
            idx = okx_sdk.check(okx_sdk.market().get_index_tickers(instId=ref))
            try:
                return float(idx[0]["idxPx"]) if idx and idx[0].get("idxPx") else None
            except (TypeError, ValueError, IndexError):
                return None
        rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=ref))
        try:
            return float(rows[0]["last"]) if rows and rows[0].get("last") else None
        except (TypeError, ValueError, IndexError):
            return None

    return _cached(f"spot:{family}", _load)


def _spot_hv(family: str, days: int = 30) -> dict:
    """现货日线年化历史波动率（对数收益标准差 × sqrt(365)）。

    仅现货盘家族可算（需日线成交价）；无现货盘（index 参考，如 XAU）→ hv_pct=None。
    """
    ref, kind = _ref_inst(family)
    if not ref:
        return {"hv_pct": None, "days": 0, "inst": None}
    if kind != "spot":
        return {"hv_pct": None, "days": 0, "inst": ref, "note": "无现货盘（指数参考价）"}
    inst = ref
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


# ── 生命周期：单期权合约从上市到现在的 mark 价 + 同时刻参考价 ────────

# 周期与 K 线体系完全对齐（td-table/回测/调度同源 16 统一周期）。
# OKX mark-price-candles 原生 bar 实测缺 8H/7D/30D（51000）；
# 7D→1W、30D→1M 语义等价映射（PERIODS 注释：30D=日历月近似=OKX 1M），
# 8H 无法映射 → 请求时显式报错。
from nanobot_quant.data_sources.periods import PERIODS as _PERIODS

LIFECYCLE_BARS: tuple[str, ...] = _PERIODS
_OKX_BAR_MAP = {"7D": "1W", "30D": "1M"}
_OKX_BAR_UNAVAILABLE = {"8H": "OKX 期权市场无 8H 周期（可用 6H 或 12H）"}
_LC_MAX_PAGES = 20    # 每页 ≤300 根 → 生命周期视图最多 ~6000 根，超出截断标记
# 典型生命周期根数：3 天合约 1m≈4300 / 3m≈1450 / 5m≈870 / 15m≈290——细粒度全生命周期可拿


def _family_of(inst_id: str) -> str:
    """SOL-USD_UM-260906-101-P → SOL-USD_UM（尾部固定 3 段 = exp/strike/type）。"""
    return inst_id.rsplit("-", 3)[0]


def _find_inst(inst_id: str) -> dict:
    """在家族 instruments（含已到期）中定位单合约；未知家族/合约报错。"""
    family = _family_of(inst_id)
    if family not in FAMILIES:
        raise OkxSdkError(f"未知标的家族 {family}，可选 {FAMILIES}")
    for i in _instruments(family):
        if i.get("instId") == inst_id:
            return i
    raise OkxSdkError(f"合约不存在或已被移除: {inst_id}")


def _mark_candle_pages(inst_id: str, bar: str) -> tuple[dict[int, float], bool]:
    """某期权合约 mark 价全生命周期（ts → close，升序合并）。

    OKX 分页语义：after=<ts> 返回更早数据（与多数交易所相反，官方 CLI 文档钉死）。
    先翻 mark-price-candles 近期段，**段空 ≠ 尽头**（mark-price-candles 深度浅，
    实测 1m 粒度只回溯 ~24h）——必须自动接 history-mark 段继续往前翻，直至
    history 段也空（= 真到 listTime）。总页数上限防异常 → truncated 标记。
    去重 key=ts，先收集者优先。
    """
    out: dict[int, float] = {}
    truncated = False
    total_pages = 0
    for method in ("get_mark_price_candles", "get_history_mark_price_candles"):
        fn = getattr(okx_sdk.market(), method)
        after = str(min(out)) if out else ""
        while total_pages < _LC_MAX_PAGES:
            payload = fn(instId=inst_id, bar=bar, after=after, limit="300")
            batch = okx_sdk.check(payload) or []
            if not batch:
                break
            for r in batch:
                ts = int(r[0])
                out.setdefault(ts, float(r[4]))
            after = str(min(out))
            total_pages += 1
        if total_pages >= _LC_MAX_PAGES:
            truncated = True
            break
        if method == "get_history_mark_price_candles":
            break            # history 段翻空 = 真到 listTime，结束
    return out, truncated


def _ref_prices(ref_inst: str, kind: str, bar: str, need_oldest: int) -> dict[int, float]:
    """参考价（现货或 index）同粒度 K 线，覆盖到期权最早 bar。"""
    fn = (okx_sdk.market().get_history_index_candles if kind == "index"
          else okx_sdk.market().get_history_candles)
    out: dict[int, float] = {}
    after = ""
    for _ in range(_LC_MAX_PAGES):
        payload = fn(instId=ref_inst, bar=bar, after=after, limit="300")
        batch = okx_sdk.check(payload) or []
        if not batch:
            break
        for r in batch:
            ts = int(r[0])
            out.setdefault(ts, float(r[4]))
        oldest = min(out)
        if oldest <= need_oldest:
            break
        after = str(oldest)
    return out


def fetch_lifecycle(inst_id: str, bar: str = "15m") -> dict:
    """某期权合约从上市（listTime）到现在的 mark 价 + 同时刻标的参考价。

    数据源：mark-price-candles（+ history 段接续）——mark 价由交易所模型持续
    计算、无成交时段也连续，是「设立→行权」价值曲线的正确载体（成交 K 线只
    覆盖有成交的时段，9/3 上市合约 9/4 前可能全空白）。参考价（现货/USDC 或
    index）同粒度按 bar 起点 ts 对齐，可直接观察 put 权利金与标的价的负相关。

    returns: {"inst_id", "family", "strike", "opt_type", "exp_ms", "list_ms",
              "lot_coin", "ref_inst", "ref_kind", "bar",
              "rows": [{"ts", "mark_px", "ref_px"} 升序], "truncated"}
    """
    inst_id = inst_id.strip().upper()
    if bar not in LIFECYCLE_BARS:
        raise OkxSdkError(f"不支持粒度 {bar}，可选 {LIFECYCLE_BARS}")
    if bar in _OKX_BAR_UNAVAILABLE:
        raise OkxSdkError(_OKX_BAR_UNAVAILABLE[bar])
    okx_bar = _OKX_BAR_MAP.get(bar, bar)
    inst = _find_inst(inst_id)
    family = inst.get("instFamily") or _family_of(inst_id)
    mark_rows, truncated = _mark_candle_pages(inst_id, okx_bar)
    if not mark_rows:
        raise OkxSdkError(f"合约 {inst_id} 暂无 mark K 线数据")

    times = sorted(mark_rows)
    ref_inst, kind = _ref_inst(family)
    ref_map: dict[int, float] = {}
    if ref_inst:
        ref_map = _ref_prices(ref_inst, kind, okx_bar, times[0])
    return {
        "inst_id": inst_id,
        "family": family,
        "strike": float(inst.get("stk") or 0),
        "opt_type": inst.get("optType"),
        "exp_ms": int(inst.get("expTime") or 0),
        "list_ms": int(inst.get("listTime") or 0),
        "lot_coin": _lot_coin(inst),
        "ref_inst": ref_inst or None,
        "ref_kind": kind or None,
        "bar": bar,
        "rows": [{"ts": ts, "mark_px": mark_rows[ts], "ref_px": ref_map.get(ts)}
                  for ts in times],
        "truncated": truncated,
    }


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
                        ask = cell["ask"]
                        # U 本位线性：bidPx/askPx = USD/1 名义币 → 每张 = ask × lot（直接 USD）
                        cell["prem_usd"] = ask * lot
                        cell["prem_pct"] = ask / spot * 100.0
                        exp_days = max((exp - now) / 86400000.0, 1 / 365.0)
                        cell["apr_pct"] = cell["prem_pct"] * 365.0 / exp_days
                row[side] = cell
            rows.append(row)
        if rows:
            info = _expiry_info(exp, now)
            groups.append({
                "exp_ms": exp, "date": info["date"], "days": info["days"],
                "rows": rows, "contracts": len(contracts),
            })

    ref, kind = _ref_inst(family)
    spot_inst_label = ref if kind == "spot" else f"{ref} (index)" if ref else family
    return {
        "family": family,
        "spot": spot,
        "spot_inst": spot_inst_label,
        "lot_coin": lot,
        "hv": hv,
        "groups": groups,
        "chosen": chosen,
        "expiries": exp_set,
        "price_note": "U 本位线性期权（USDSⓈ-M）：盘口 = USD/1 名义币；每张权利金(USD) = ask × 每张面值；"
                      f"面值比例 = ask ÷ 现货参考价；现金担保 = strike × 每张面值 × 张数 "
                      "（简化口径，正式保证金以 OKX 计算为准）",
    }
