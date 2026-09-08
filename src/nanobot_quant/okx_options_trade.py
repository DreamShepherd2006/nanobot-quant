"""OKX 期权卖 put 执行层 — 期权线批次 C（U 本位 USDSⓈ-M，官方 python-okx Trade/Account）。

职责：
- 卖 put（开仓，side=sell）+ 买回平仓（side=buy）——OKX ``Trade.set_order``
- 现货补买（到期 ITM 现金结算后，手动闭环持有现货）
- 本地台账（``okx_options_ledger.json``，credential 同目录）：卖出的 put 跨重启
  保留，OKX 仓位在平仓/结算后消失，页面历史与到期监控依赖台账
- 持仓/到期只读查询（Account.get_positions）

口径（批次 B/C 定稿，官方 docs + 实测 2026-09-04）：
- U 本位线性：bidPx/askPx = USD/1 单位名义币，每张面值 lot = ctVal×ctMult
  （BTC 0.01 / ETH 0.01 / SOL 0.1 / XAU 0.01），每张权利金(USD) = px × lot
- 卖 put 逐仓（isolated）模式，每张独立保证金/强平边界（多笔低9 卖 put 同账号
  并存互不拖累）；等效现金担保（自留口径）= Σ(strike × lot × sz)（账户现金，不设杠杆）
- 到期结算：欧式、现金结算（settle = 到期日 08:00 UTC 后 30 分钟 TWAP，官方口径）；
  OKX 到期自动结算入账，本模块不重复算钱，到期后经台账标 ``settled`` 并引导核对账单
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import okx_sdk
from .okx_sdk import OkxSdkError

#: OKX 期权保证金模式（2026-09-04 实测网页 + 官方 agent-skills 确认）：
#: 纯买方开仓(cash)付全权利金无杠杆；我们只卖 put（side=sell）= isolated
#: 逐仓——TD 低9 多笔卖 put 在同一子账号并存，逐仓给每张独立保证金与
#: 强平边界（亏穿只平该张，不拖累同账号其他仓位），无需每笔开子账号。
#: 等效现金担保 = 账户自留 ≥ Σ(strike×面值) 现金（不设杠杆，平台不冻结全额）。
SELL_TDMODE = "isolated"
BUY_TDMODE = "cash"
#: 跨币种保证金（acctLv=3 net_mode）下逐仓期权仓的 posSide 标识为 net
POS_SIDE = "net"
#: 平仓/减仓单的 tdMode 必须与持仓保证金模式一致（OKX 规则：不匹配 →
#: 51000 Parameter tdMode error）。本仓开在 isolated（SELL_TDMODE），
#: 买回平仓同样 isolated；cash 仅用于无平仓对象的纯买入开仓。
CLOSE_TDMODE = "isolated"

#: OKX 期权无市价单（官方 docs："For OPTION, market order is not supported yet"——
#: 成交价无法预知、保证金无法预冻结；实测 isolated buy market 亦 50016）。
#: px 必须客户端自定价——建议价规则（页面预填与自动化共用同一规则）：
#: 卖 put（开仓）：IOC px = bid × 0.5 保护线——正常盘口 px<bid 吃单成交在最优买价，
#:   盘口闪崩至 bid×0.5 以下自动拒单（防贱卖）；低比例非目标价而是「最差接受线」。
#: 买回（平仓）：IOC px = ask——精确吃当前卖一；ask 波动升高自动撤、下轮重试不追价。
SELL_PX_BID_RATIO = 0.5


def suggest_sell_px(bid: Optional[float]) -> Optional[float]:
    """卖 put 开仓建议 px（IOC 保底线 = bid×0.5）。

    px 只是保护线不是目标价：正常盘口 IOC 扫单仍按最优买价（bid）成交，
    仅当盘口崩至 bid×0.5 以下时自动撤单。bid 缺失/非正 → None
    （自动化调用方须 fail-closed 不下单）。页面预填与此同规则（toFixed(2)）。
    """
    if not bid or bid <= 0:
        return None
    return round(bid * SELL_PX_BID_RATIO, 2)


def suggest_close_px(ask: Optional[float]) -> Optional[float]:
    """买回平仓建议 px = 当前 ask（吃买一，不留缓冲）。

    px 是最高接受价：填 ask 精确吃当前卖一；ask 升高 IOC 自动撤单、
    下轮重试（薄盘口不追价，重试成本低）。ask 缺失/非正 → None。
    """
    if not ask or ask <= 0:
        return None
    return round(ask, 2)
#: 订单来源标签（OKX tag：纯字母数字、<=16 位）
TAG_OPEN = "nbputo1"
TAG_CLOSE = "nbputc1"
TAG_COVER = "nbcov1"

_LEDGER_NAME = "okx_options_ledger.json"
_TTL = 30.0  # 两步确认一次性 tx_id 有效期（秒）


# ── 台账（持久化）──────────────────────────────────────────────

def _storage_dir() -> Path:
    from .credential_registry import _get_storage_dir

    return Path(_get_storage_dir())


def ledger_path() -> Path:
    return _storage_dir() / _LEDGER_NAME


def load_ledger() -> list[dict]:
    p = ledger_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_ledger(entries: list[dict]) -> None:
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)


# ── 期权参数（WebUI 担保设置，独立于现货 exec_params）───────────

_PARAMS_NAME = "okx_options_params.json"
DEFAULT_COLLATERAL_RATIO_PCT = 100


def params_path() -> Path:
    return _storage_dir() / _PARAMS_NAME


def load_option_params() -> dict:
    p = params_path()
    if not p.exists():
        return {"collateral_ratio_pct": DEFAULT_COLLATERAL_RATIO_PCT}
    try:
        d = json.loads(p.read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_option_params(**fields) -> dict:
    d = load_option_params()
    d.update(fields)
    ratio = d.get("collateral_ratio_pct", DEFAULT_COLLATERAL_RATIO_PCT)
    try:
        ratio = int(ratio)
    except (TypeError, ValueError):
        ratio = DEFAULT_COLLATERAL_RATIO_PCT
    d["collateral_ratio_pct"] = min(max(ratio, 0), 200)
    p = params_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(p)
    return d


def collateral_ratio_pct() -> int:
    """逐仓自动追加担保比例：全损(strike×面值×张数)的百分比，0 = 关闭自动追加。"""
    return load_option_params().get(
        "collateral_ratio_pct", DEFAULT_COLLATERAL_RATIO_PCT)


def _new_id() -> str:
    return f"{int(time.time()*1000):x}{secrets.token_hex(3)}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_ledger(**fields) -> dict:
    entry = {"id": _new_id(), "ts": _utc_now(), "status": "pending", **fields}
    entries = load_ledger()
    entries.append(entry)
    save_ledger(entries)
    return entry


def update_ledger(pred: Callable[[dict], bool], **fields) -> Optional[dict]:
    entries = load_ledger()
    hit = None
    for e in entries:
        if pred(e):
            e.update(fields)
            hit = e
            break
    if hit is not None:
        save_ledger(entries)
    return hit


def find_entry(pred: Callable[[dict], bool]) -> Optional[dict]:
    for e in reversed(load_ledger()):
        if pred(e):
            return e
    return None


# ── instrument / 盘口辅助 ──────────────────────────────────────

def inst_family_of(inst_id: str) -> str:
    """从 instId 解析 instFamily（如 SOL-USD_UM-260905-101-P → SOL-USD_UM）。

    OKX OPTION instId = {instFamily}-{yyMMdd}-{strike}-{C/P}；instFamily
    本身含连字符（SOL-USD_UM），故从尾部日期段反向切分。
    """
    m = re.search(r"-\d{6}-\d+(?:\.\d+)?-[CP]$", inst_id or "")
    return inst_id[: m.start()] if m else (inst_id or "")


def resolve_instrument(inst_id: str) -> dict:
    """单只期权合约规格（instId → stk/expTime/ctVal/ctMult/lot/optType/uly）。"""
    fam = inst_family_of(inst_id)
    # OPTION 查询必须带 instFamily/uly（官方 50015 约束），即使给了 instId
    rows = okx_sdk.check(okx_sdk.public().get_instruments(
        instType="OPTION", instFamily=fam, instId=inst_id))
    if not rows:
        raise OkxSdkError(f"OKX 查无期权合约 {inst_id}")
    r = rows[0]
    lot = _f(r.get("ctVal")) * _f(r.get("ctMult"))
    return {
        "inst_id": inst_id,
        "inst_family": r.get("instFamily", ""),
        "opt_type": r.get("optType", ""),
        "strike": _f(r.get("stk")),
        "exp_ms": int(r.get("expTime") or 0),
        "lot": lot or 0.0,
        "uly": r.get("uly", ""),
        "state": r.get("state", ""),
    }


# U 本位线性期权家族固定面值（ctVal=1 × ctMult，官方规格；SOL 0.1 币/张，其余 0.01）。
# 供到期补买预填等场景：合约到期后 OKX instruments 不再返回规格，面值须本地解析。
FAMILY_LOT = {"BTC": 0.01, "ETH": 0.01, "SOL": 0.1, "XAU": 0.01}


def cover_prefill_defaults(inst_id: str, sz: int, spot_px: float | None = None,
                           entry: Optional[dict] = None) -> dict:
    """到期 ITM 补买预填：数量 = 面值(lot)×张数；价格默认 = 现货现价
    （fallback：结算价 → 行权价，全部可在页面修改）。
    """
    lot = FAMILY_LOT.get((inst_id or "").split("-")[0], 0.0)
    if not lot:
        try:
            inst = resolve_instrument(inst_id)
            lot = float(inst.get("lot") or 0.0)
        except Exception:
            lot = 0.0
    px, src = spot_px, "spot"
    if not px and entry:
        if entry.get("settle_px"):
            px, src = entry.get("settle_px"), "settle"
        elif entry.get("strike"):
            px, src = entry.get("strike"), "strike"
    return {"qty": round(lot * sz, 6) if lot else 0.0,
            "px": px, "px_src": src if px else None}


def ticker_quote(inst_id: str) -> dict:
    """当前盘口/最新（bidPx/askPx/last —— USD/1 名义币）。"""
    rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=inst_id))
    r = rows[0] if rows else {}
    return {
        "bid": _f(r.get("bidPx")),
        "ask": _f(r.get("askPx")),
        "last": _f(r.get("last")),
    }


def order_book(inst_id: str, levels: int = 5) -> dict:
    """多档盘口（USD/1 名义币；size 单位=张；bids 降序 / asks 升序）。

    C22a 盘口深度模拟数据源：卖 put 吃买盘(bids)、买回平仓吃卖盘(asks)。
    期权 books 公共端点与现货同构——level = [price, size, ...]。
    """
    rows = okx_sdk.check(okx_sdk.market().get_books(instId=inst_id, sz=str(levels)))
    book = rows[0] if rows else {}
    bids, asks = [], []
    for lv in book.get("bids") or []:
        if len(lv) >= 2:
            px, amt = _f(lv[0]), _f(lv[1])
            if px and amt and px > 0 and amt > 0:
                bids.append([px, amt])
    for lv in book.get("asks") or []:
        if len(lv) >= 2:
            px, amt = _f(lv[0]), _f(lv[1])
            if px and amt and px > 0 and amt > 0:
                asks.append([px, amt])
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {"bids": bids, "asks": asks, "ts": book.get("ts")}


def simulate_fill(inst_id: str, side: str, sz_qty: int, levels: int = 5) -> Optional[dict]:
    """盘口吃单模拟（C22a 报价模拟；与 IOC/限价扫单同向吃档）。

    side="sell"（卖 put 开仓）→ 吃买盘 bids（最优价起向下）；
    side="buy"（买回平仓）→ 吃卖盘 asks（最优价起向上）。
    按 sz_qty 张逐档累计，输出吃穿档数 / 加权均价 / 相对最优价折让%
    与权利金滑点 USD——回答「现在按盘口市价这单会以什么价成交」。

    盘口不可用 / 无对手档 → 返回 None（调用方容错，不阻塞下单）。
    盘口总量 < sz_qty → full=False + missing（IOC 部分成交后撤余量）。
    """
    side = (side or "sell").lower()
    if side not in ("sell", "buy"):
        raise OkxSdkError(f"side 仅支持 sell/buy，收到 {side}")
    want = float(int(sz_qty) or 0)
    if want <= 0:
        raise OkxSdkError("张数必须为正整数")
    try:
        book = order_book(inst_id, levels)
    except OkxSdkError:
        return None
    legs = book["bids"] if side == "sell" else book["asks"]
    if not legs:
        return None
    lot = resolve_instrument(inst_id)["lot"]
    best = legs[0][0]
    got, cost, used = 0.0, 0.0, 0
    for px, amt in legs:
        if want <= 0:
            break
        take = min(want, amt)
        got += take
        cost += take * px
        want -= take
        used += 1
    avg = cost / got if got > 0 else None
    dip = (best - avg) / best * 100.0 if avg else None
    return {
        "ok": True, "side": side, "qty": int(sz_qty), "best_px": best,
        "avg_px": round(avg, 4) if avg is not None else None,
        "levels_used": used, "filled": round(got, 4), "full": want <= 0,
        "missing": round(want, 4) if want > 0 else 0,
        "dip_pct": round(dip, 3) if dip is not None else None,
        "slip_usd": round((best - avg) * lot * got, 6) if avg else None,
    }


def preview_open_put(inst_id: str, sz: int, ord_type: str = "limit",
                     px: Optional[float] = None) -> dict:
    """卖 put 订单预览（纯计算，不下单）。

    输出含：合约规格、参考盘口、订单参数（side=sell tdMode=isolated 逐仓）、
    预计权利金 = px×lot×sz（limit）或 ask×lot×sz（market 参考）、
    等效现金担保（自留口径 Σ strike×面值）与提示。
    """
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "P":
        raise OkxSdkError(f"{inst_id} 不是 Put 合约（{spec['opt_type']}）")
    lot = spec["lot"]
    if lot <= 0:
        raise OkxSdkError(f"{inst_id} 面值解析失败")
    q = ticker_quote(inst_id)
    ord_type = (ord_type or "limit").lower()
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"不支持的订单类型 {ord_type}：OKX 期权不支持市价单（50016），"
            "仅限价/IOC/FOK/post_only（需带价格）")
    if px is None:
        raise OkxSdkError("期权限价类订单需提供价格 px")
    ref_px = px
    prem = ref_px * lot * sz
    collat = spec["strike"] * lot * sz
    ratio = collateral_ratio_pct()
    try:
        sim = simulate_fill(inst_id, "sell", int(sz))
    except (OkxSdkError, RuntimeError, ValueError):
        sim = None
    exp_iso = _exp_str(spec["exp_ms"])
    return {
        "ok": True,
        "inst_id": inst_id,
        "opt_type": "P",
        "strike": spec["strike"],
        "exp_ms": spec["exp_ms"],
        "exp_date": exp_iso,
        "lot": lot,
        "family": spec["inst_family"],
        "sz": int(sz),
        "ord_type": ord_type,
        "px": ref_px,
        "ref": {"bid": q["bid"], "ask": q["ask"], "last": q["last"]},
        "td_mode": SELL_TDMODE,
        "est_premium_usd": round(prem, 4),
        "collateral_est_usd": round(collat, 2),
        "collateral_target_usd": round(collat * ratio / 100.0, 2),
        "collateral_ratio_pct": ratio,
        "note": ("U 本位线性：盘口=USD/1 名义币，权利金(USD)=px×每张面值；"
                 "卖方以逐仓(isolated)冻结——每张独立保证金/强平边界，多笔卖 put"
                 "共存互不拖累（TD 低9 分批同账号操作，无需多子账号）；"
                 f"成交后自动把该仓保证金追加至担保目标（全损×{ratio}%≈"
                 f"collateral_target_usd），实现等效现金担保——浮亏上限 < 保证金则"
                 "数学上无强平路径，可扛到到期；0% = 关闭自动追加（仅平台默认 IM"
                 "冻结，浮亏可能击穿提前强平）；追加资金在平仓/到期后自动释放；"
                 "账户可用余额须≥追加额。欧式现金结算，不可提前行权。"),
        "sim": sim,
    }


def preview_open_call(inst_id: str, sz: int, ord_type: str = "limit",
                      px: Optional[float] = None,
                      cost_basis: Optional[float] = None) -> dict:
    """卖 call（covered call）订单预览（纯计算，不下单）——镜像 preview_open_put。

    输出含：合约规格、参考盘口、订单参数（side=sell tdMode=isolated 逐仓）、
    预计权利金 = px×lot×sz、保本门状态（K_call + px ≥ C，cost_basis 提供时）、
    covered 语义说明（现货在手覆盖上行，不做全损现金担保）。
    """
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "C":
        raise OkxSdkError(f"{inst_id} 不是 Call 合约（{spec['opt_type']}）")
    lot = spec["lot"]
    if lot <= 0:
        raise OkxSdkError(f"{inst_id} 面值解析失败")
    q = ticker_quote(inst_id)
    ord_type = (ord_type or "limit").lower()
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"不支持的订单类型 {ord_type}：OKX 期权不支持市价单（50016），"
            "仅限价/IOC/FOK/post_only（需带价格）")
    if px is None:
        raise OkxSdkError("期权限价类订单需提供价格 px")
    ref_px = px
    prem = ref_px * lot * sz
    exp_iso = _exp_str(spec["exp_ms"])
    gate = None
    if cost_basis is not None:
        cb = float(cost_basis)
        gate = {"cost_basis": round(cb, 4),
                "guard": round(spec["strike"] + ref_px, 4),
                "ok": (spec["strike"] + ref_px) >= cb - 1e-9}
    try:
        sim = simulate_fill(inst_id, "sell", int(sz))
    except (OkxSdkError, RuntimeError, ValueError):
        sim = None
    return {
        "ok": True,
        "inst_id": inst_id,
        "opt_type": "C",
        "strike": spec["strike"],
        "exp_ms": spec["exp_ms"],
        "exp_date": exp_iso,
        "lot": lot,
        "family": spec["inst_family"],
        "sz": int(sz),
        "ord_type": ord_type,
        "px": ref_px,
        "ref": {"bid": q["bid"], "ask": q["ask"], "last": q["last"]},
        "td_mode": SELL_TDMODE,
        "est_premium_usd": round(prem, 4),
        "gate": gate,
        "note": ("卖 call（covered call）：持有现货 + 卖虚值/平值 call 收权利金。"
                 "被行权 = 现金结算赔付 (结算价−K)×面值 后现货市价卖出，"
                 "等效按 K 出货（保本门保证 K+权利金 ≥ 被动持仓成本 C）。"
                 "call 上行无界，**不做全损现金担保**——covered 由现货在手实现；"
                 "isolated 仅平台 IM 冻结，暴涨接近强平价时需主动买回平仓。"
                 "欧式现金结算，不可提前行权。"),
        "sim": sim,
    }


def _exp_str(exp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"


# ── 下单（真实操作，两步确认由 handler 层执行）──────────────

def _entry_account(account: str) -> dict:
    """account = 子账号 name 或 uid。返回 creds + 匹配条目的 name/uid/label。

    单独再查一次存储以拿到 name/uid 元数据——get_okx_cex_credentials 只返回
    三要素（api_key/secret_key/passphrase），不含账号身份字段。
    """
    from .okx_cex_credentials import (
        get_okx_cex_credentials, load_okx_cex_credentials, normalize_stored,
        _REQUIRED,
    )

    creds = get_okx_cex_credentials(account=account or None)
    name, uid = "", ""
    try:
        stored = normalize_stored(load_okx_cex_credentials() or {})
        subs = stored.get("sub_accounts") or []
        for s in subs:
            hit = (account and (s.get("uid") == account or s.get("name") == account)) or (
                not account and all(s.get(k) for k in _REQUIRED))
            if hit:
                name = s.get("name") or ""
                uid = s.get("uid") or ""
                if account:
                    break
    except Exception:
        pass
    return {"creds": creds, "label": name or account or "",
            "name": name, "uid": uid or account or ""}


def _place(creds: dict, *, inst_id: str, side: str, sz: int,
           ord_type: str, px: Optional[float], tag: str,
           td_mode: Optional[str] = None) -> dict:
    """OKX 下单统一入口。td_mode 缺省按 side 分流：sell（卖 put 开仓）→
    isolated 逐仓；buy → cash（纯买开仓）。平仓单必须显式传 CLOSE_TDMODE
    （跟随持仓保证金模式），cash 平 isolated 仓位会报 51000。"""
    if td_mode is None:
        td_mode = BUY_TDMODE if side == "buy" else SELL_TDMODE
    params = {
        "instId": inst_id,
        "tdMode": td_mode,
        "side": side,
        "ordType": ord_type,
        "sz": str(int(sz)),
        "tag": tag,
    }
    if ord_type not in ("limit", "post_only", "fok", "ioc"):
        raise OkxSdkError(
            f"期权不支持 ordType={ord_type}（OKX 50016，无纯市价单）——"
            "用 limit（px=盘口价立即成交）或 IOC（px=保底价扫单）")
    if px is None:
        raise OkxSdkError("该订单类型需提供 px")
    # 原样透传用户价格（BTC tick=1、SOL/XAU tick=0.1），合法性交给 OKX 校验
    params["px"] = str(px)
    data = okx_sdk.check(okx_sdk.trade_for(creds).set_order(**params))
    row = data[0] if isinstance(data, list) and data else {}
    s_code = row.get("sCode")
    if s_code not in (None, "", "0"):
        raise OkxSdkError(f"OKX {s_code} {row.get('sMsg', '')}".strip())
    return {"ord_id": row.get("ordId") or "", "cl_ord_id": row.get("clOrdId") or ""}


def poll_order(creds: dict, inst_id: str, ord_id: str) -> dict:
    """查询订单状态 → {state, avg_px, acc_fill_sz, fee, status}。"""
    rows = okx_sdk.check(okx_sdk.trade_for(creds).get_order(
        instId=inst_id, ordId=ord_id))
    r = rows[0] if isinstance(rows, list) and rows else {}
    st = r.get("state", "")
    status = {"live": "pending", "partially_filled": "pending",
              "filled": "filled", "canceled": "cancelled",
              "mmp_canceled": "cancelled"}.get(st, st or "unknown")
    return {
        "state": st,
        "status": status,
        "ord_id": ord_id,
        "avg_px": _f(r.get("avgPx")),
        "acc_fill_sz": _f(r.get("accFillSz")),
        "fee": _f(r.get("fee")),
    }


def cancel_order(account: str, *, inst_id: str, ord_id: str) -> dict:
    """撤销未成交委托（单步，撤单不产生资金流）。

    先 get_order 查状态：filled → 返回 status=filled（无法撤，提示刷新）；
    已 cancelled → 幂等（顺带把台账条目对齐）。live/partially_filled →
    set_cancel_order 撤销。撤单成功后按 ord_id 把对应台账条目置 cancelled
    （保留记录）。平仓单（close_put）撤销时对应 open_put 行保持 open——
    持仓未动、仍受到期监控。
    """
    a = _entry_account(account)
    creds = a["creds"]
    o = poll_order(creds, inst_id, ord_id)
    if o["status"] == "filled":
        return {"status": "filled", "message": "订单已成交，无需撤销",
                "ord_id": ord_id}
    if o["status"] == "cancelled":
        update_ledger(lambda x: x.get("ord_id") == ord_id, status="cancelled")
        return {"status": "cancelled", "message": "订单已是撤销状态",
                "ord_id": ord_id}
    rows = okx_sdk.check(okx_sdk.trade_for(creds).set_cancel_order(
        instId=inst_id, ordId=ord_id))
    row = rows[0] if isinstance(rows, list) and rows else {}
    s_code = row.get("sCode")
    if s_code not in (None, "", "0"):
        raise OkxSdkError(f"OKX {s_code} {row.get('sMsg', '')}".strip())
    update_ledger(lambda x: x.get("ord_id") == ord_id, status="cancelled",
                  cancel_ts=_utc_now())
    return {"status": "cancelled", "message": "已撤销", "ord_id": ord_id}


def pending_orders(account: str = "", inst_family: str = "") -> list[dict]:
    """子账号当前未成交委托（只读，含官方后台手动挂的单）。

    OKX /trade/orders-pending 要求 instId / instFamily / uly 至少一个——
    本实现按 instFamily 拉取该家族全部未成交委托；family 缺失时显式报错
    （不静默回退）。归一化输出 inst_id/ord_id/px/sz/side/ord_type/ts_ms。
    """
    if not inst_family:
        raise OkxSdkError("pending_orders 需 inst_family（OKX 不支持无 family 全量拉取）")
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.trade_for(a["creds"]).get_orders_pending(
        instType="OPTION", instFamily=inst_family))
    out = []
    for r in rows if isinstance(rows, list) else []:
        out.append({
            "inst_id": r.get("instId", ""),
            "ord_id": r.get("ordId", ""),
            "px": _f(r.get("px")),
            "sz": _f(r.get("sz")),
            "side": r.get("side", ""),
            "ord_type": r.get("ordType", ""),
            "td_mode": r.get("tdMode", ""),
            "ts_ms": int(r.get("cTime") or 0) or None,
        })
    out.sort(key=lambda x: x["ts_ms"] or 0)
    return out


def open_put(account: str, *, inst_id: str, sz: int,
             ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """卖 put 开仓（真实下单）。成功后写入台账（pending → 轮询 filled）。"""
    return _open_option(account=account, inst_id=inst_id, sz=sz,
                        ord_type=ord_type, px=px, kind="open_put")


def open_call(account: str, *, inst_id: str, sz: int,
              ord_type: str = "limit", px: Optional[float] = None,
              cost_basis: Optional[float] = None) -> dict:
    """卖 call（covered call）开仓——镜像 open_put（side=sell、isolated 逐仓）。

    保本门（§33.23，强制 fail-closed）：cost_basis = 被动持仓成本锚 C
    （接货价 K_put，put 台账 settled_itm max(strike) 自动带出、可改）。
    提供时要求 strike + px ≥ C（px 为 IOC 保底线 = 最差接受成交价，
    门成立则实际成交必成立）；不满足直接拒绝该轮，不硬卖。

    担保差异：call 上行无界，**不做全损现金担保**（_ensure_collateral 仅用于
    卖 put）——covered 语义 = 现货在手，被行权 = 现金结算赔付后现货市价卖出
    （等效按 K 出货）；isolated 仅平台 IM 冻结，极端暴涨接近强平时需主动
    买回平仓或补保证金（页面强平价可见）。
    """
    return _open_option(account=account, inst_id=inst_id, sz=sz,
                        ord_type=ord_type, px=px, kind="open_call",
                        cost_basis=cost_basis)


def _open_option(account: str, *, inst_id: str, sz: int,
                 ord_type: str = "limit", px: Optional[float] = None,
                 kind: str = "open_put",
                 cost_basis: Optional[float] = None) -> dict:
    """卖期权开仓公共路径：kind=open_put/open_call 决定合约类型断言与担保行为。"""
    a = _entry_account(account)
    spec = resolve_instrument(inst_id)
    want = "P" if kind == "open_put" else "C"
    if spec["opt_type"] != want:
        raise OkxSdkError(
            f"{inst_id} 不是 {'Put' if want == 'P' else 'Call'} 合约"
            f"（opt_type={spec['opt_type']}，请选 {'P' if want == 'P' else 'C'} 侧合约）")
    if int(sz) <= 0:
        raise OkxSdkError("张数 sz 必须为正整数")
    if kind == "open_call" and cost_basis is not None:
        guard = spec["strike"] + (px or 0.0)
        if guard < float(cost_basis) - 1e-9:
            raise OkxSdkError(
                f"保本门不通过（fail-closed，该轮跳过）：K({spec['strike']})"
                f" + px({px or 0}) = {guard:.4f} < 成本锚 C({float(cost_basis):.4f})。"
                "请抬高行权价、等待权利金回升，或上调成本锚后再卖 call。")
    # 开盘前快照盘口供参考（下单后立即轮询会很快，先落台账 pending）
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind=kind, account=account or a["label"], inst_id=inst_id,
        strike=spec["strike"], exp_ms=spec["exp_ms"], lot=spec["lot"],
        family=spec["inst_family"], side="sell", ord_type=ord_type,
        px=px,
        sz=int(sz), status="pending", ref_bid=q["bid"], ref_ask=q["ask"],
    )
    if kind == "open_call" and cost_basis is not None:
        update_ledger(lambda x: x["id"] == entry["id"], cost_basis=round(float(cost_basis), 6))
        entry["cost_basis"] = round(float(cost_basis), 6)
    try:
        res = _place(a["creds"], inst_id=inst_id, side="sell", sz=int(sz),
                     ord_type=ord_type, px=px, tag=TAG_OPEN)
        ord_id = res["ord_id"]
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=ord_id)
    entry["ord_id"] = ord_id
    return _settle_open_entry(a["creds"], entry, kind=kind)


def _settle_open_entry(creds: dict, entry: dict, kind: str = "open_put") -> dict:
    """短轮询（约 10×0.5s）等成交，回填 avg px/权利金/状态。

    filled → open 时：卖 put（kind=open_put）自动触发 _ensure_collateral
    （逐仓保证金追加至全损担保，走 A）；卖 call（open_call）**不做全损担保**
    （上行无界，covered=现货在手），仅记 margin_note。
    """
    if not entry.get("ord_id"):
        return entry
    for _ in range(10):
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            px = o["avg_px"] or entry.get("px") or 0.0
            filled_sz = o["acc_fill_sz"] or entry.get("sz") or 0
            upd = update_ledger(
                lambda x: x["id"] == entry["id"], status="open",
                filled_px=px, filled_sz=filled_sz, fee=o["fee"],
                premium_usd=round(px * entry["lot"] * filled_sz, 4),
            )
            if upd:
                if kind == "open_put":
                    upd = _ensure_collateral(creds, upd) or upd
                else:
                    upd = update_ledger(
                        lambda x: x["id"] == entry["id"],
                        collateral_usd=None, margin_added=0.0,
                        margin_note="covered（现货在手——call 上行无界不做全损"
                                    "现金担保；isolated 仅平台 IM 冻结，暴涨接近"
                                    "强平价时需主动买回平仓或补保证金）") or upd
            return upd or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry  # 仍 pending，等页面刷新再轮询


def _position_margin(creds: dict, inst_id: str) -> tuple[float, str]:
    """该逐仓期权仓当前保证金（USDC）。margin 字段为空/0 时回退 imr。"""
    rows = okx_sdk.check(okx_sdk.account_for(creds).get_positions(
        instType="OPTION", instId=inst_id))
    r = rows[0] if isinstance(rows, list) and rows else {}
    mgn = _f(r.get("margin"))
    if mgn <= 0:
        mgn = _f(r.get("imr"))
    return mgn, r.get("mgnMode") or ""


def _ensure_collateral(creds: dict, entry: dict) -> Optional[dict]:
    """逐仓现金担保（走 A）：成交后将仓位保证金追加至全损上限。

    背景：isolated 单笔默认仅冻结约 IM（~12% 名义，99-P 实测 1.26 vs 名义
    9.9），标的大幅波动浮亏可击穿单笔保证金 → 提前强平；账户旁的自留现金
    （隔离在外）救不了这笔。把保证金追加至 strike×面值×张数 × 担保比例
    （collateral_ratio_pct，WebUI 可配，默认 100 = 全损上限）后，浮亏上限
    < 保证金 → 数学上无强平路径，可扛到到期按结算了结
    （接货/现金结算），多笔之间仍逐仓隔离互不拖累。

    追加的保证金在平仓/到期后自动释放回账户余额。追加失败不阻断——仓位已
    成交保持 open，台账记 margin_note 供页面提醒补担保。
    """
    strike = _f(entry.get("strike"))
    lot = _f(entry.get("lot"))
    filled = _f(entry.get("filled_sz")) or _f(entry.get("sz")) or 0
    ratio = collateral_ratio_pct()
    if ratio <= 0:
        # 自动担保已关闭：仅平台默认 IM 冻结（浮亏可能击穿提前强平，语义自知）
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=None, margin_added=None,
            margin_note="自动担保已关闭（比例 0%，仅平台 IM 冻结）")
    target = round(strike * lot * filled * ratio / 100.0, 2)
    if target <= 0:
        return None
    cur, _mgn_mode = _position_margin(creds, entry["inst_id"])
    amt = round(target - cur, 2)
    if amt <= 0.01:
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=target, margin_added=0.0,
            margin_note="ok（已达标）")
    try:
        okx_sdk.check(okx_sdk.account_for(creds).set_margin_balance(
            instId=entry["inst_id"], posSide=POS_SIDE, type="add",
            amt=str(amt)))
    except Exception as e:  # noqa: BLE001 —— 追加失败不阻断开仓（仓位已成交）
        return update_ledger(
            lambda x: x["id"] == entry["id"],
            collateral_usd=target, margin_added=None,
            margin_note=f"追加失败（仓位已开，担保未到位）: {e}")
    return update_ledger(
        lambda x: x["id"] == entry["id"],
        collateral_usd=target, margin_added=amt,
        margin_note="ok（现金担保已追加）")


def close_put(account: str, *, inst_id: str, sz: int,
              ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """买回平仓卖 put（真实下单）。入口须先找到该 inst 的 open 台账行。"""
    return _close_option(account=account, inst_id=inst_id, sz=sz,
                         ord_type=ord_type, px=px,
                         open_kind="open_put", close_kind="close_put")


def close_call(account: str, *, inst_id: str, sz: int,
               ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """买回平仓卖 call（covered call 平仓，止盈/主动落袋）——镜像 close_put。

    止盈语义：卖 call 收权利金后，权利金回落（如 30%，控制参数待自动化批）
    或主动决策时买回平仓，pnl = (开仓价 − 买回价)×每张面值×张数。
    """
    return _close_option(account=account, inst_id=inst_id, sz=sz,
                         ord_type=ord_type, px=px,
                         open_kind="open_call", close_kind="close_call")


def _close_option(account: str, *, inst_id: str, sz: int,
                  ord_type: str = "limit", px: Optional[float] = None,
                  open_kind: str = "open_put",
                  close_kind: str = "close_put") -> dict:
    """买回平仓公共路径：open_kind/close_kind 决定台账行与记录类型。"""
    a = _entry_account(account)
    open_entry = find_entry(lambda x: (x.get("kind") == open_kind
                                       and x.get("inst_id") == inst_id
                                       and x.get("status") == "open"))
    if open_entry is None:
        label = "卖 put" if open_kind == "open_put" else "卖 call"
        raise OkxSdkError(f"台账无 {inst_id} 的 open {label} 记录（无法平仓）")
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind=close_kind, account=account or a["label"], inst_id=inst_id,
        strike=open_entry.get("strike"), exp_ms=open_entry.get("exp_ms"),
        lot=open_entry.get("lot"), family=open_entry.get("family"),
        side="buy", ord_type=ord_type, px=px,
        sz=int(sz), status="pending", open_id=open_entry["id"],
        ref_bid=q["bid"], ref_ask=q["ask"],
    )
    try:
        res = _place(a["creds"], inst_id=inst_id, side="buy", sz=int(sz),
                     ord_type=ord_type, px=px, tag=TAG_CLOSE,
                     td_mode=CLOSE_TDMODE)
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=res["ord_id"])
    entry["ord_id"] = res["ord_id"]
    return _settle_close_entry(a["creds"], entry, open_entry)


def _settle_close_entry(creds: dict, entry: dict, open_entry: dict) -> dict:
    for _ in range(10):
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            px = o["avg_px"] or entry.get("px") or 0.0
            lot = entry.get("lot") or 0.0
            open_px = open_entry.get("filled_px") or open_entry.get("px") or 0.0
            pnl = (open_px - px) * lot * int(entry.get("sz") or 0)
            update_ledger(lambda x: x["id"] == entry["id"], status="closed",
                          filled_px=px, filled_sz=o["acc_fill_sz"], fee=o["fee"],
                          pnl_usd=round(pnl, 4))
            update_ledger(lambda x: x["id"] == open_entry["id"], status="closed",
                          close_ts=entry["ts"], pnl_usd=round(pnl, 4))
            return update_ledger(lambda x: x["id"] == entry["id"]) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry


def spot_cover(account: str, *, spot_inst: str,
               base_qty: Optional[float] = None,
               quote_amt: Optional[float] = None) -> dict:
    """到期 ITM 现金结算后的现货补买（手动闭环持有现货）。

    spot_inst = OKX 现货交易对（如 BTC-USDC）；二选一指定数量：
    base_qty 按基础币数量（tgtCcy=base_ccy）／quote_amt 按计价币金额（默认）。
    市价单、cash、无杠杆；金额 ≤0 拒绝（fail-closed）。
    """
    a = _entry_account(account)
    if base_qty is not None:
        if base_qty <= 0:
            raise OkxSdkError("补买数量必须 > 0")
        sz, tgt = f"{base_qty:.8f}".rstrip("0").rstrip("."), "base_ccy"
    elif quote_amt is not None:
        if quote_amt <= 0:
            raise OkxSdkError("补买金额必须 > 0")
        sz, tgt = f"{quote_amt:.2f}", "quote_ccy"
    else:
        raise OkxSdkError("补买需指定 base_qty 或 quote_amt")
    entry = add_ledger(
        kind="spot_cover", account=account or a["label"], inst_id=spot_inst,
        spot_inst=spot_inst, side="buy", ord_type="market",
        sz=sz, tgt_ccy=tgt, status="pending",
    )
    params = {
        "instId": spot_inst, "tdMode": "cash", "side": "buy",
        "ordType": "market", "sz": sz, "tgtCcy": tgt, "tag": TAG_COVER,
    }
    is_usd_pair = spot_inst.endswith("-USD")
    params = {
        "instId": spot_inst, "side": "buy",
        "ordType": "market", "sz": sz, "tgtCcy": tgt, "tag": TAG_COVER,
        # 期权子账号为 acctLv=3（Multi-currency margin）模式：OKX 官方 Jupyter
        # 教程第 8 节——multi-currency/portfolio margin 模式下现货订单须
        # tdMode='cross'（cash 仅限 Spot / Spot-and-futures 模式，传 cash 报 51000）
        "tdMode": "cross",
    }
    api = okx_sdk.trade_for(a["creds"])
    try:
        if is_usd_pair:
            # Crypto-USD（官网 币种/USDⓢ，统一 USD 订单簿）：用 USDC 交易须
            # tradeQuoteCcy=USDC（changelog 迁移场景；FAQ：多稳定币自动内部换算）。
            # python-okx set_order 未封装 tradeQuoteCcy → 经 send_request 透传。
            # 9/30 后 instId 须切 Crypto-USDC（届时默认 USDC 结算、可不传 tradeQuoteCcy）。
            params["tradeQuoteCcy"] = "USDC"
            data = okx_sdk.check(api.send_request("/api/v5/trade/order", "POST", **params))
        else:
            data = okx_sdk.check(api.set_order(**params))
    except Exception as e:
        update_ledger(lambda x: x["id"] == entry["id"], status="failed",
                      note=f"下单失败: {e}")
        raise
    row = data[0] if isinstance(data, list) and data else {}
    if row.get("sCode") not in (None, "", "0"):
        raise OkxSdkError(f"OKX {row.get('sCode')} {row.get('sMsg', '')}".strip())
    ord_id = row.get("ordId") or ""
    update_ledger(lambda x: x["id"] == entry["id"], ord_id=ord_id)
    entry["ord_id"] = ord_id
    return _settle_cover_entry(a["creds"], entry)


def _settle_cover_entry(creds: dict, entry: dict) -> dict:
    for _ in range(10):
        if not entry.get("ord_id"):
            return entry
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            return update_ledger(
                lambda x: x["id"] == entry["id"], status="filled",
                filled_px=o["avg_px"], filled_sz=o["acc_fill_sz"], fee=o["fee"],
            ) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry


# ── 持仓 / 到期监控（只读）──────────────────────────────────

def account_config(account: str = "") -> dict:
    """子账号账户配置（只读）：期权开通/保证金模式/结算币种/权限。

    v5 /account/config → data[0]：opAuth(0 未开通/1 已开通)、acctLv(3=跨币种)、
    posMode(net_mode/long_short_mode)、settleCcy、perm。
    """
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_config())
    d = rows[0] if isinstance(rows, list) and rows else {}
    return {
        "uid": str(d.get("uid") or a["uid"] or ""),
        "acct_lv": str(d.get("acctLv") or ""),
        "pos_mode": str(d.get("posMode") or ""),
        "op_auth": int(d.get("opAuth") or 0),
        "settle_ccy": str(d.get("settleCcy") or ""),
        "perm": str(d.get("perm") or ""),
    }


def account_balance(account: str = "") -> dict:
    """子账号资产（只读）：details 按币种 + 总权益 totalEq(USD)。

    字段（v5 account/balance）：ccy/cashBal/availBal/frozenBal/eq/eqUsd。
    cashBal=现金（含挂单冻结）、availBal=可下新单、frozenBal=冻结（挂单/保证金占用）、
    eq=币种总权益。期权卖方现金担保占用反映在 availBal 减少。
    """
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_balance())
    d = rows[0] if isinstance(rows, list) and rows else {}
    details = d.get("details") or []
    out = []
    for r in details if isinstance(details, list) else []:
        ccy = r.get("ccy", "")
        eq = _f(r.get("eq"))
        if eq > 0 or _f(r.get("cashBal")) > 0:
            out.append({
                "ccy": ccy,
                "cash_bal": _f(r.get("cashBal")),
                "avail_bal": _f(r.get("availBal")),
                "frozen_bal": _f(r.get("frozenBal")),
                "eq": eq,
                "eq_usd": _f(r.get("eqUsd")),
                "update_ms": int(r.get("uTime") or 0),
            })
    out.sort(key=lambda x: x["eq_usd"], reverse=True)
    return {"total_eq_usd": _f(d.get("totalEq")), "details": out,
            "account": a["name"] or a["label"], "account_uid": a["uid"]}


def open_puts(account: str = "") -> list[dict]:
    """OKX 当前期权净仓（只读）。无凭证/无仓位 → []；失败抛错由调用方处理。"""
    a = _entry_account(account)
    rows = okx_sdk.check(
        okx_sdk.account_for(a["creds"]).get_positions(instType="OPTION"))
    out = []
    for r in rows if isinstance(rows, list) else []:
        if r.get("pos") is None or _f(r.get("pos")) == 0:
            continue
        out.append(_normalize_position(r))
    return out


def _normalize_position(r: dict) -> dict:
    inst = r.get("instId", "")
    ps = r.get("posSide")
    if ps in ("long", "short"):
        side = ps
    else:
        # net_mode（跨币种保证金）下 posSide=net，空头由 pos 符号表达
        side = "short" if _f(r.get("pos")) < 0 else "long"
    return {
        "inst_id": inst,
        "side": side,
        "pos": abs(_f(r.get("pos"))),
        "avg_px": _f(r.get("avgPx")),
        "mark_px": _f(r.get("markPx")),
        "upl": _f(r.get("upl")),
        "upl_ratio": _f(r.get("uplRatio")),
        "mgn_mode": r.get("mgnMode", ""),
        "lever": r.get("lever", ""),
        # 逐仓实际冻结保证金（margin 0/空时回退 imr）；足额担保时 OKX 无强平价（--）
        "margin_usd": round(_f(r.get("margin")) or _f(r.get("imr")), 2),
        "liq_px": _norm_liq(r.get("liqPx")),
        "exp_ms": _parse_exp(inst),
        "strike": _parse_strike(inst),
    }


def _parse_strike(inst_id: str) -> Optional[float]:
    parts = inst_id.split("-")
    if len(parts) >= 4:
        try:
            return float(parts[-2])
        except ValueError:
            return None
    return None


def _parse_exp(inst_id: str) -> Optional[int]:
    # instId 格式：FAMILY-QUOTE[_UM]-YYMMDD-STRIKE-C/P（如 SOL-USD_UM-260905-99-P）
    # 日期段是倒数第 3 段（从右数：type、strike、date）——不能用固定 index，
    # family/quote 可能含连字符与 _UM 后缀导致错位（曾取 parts[1] 把
    # USD_UM 当日期解析失败 → 1970-01-01）。
    parts = inst_id.split("-")
    if len(parts) >= 4:
        dseg = parts[-3]
        try:
            y, m, d = 2000 + int(dseg[:2]), int(dseg[2:4]), int(dseg[4:6])
            return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None
    return None


def expiry_reminder(account: str = "", hours: int = 72) -> list[dict]:
    """台账中 72h 内到期 / 已到期未确认的 open put 提醒。"""
    now_ms = int(time.time() * 1000)
    out = []
    for e in load_ledger():
        if e.get("kind") != "open_put" or e.get("status") != "open":
            continue
        exp = int(e.get("exp_ms") or 0)
        if exp and exp - now_ms <= hours * 3600_000:
            out.append({
                "id": e["id"], "inst_id": e["inst_id"], "account": e.get("account"),
                "strike": e.get("strike"), "exp_ms": exp, "sz": e.get("sz"),
                "premium_usd": e.get("premium_usd"), "lot": e.get("lot"),
                "expired": exp <= now_ms,
            })
    return out



# ── 到期结算判定（C23，2026-09-07）─────────────────────────

# OKX 账单 type/subType 官方枚举（python-okx Account.get_bills docstring 权威表；
# 用户 OKX 后台中文样本：101-P「账单主类型=交割 / 子类型=到期作废」）。对卖 put：
#   type=3 交割 + subType=172 到期作废   → OTM（结算价 ≥ strike，put 无价值，保证金释放）
#   type=3 交割 + subType=171 到期被行权 → ITM（结算价 < strike，现金赔付）
_BILL_TYPE_DELIVERY = "3"
_BILL_SUBTYPE_WORTHLESS = "172"   # 到期作废（OTM）
_BILL_SUBTYPE_EXERCISED = "171"   # 到期被行权（ITM）

STATUS_SETTLED_OTM = "settled_otm"
STATUS_SETTLED_ITM = "settled_itm"
STATUS_SETTLED_REVIEW = "settled_review"


def settle_expired_puts(now_ms=None) -> list:
    """台账已到期仍 open 的 put → 查 OKX 交割账单判定 作废/被行权 → 更新台账。

    判定信号（不做单信号赌博）——每笔结算行交叉校验：
      subType=172（到期作废）且 px(结算价) ≥ strike → settled_otm
      subType=171（到期被行权）且 px < strike        → settled_itm
      subType 与 px 矛盾 / 其他交割 subType（170 等） → settled_review（fail-closed）
      账单未出（OKX 结算后 ~27s 出现）/ 查不到         → 保持 open，下轮重试
    返回本次判定列表 [{id, inst_id, status, settle_px, settle_pnl, note}]。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    settled = []
    for e in load_ledger():
        if e.get("kind") != "open_put" or e.get("status") != "open":
            continue
        exp_ms = int(e.get("exp_ms") or 0)
        if not exp_ms or exp_ms > now_ms:
            continue  # 未到期
        inst_id = e.get("inst_id") or ""
        row = _find_delivery_bill(e.get("account") or "", inst_id, exp_ms, now_ms)
        if row is None:
            continue  # 账单未出/延迟 → 保持 open，下轮重试
        sub = row.get("subType")
        px = _f(row.get("px"))          # 账单「成交价」= OKX 结算价（101-P: 105.0）
        pnl = _f(row.get("pnl"))
        strike = float(e.get("strike") or 0)
        result = _classify_settlement(sub, px, strike)
        fields = {
            "status": result["status"],
            "settle_subtype": sub,
            "settle_px": px,
            "settle_pnl": round(pnl, 6),
            "settle_ts": _utc_now(),
        }
        # ITM：毛赔付 = (行权价−结算价)×面值×张数（净盈亏 settle_pnl 已含权利金收入）
        if result["status"] == STATUS_SETTLED_ITM:
            base = (inst_id or "").split("-")[0]
            lot = FAMILY_LOT.get(base)
            sz = int(e.get("sz") or 0)
            fields["settle_payout"] = (round((strike - px) * lot * sz, 6)
                                        if lot and sz and px is not None else None)
        if result["status"] == STATUS_SETTLED_REVIEW:
            fields["note"] = result["note"]
        if update_ledger(lambda x: x["id"] == e["id"], **fields) is not None:
            settled.append({"id": e["id"], "inst_id": inst_id,
                            "status": result["status"], "settle_px": px,
                            "settle_pnl": round(pnl, 6),
                            "settle_payout": fields.get("settle_payout"),
                            "note": result.get("note", "")})
    return settled


