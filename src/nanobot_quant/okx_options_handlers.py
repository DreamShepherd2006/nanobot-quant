"""OKX 期权链 WebUI page (Commander only) — 期权线批次 B/C。

只读通道（批次 B）：
GET /config/okx-options           — 页面（标的/到期/现货 HV/期权链定价表）
GET /config/okx-options/expiries  — 某 family 全部未到期列表（JSON）
GET /config/okx-options/chain     — 链数据 JSON（family/expiries/hv_days/range）

卖 put 执行通道（批次 C，页面两步确认后才真实下单）：
GET  /config/okx-options/accounts   — 已配置子账户（下单目标）
GET  /config/okx-options/positions  — OKX 期权持仓 + 台账 open 行（只读）
GET  /config/okx-options/ledger     — 完整台账
GET  /config/okx-options/reminder   — 到期提醒（72h 内/已到期）
POST /config/okx-options/preview    — 卖 put 订单预览（纯计算，不下单）
POST /config/okx-options/sell/start|confirm   — 卖 put 两步确认（真实下单）
POST /config/okx-options/close/start|confirm  — 买回平仓两步确认
POST /config/okx-options/cover/start|confirm  — 到期 ITM 现货补买两步确认

数据/下单全部经官方 python-okx SDK（okx_sdk 唯一 import 点）；
金额/张数由后端校验，确认令牌 30s 一次性（与钱包转账同模式）。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nanobot_quant import okx_options_data as od
from nanobot_quant import okx_options_trade as ot
from nanobot_quant.okx_cex_credentials import list_sub_accounts
from nanobot_quant.okx_sdk import OkxSdkError

_HERE = os.path.dirname(os.path.abspath(__file__))

_PAGE_HTML: str = ""

_TX_TTL = 30
_pending_tx: dict[str, dict] = {}


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    _u = request.session.get("user")
    if not _u:
        return "请先登录", False
    if not gatekeeper._platform.is_commander(_u):
        return "仅 Commander 可访问", False
    return None, True


def _deny(err: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": err},
                        status_code=403 if "Commander" in err else 401)


def _cleanup() -> None:
    now = time.time()
    for k in [k for k, v in _pending_tx.items() if now - v["ts"] > _TX_TTL]:
        _pending_tx.pop(k, None)


def _stage(action: str, payload: dict) -> dict:
    _cleanup()
    tx_id = secrets.token_urlsafe(12)
    _pending_tx[tx_id] = {"action": action, "payload": payload, "ts": time.time()}
    return {"tx_id": tx_id, "expires_in": _TX_TTL, "payload": payload}


def _consume(body: dict) -> tuple[dict | None, str | None]:
    """取走 pending 动作（一次性；不存在/过期报错）。"""
    tx_id = (body or {}).get("tx_id") or (body or {}).get("txId")
    if not tx_id:
        return None, "缺少 tx_id"
    p = _pending_tx.pop(tx_id, None)
    if p is None or time.time() - p["ts"] > _TX_TTL:
        return None, "确认令牌无效或已过期（30 秒），请重新发起"
    return p, None


def _num(body: dict, key: str, default=None):
    try:
        v = body.get(key, default)
        if v in (None, ""):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


async def _json_body(request: Request) -> tuple[dict | None, str | None]:
    try:
        return await request.json(), None
    except Exception:
        return None, "无效的 JSON 数据"


def register_okx_options_routes(app, gatekeeper) -> None:
    """Register OKX options chain page routes on the FastAPI app.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """
    global _PAGE_HTML
    if not _PAGE_HTML:
        _PAGE_HTML = _load_template("okx_options_page.html")

    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(
                f"<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 {err}</h3>",
                status_code=403 if "Commander" in err else 401,
            )
        return HTMLResponse(_PAGE_HTML, status_code=200)

    async def _expiries(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        family = (request.query_params.get("family") or "BTC-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse({"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            exps = await asyncio.to_thread(od.list_expiries, family)
        except OkxSdkError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "expiries": exps})

    async def _chain(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        family = (q.get("family") or "BTC-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse({"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            hv_days = int(q.get("hv_days") or 30)
        except (TypeError, ValueError):
            hv_days = 30
        try:
            rng = q.get("range")
            spot_pct_range = float(rng) if rng else 20.0
        except (TypeError, ValueError):
            spot_pct_range = 20.0
        exp_raw = q.get("expiries")
        expiries = None
        if exp_raw:
            try:
                expiries = [int(x) for x in exp_raw.split(",") if x]
            except ValueError:
                return JSONResponse({"ok": False, "error": "expiries 参数非法"})
        try:
            chain = await asyncio.to_thread(
                od.fetch_chain, family, expiries=expiries,
                spot_pct_range=spot_pct_range, hv_days=hv_days)
        except OkxSdkError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "data": chain})

    # ── 批次 C：账户 / 持仓 / 台账 / 提醒（只读）────────────────

    async def _ticker(request: Request):
        # 单合约实时盘口（平仓/卖 put 弹窗 px 预填，免先刷新期权链）
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        inst = (request.query_params.get("inst_id") or "").strip().upper()
        if not inst:
            return JSONResponse({"ok": False, "error": "inst_id 必填"})
        try:
            data = await asyncio.to_thread(od.get_ticker_bid_ask, inst)
        except OkxSdkError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "data": data})

    async def _accounts(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        return JSONResponse({"ok": True, "accounts": list_sub_accounts()})

    async def _positions(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        account = request.query_params.get("account") or ""
        try:
            puts = await asyncio.to_thread(ot.open_puts, account)
            bal = await asyncio.to_thread(ot.account_balance, account)
            cfg = await asyncio.to_thread(ot.account_config, account)
            open_rows = [e for e in ot.load_ledger()
                         if e.get("kind") == "open_put"
                         and e.get("status") in ("open", "pending")]
            return JSONResponse({"ok": True, "positions": puts,
                                 "balance": bal, "config": cfg,
                                 "ledger_open": open_rows})
        except (OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _ledger(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        rows = ot.load_ledger()
        rows.reverse()
        return JSONResponse({"ok": True, "ledger": rows})

    async def _reminder(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        return JSONResponse({"ok": True, "reminders": ot.expiry_reminder()})

    # ── 撤单 / 当前委托（单步，撤单无资金流）────────────

    async def _pending(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        account = request.query_params.get("account") or ""
        inst_family = request.query_params.get("inst_family") or ""
        try:
            rows = await asyncio.to_thread(
                ot.pending_orders, account, inst_family)
            return JSONResponse({"ok": True, "pending": rows})
        except (okx_sdk.OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _cancel(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        inst_id = (body or {}).get("inst_id") or ""
        ord_id = (body or {}).get("ord_id") or ""
        account = (body or {}).get("account") or ""
        if not inst_id or not ord_id:
            return JSONResponse({"ok": False, "error": "缺少 inst_id / ord_id"})
        try:
            res = await asyncio.to_thread(
                ot.cancel_order, account, inst_id=inst_id, ord_id=ord_id)
            return JSONResponse({"ok": True, **res})
        except (okx_sdk.OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    # ── 担保设置（逐仓自动追加比例，option_params.json）────────

    async def _params_get(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        return JSONResponse({"ok": True, "params": ot.load_option_params()})

    async def _params_save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        raw = body.get("collateral_ratio_pct")
        try:
            ratio = int(raw)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "担保比例必须为整数（0–200）"})
        if not 0 <= ratio <= 200:
            return JSONResponse({"ok": False, "error": "担保比例须在 0–200 之间"})
        return JSONResponse({"ok": True, "params": ot.save_option_params(
            collateral_ratio_pct=ratio)})

    # ── 批次 C：下单（预览 → start → confirm 两步确认）─────────

    async def _preview(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        inst_id = (body.get("inst_id") or "").strip().upper()
        sz = int(_num(body, "sz", 0) or 0)
        ord_type = (body.get("ord_type") or "limit").lower()
        px = _num(body, "px")
        try:
            out = await asyncio.to_thread(ot.preview_open_put, inst_id, sz, ord_type, px)
            return JSONResponse(out)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _sell_start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        account = (body.get("account") or "").strip()
        inst_id = (body.get("inst_id") or "").strip().upper()
        sz = int(_num(body, "sz", 0) or 0)
        ord_type = (body.get("ord_type") or "limit").lower()
        px = _num(body, "px")
        if sz <= 0:
            return JSONResponse({"ok": False, "error": "张数必须为正整数"})
        try:
            prev = await asyncio.to_thread(ot.preview_open_put, inst_id, sz, ord_type, px)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "stage": _stage("sell", {
            "account": account, "inst_id": inst_id, "sz": sz,
            "ord_type": ord_type, "px": px if ord_type != "market" else None,
            "preview": prev})})

    async def _sell_confirm(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        act, perr = _consume(body)
        if perr:
            return JSONResponse({"ok": False, "error": perr})
        if act["action"] != "sell":
            return JSONResponse({"ok": False, "error": "动作类型不匹配，请重新发起"})
        p = act["payload"]
        try:
            res = await asyncio.to_thread(
                ot.open_put, p["account"], inst_id=p["inst_id"], sz=p["sz"],
                ord_type=p["ord_type"], px=p.get("px"))
            return JSONResponse({"ok": True, "entry": res})
        except (OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _close_start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        account = (body.get("account") or "").strip()
        inst_id = (body.get("inst_id") or "").strip().upper()
        sz = int(_num(body, "sz", 0) or 0)
        ord_type = (body.get("ord_type") or "limit").lower()
        px = _num(body, "px")
        if sz <= 0:
            return JSONResponse({"ok": False, "error": "张数必须为正整数"})
        prev = {"inst_id": inst_id, "sz": sz, "ord_type": ord_type, "px": px,
                "note": "买回平仓：开仓已收权利金，买回支付权利金；"
                        "净盈亏 = (开仓价 − 买回价) × 每张面值 × 张数。"}
        return JSONResponse({"ok": True, "stage": _stage("close", {
            "account": account, "inst_id": inst_id, "sz": sz,
            "ord_type": ord_type, "px": px if ord_type != "market" else None,
            "preview": prev})})

    async def _close_confirm(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        act, perr = _consume(body)
        if perr:
            return JSONResponse({"ok": False, "error": perr})
        if act["action"] != "close":
            return JSONResponse({"ok": False, "error": "动作类型不匹配，请重新发起"})
        p = act["payload"]
        try:
            res = await asyncio.to_thread(
                ot.close_put, p["account"], inst_id=p["inst_id"], sz=p["sz"],
                ord_type=p["ord_type"], px=p.get("px"))
            return JSONResponse({"ok": True, "entry": res})
        except (OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _cover_start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        account = (body.get("account") or "").strip()
        spot_inst = (body.get("spot_inst") or "").strip().upper()
        base_qty = _num(body, "base_qty")
        quote_amt = _num(body, "quote_amt")
        if not spot_inst:
            return JSONResponse({"ok": False, "error": "缺少 spot_inst（现货交易对，如 BTC-USDC）"})
        if (base_qty is None or base_qty <= 0) and (quote_amt is None or quote_amt <= 0):
            return JSONResponse({"ok": False, "error": "需指定 base_qty 或 quote_amt"})
        prev = {"spot_inst": spot_inst, "base_qty": base_qty, "quote_amt": quote_amt,
                "note": "到期 ITM 现金结算后的现货补买（市价单、cash 无杠杆）——补买后现货归自己持有。"}
        return JSONResponse({"ok": True, "stage": _stage("cover", {
            "account": account, "spot_inst": spot_inst,
            "base_qty": base_qty, "quote_amt": quote_amt, "preview": prev})})

    async def _cover_confirm(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        act, perr = _consume(body)
        if perr:
            return JSONResponse({"ok": False, "error": perr})
        if act["action"] != "cover":
            return JSONResponse({"ok": False, "error": "动作类型不匹配，请重新发起"})
        p = act["payload"]
        try:
            res = await asyncio.to_thread(
                ot.spot_cover, p["account"], spot_inst=p["spot_inst"],
                base_qty=p.get("base_qty"), quote_amt=p.get("quote_amt"))
            return JSONResponse({"ok": True, "entry": res})
        except (OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    app.add_api_route("/config/okx-options", _page, methods=["GET"])
    app.add_api_route("/config/okx-options/expiries", _expiries, methods=["GET"])
    app.add_api_route("/config/okx-options/chain", _chain, methods=["GET"])
    app.add_api_route("/config/okx-options/ticker", _ticker, methods=["GET"])
    app.add_api_route("/config/okx-options/accounts", _accounts, methods=["GET"])
    app.add_api_route("/config/okx-options/positions", _positions, methods=["GET"])
    app.add_api_route("/config/okx-options/ledger", _ledger, methods=["GET"])
    app.add_api_route("/config/okx-options/reminder", _reminder, methods=["GET"])
    app.add_api_route("/config/okx-options/pending", _pending, methods=["GET"])
    app.add_api_route("/config/okx-options/cancel", _cancel, methods=["POST"])
    app.add_api_route("/config/okx-options/params", _params_get, methods=["GET"])
    app.add_api_route("/config/okx-options/params", _params_save, methods=["POST"])
    app.add_api_route("/config/okx-options/preview", _preview, methods=["POST"])
    app.add_api_route("/config/okx-options/sell/start", _sell_start, methods=["POST"])
    app.add_api_route("/config/okx-options/sell/confirm", _sell_confirm, methods=["POST"])
    app.add_api_route("/config/okx-options/close/start", _close_start, methods=["POST"])
    app.add_api_route("/config/okx-options/close/confirm", _close_confirm, methods=["POST"])
    app.add_api_route("/config/okx-options/cover/start", _cover_start, methods=["POST"])
    app.add_api_route("/config/okx-options/cover/confirm", _cover_confirm, methods=["POST"])
