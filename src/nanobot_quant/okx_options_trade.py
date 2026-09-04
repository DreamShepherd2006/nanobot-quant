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
#: 买方(side=buy，买回平仓)=cash（付全权利金，无杠杆）；
#: 卖方(side=sell，卖 put)=isolated 逐仓——TD 低9 多笔卖 put 在同一
#: 子账号并存，逐仓给每张独立保证金与强平边界（亏穿只平该张，不拖累
#: 同账号其他仓位），无需每笔开子账号。等效现金担保 = 账户自留
#: ≥ Σ(strike×面值) 现金（不设杠杆，平台不冻结全额，见页面提示）。
SELL_TDMODE = "isolated"
BUY_TDMODE = "cash"
#: 跨币种保证金（acctLv=3 net_mode）下逐仓期权仓的 posSide 标识为 net
POS_SIDE = "net"
#: 平仓/减仓单的 tdMode 必须与持仓保证金模式一致（OKX 规则：不匹配 →
#: 51000 Parameter tdMode error）。本仓开在 isolated（SELL_TDMODE），
#: 买回平仓同样 isolated；cash 仅用于无平仓对象的纯买入开仓。
CLOSE_TDMODE = "isolated"
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


def ticker_quote(inst_id: str) -> dict:
    """当前盘口/最新（bidPx/askPx/last —— USD/1 名义币）。"""
    rows = okx_sdk.check(okx_sdk.market().get_ticker(instId=inst_id))
    r = rows[0] if rows else {}
    return {
        "bid": _f(r.get("bidPx")),
        "ask": _f(r.get("askPx")),
        "last": _f(r.get("last")),
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


def open_put(account: str, *, inst_id: str, sz: int,
             ord_type: str = "limit", px: Optional[float] = None) -> dict:
    """卖 put 开仓（真实下单）。成功后写入台账（pending → 轮询 filled）。"""
    a = _entry_account(account)
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "P":
        raise OkxSdkError(f"{inst_id} 不是 Put 合约")
    if int(sz) <= 0:
        raise OkxSdkError("张数 sz 必须为正整数")
    # 开盘前快照盘口供参考（下单后立即轮询会很快，先落台账 pending）
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind="open_put", account=account or a["label"], inst_id=inst_id,
        strike=spec["strike"], exp_ms=spec["exp_ms"], lot=spec["lot"],
        family=spec["inst_family"], side="sell", ord_type=ord_type,
        px=px,
        sz=int(sz), status="pending", ref_bid=q["bid"], ref_ask=q["ask"],
    )
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
    return _settle_open_entry(a["creds"], entry)


def _settle_open_entry(creds: dict, entry: dict) -> dict:
    """短轮询（约 10×0.5s）等成交，回填 avg px/权利金/状态。

    filled → open 时自动触发 _ensure_collateral：把逐仓保证金追加至
    全损担保（走 A——见 _ensure_collateral 文档）。
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
                upd = _ensure_collateral(creds, upd) or upd
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
    """买回平仓（真实下单）。入口须先找到该 inst 的 open 台账行。"""
    a = _entry_account(account)
    open_entry = find_entry(lambda x: (x.get("kind") == "open_put"
                                       and x.get("inst_id") == inst_id
                                       and x.get("status") == "open"))
    if open_entry is None:
        raise OkxSdkError(f"台账无 {inst_id} 的 open 卖 put 记录（无法平仓）")
    q = ticker_quote(inst_id)
    entry = add_ledger(
        kind="close_put", account=account or a["label"], inst_id=inst_id,
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
    try:
        data = okx_sdk.check(okx_sdk.trade_for(a["creds"]).set_order(**params))
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