def _find_delivery_bill(account: str, inst_id: str, begin_ms: int,
                        end_ms: int):
    """查该 put 在 [expiry, now] 窗口的交割账单行（type=3，按 instId 匹配）。"""
    a = _entry_account(account)
    rows = okx_sdk.check(okx_sdk.account_for(a["creds"]).get_bills(
        instType="OPTION", type=_BILL_TYPE_DELIVERY,
        begin=str(begin_ms), end=str(end_ms), limit="100"))
    hits = [r for r in (rows if isinstance(rows, list) else [])
            if r.get("instId") == inst_id
            and r.get("type") == _BILL_TYPE_DELIVERY]
    return hits[0] if hits else None


def _classify_settlement(sub: str, px: float, strike: float) -> dict:
    """账单行 subType + 结算价交叉判定 OTM/ITM（矛盾 → review，不猜）。"""
    if sub == _BILL_SUBTYPE_WORTHLESS:            # 172 到期作废
        if px >= strike:
            return {"status": STATUS_SETTLED_OTM}
        return {"status": STATUS_SETTLED_REVIEW,
                "note": "作废行但结算价 {} < strike {}，账单存疑".format(px, strike)}
    if sub == _BILL_SUBTYPE_EXERCISED:            # 171 到期被行权
        if px < strike:
            return {"status": STATUS_SETTLED_ITM}
        return {"status": STATUS_SETTLED_REVIEW,
                "note": "被行权行但结算价 {} ≥ strike {}，账单存疑".format(px, strike)}
    return {"status": STATUS_SETTLED_REVIEW,
            "note": "交割账单未知 subType={}，请人工核对".format(sub)}

