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
- 卖 put 现金担保（简化口径）= strike × lot × sz；正式保证金/冻结以 OKX 计算为准
  （跨币种保证金模式 acctLv=3，settleCcy=USDC；期权下单 tdMode=cash 现金全额担保，
  与「100% 现金担保、零杠杆」铁律一致）
- 到期结算：欧式、现金结算（settle = 到期日 08:00 UTC 后 30 分钟 TWAP，官方口径）；
  OKX 到期自动结算入账，本模块不重复算钱，到期后经台账标 ``settled`` 并引导核对账单
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import okx_sdk
from .okx_sdk import OkxSdkError

#: 期权下单保证金模式：cash = 非杠杆现金全额担保（与交易铁律一致）。
DEFAULT_TDMODE = "cash"
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

def resolve_instrument(inst_id: str) -> dict:
    """单只期权合约规格（instId → stk/expTime/ctVal/ctMult/lot/optType/uly）。"""
    rows = okx_sdk.check(okx_sdk.public().get_instruments(
        instType="OPTION", instId=inst_id))
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

    输出含：合约规格、参考盘口、订单参数（side=sell tdMode=cash）、
    预计权利金 = px×lot×sz（limit）或 ask×lot×sz（market 参考）、
    现金担保（简化口径）与提示。
    """
    spec = resolve_instrument(inst_id)
    if spec["opt_type"] != "P":
        raise OkxSdkError(f"{inst_id} 不是 Put 合约（{spec['opt_type']}）")
    lot = spec["lot"]
    if lot <= 0:
        raise OkxSdkError(f"{inst_id} 面值解析失败")
    q = ticker_quote(inst_id)
    ord_type = (ord_type or "limit").lower()
    if ord_type not in ("limit", "market", "post_only", "fok", "ioc"):
        raise OkxSdkError(f"不支持的订单类型 {ord_type}（limit/market/post_only/fok/ioc）")
    if ord_type == "limit" and px is None:
        raise OkxSdkError("限价单需提供价格 px")
    ref_px = px if ord_type == "limit" else (q["ask"] or q["last"] or 0.0)
    prem = ref_px * lot * sz
    collat = spec["strike"] * lot * sz
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
        "td_mode": DEFAULT_TDMODE,
        "est_premium_usd": round(prem, 4),
        "collateral_est_usd": round(collat, 2),
        "note": ("U 本位线性：盘口=USD/1 名义币，权利金(USD)=px×每张面值；"
                 "现金担保为简化口径 strike×面值×张数，正式冻结以 OKX 为准；"
                 "欧式现金结算，不可提前行权。"),
    }


def _exp_str(exp_ms: int) -> str:
    try:
        return datetime.fromtimestamp(exp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"


# ── 下单（真实操作，两步确认由 handler 层执行）──────────────

def _entry_account(account: str) -> dict:
    """account = 子账号 name 或 uid。"""
    from .okx_cex_credentials import get_okx_cex_credentials

    creds = get_okx_cex_credentials(account=account or None)
    return {"creds": creds, "label": account or creds.get("name", "")}


def _place(creds: dict, *, inst_id: str, side: str, sz: int,
           ord_type: str, px: Optional[float], tag: str) -> dict:
    """OKX 下单统一入口（期权 tdMode=cash）。side: sell=卖 put / buy=买回。"""
    params = {
        "instId": inst_id,
        "tdMode": DEFAULT_TDMODE,
        "side": side,
        "ordType": ord_type,
        "sz": str(int(sz)),
        "tag": tag,
    }
    if ord_type != "market":
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
        px=px if ord_type == "limit" else (q["ask"] or 0.0),
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
    """短轮询（约 10×0.5s）等成交，回填 avg px/权利金/状态。"""
    if not entry.get("ord_id"):
        return entry
    for _ in range(10):
        o = poll_order(creds, entry["inst_id"], entry["ord_id"])
        if o["status"] == "filled":
            px = o["avg_px"] or entry.get("px") or 0.0
            filled_sz = o["acc_fill_sz"] or entry.get("sz") or 0
            return update_ledger(
                lambda x: x["id"] == entry["id"], status="open",
                filled_px=px, filled_sz=filled_sz, fee=o["fee"],
                premium_usd=round(px * entry["lot"] * filled_sz, 4),
            ) or entry
        if o["status"] == "cancelled":
            return update_ledger(lambda x: x["id"] == entry["id"],
                                 status="cancelled") or entry
        time.sleep(0.5)
    return entry  # 仍 pending，等页面刷新再轮询


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
        side="buy", ord_type=ord_type, px=px if ord_type == "limit" else (q["ask"] or 0.0),
        sz=int(sz), status="pending", open_id=open_entry["id"],
        ref_bid=q["bid"], ref_ask=q["ask"],
    )
    try:
        res = _place(a["creds"], inst_id=inst_id, side="buy", sz=int(sz),
                     ord_type=ord_type, px=px, tag=TAG_CLOSE)
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
    name = a["creds"].get("name") or a["label"] or ""
    return {"total_eq_usd": _f(d.get("totalEq")), "details": out,
            "account": name, "account_uid": str(a["creds"].get("uid") or account)}


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
    return {
        "inst_id": inst,
        "side": r.get("posSide") or ("short" if _f(r.get("pos")) < 0 else "long"),
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
    parts = inst_id.split("-")
    if len(parts) >= 3:
        try:
            y, m, d = 2000 + int(parts[1][:2]), int(parts[1][2:4]), int(parts[1][4:6])
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