# ── 两步确认（内存 pending action，30s TTL）─────────────────

_pending: dict[str, dict] = {}


def stage_action(action: str, payload: dict) -> tuple[str, dict]:
    """生成一次性 tx_id（30s 过期）。同一动作并发/重放由 tx_id 一次性防住。"""
    tx_id = secrets.token_urlsafe(12)
    _pending[tx_id] = {"action": action, "payload": payload, "ts": time.time()}
    _gc_pending()
    return tx_id, {"tx_id": tx_id, "expires_in": int(_TTL), "payload": payload}


def take_action(tx_id: str) -> dict:
    """校验并取走 pending action（一次性；过期/不存在抛错）。"""
    p = _pending.pop(tx_id, None)
    if p is None:
        raise OkxSdkError("确认令牌无效或已过期（30 秒内需确认）")
    if time.time() - p["ts"] > _TTL:
        raise OkxSdkError("确认令牌已过期（30 秒），请重新发起")
    return p


def _gc_pending() -> None:
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["ts"] > _TTL]:
        _pending.pop(k, None)


def _f(v) -> float:
    if v in (None, ""):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _norm_liq(v) -> Optional[float]:
    """强平价 liqPx 归一化：空 / "--" / 非数字 → None（OKX 足额担保时无强平价）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None
def manual_close_entry(entry_id: str, note: str = "") -> Optional[dict]:
    """台账手动关账（单步、无资金流）：把 open 卖 put 行标 closed_manual。

    用途：OKX 官方后台手动平仓等系统外操作收尾——仓位在交易所已消失，
    但台账状态机只认本系统成交路径，open 行会变 phantom（settle 只处理
    已到期行、该仓永远查不到交割账单）。手动关账只做台账标记留痕，不查
    OKX、不回填平仓价/盈亏（外部成交不在本系统内）。
    仅 kind=open_put 且 status=open 可关；已 settled/closed 行拒绝（幂等）。
    """
    default_note = "官方后台平仓（系统外操作），手动关账"
    e = update_ledger(
        lambda x: x.get("kind") == "open_put" and x.get("status") == "open"
                  and x.get("id") == entry_id,
        status="closed_manual",
        close_ts=_utc_now(),
        note=note or default_note)
    return e


def reopen_entry(entry_id: str) -> Optional[dict]:
    """撤销手动关账：closed_manual → open（误关账恢复，交回到期巡检/settle 管辖）。

    仅 kind=open_put 且 status=closed_manual 可撤销；settled/closed 是资金流终态
    （到期结算/买回平仓后仓位已了结），不可逆。恢复后保留原 note 并追加撤销标记，
    close_ts 残留无碍（open 行渲染不显示）。
    """
    cur = find_entry(lambda x: x.get("kind") == "open_put"
                     and x.get("status") == "closed_manual" and x.get("id") == entry_id)
    if cur is None:
        return None
    note = (cur.get("note") or "") + " | 已撤销手动关账（回到 open）"
    return update_ledger(lambda x: x.get("id") == entry_id
                         and x.get("status") == "closed_manual",
                         status="open", note=note)


# ── instrument / 盘口辅助 ──────────────────────────────────────
