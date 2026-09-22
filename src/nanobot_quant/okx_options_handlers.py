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
POST /config/okx-options/preview    — 卖期权订单预览（纯计算，不下单；put/call 按 opt_type 分派）
POST /config/okx-options/sell/start|confirm   — 卖期权两步确认（put/call 分派；call 可选 cost_basis 保本门）
POST /config/okx-options/close/start|confirm  — 买回平仓两步确认（put/call 分派）
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
from fastapi.encoders import jsonable_encoder
from starlette.responses import HTMLResponse, JSONResponse

from nanobot_quant import okx_options_data as od
from nanobot_quant import okx_options_live as ol
from nanobot_quant import okx_options_select as osel
from nanobot_quant import okx_options_td as otd
from nanobot_quant import okx_options_trade as ot
from nanobot_quant import option_tape as otp
from nanobot_quant.backtest.options_replay_data_source import probe as backtest_probe
from nanobot_quant.backtest.options_replay_data_source import (
    probe_chain_dict as backtest_probe_chain,
)
from nanobot_quant.data_sources.periods import PERIODS
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


def _dispatch_preview(inst_id: str, sz: int, ord_type: str, px,
                      cost_basis=None) -> dict:
    """按合约类型分派订单预览：Call → preview_open_call（含保本门），Put → preview_open_put。"""
    if ot.resolve_instrument(inst_id)["opt_type"] == "C":
        return ot.preview_open_call(inst_id, sz, ord_type, px,
                                    cost_basis=cost_basis)
    return ot.preview_open_put(inst_id, sz, ord_type, px)


def _dispatch_sell(account: str, inst_id: str, sz: int, ord_type: str,
                   px, cost_basis=None) -> dict:
    """卖期权分派：Call → open_call（cost_basis 保本门强制），Put → open_put。"""
    if ot.resolve_instrument(inst_id)["opt_type"] == "C":
        return ot.open_call(account, inst_id=inst_id, sz=sz, ord_type=ord_type,
                            px=px, cost_basis=cost_basis)
    return ot.open_put(account, inst_id=inst_id, sz=sz, ord_type=ord_type, px=px)


def _dispatch_close(account: str, inst_id: str, sz: int, ord_type: str,
                    px) -> dict:
    """买回平仓分派：Call → close_call（只匹配 open_call），Put → close_put。"""
    if ot.resolve_instrument(inst_id)["opt_type"] == "C":
        return ot.close_call(account, inst_id=inst_id, sz=sz,
                             ord_type=ord_type, px=px)
    return ot.close_put(account, inst_id=inst_id, sz=sz, ord_type=ord_type, px=px)


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
        _PAGE_HTML = _load_template("okx_options_page.html").replace(
            "__PERIODS__", json.dumps(list(PERIODS)))

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

    # ── C24：合约选择（卖 put 候选 + 选择参数）──────────────

    async def _candidates(request: Request):
        """按选择参数从期权链挑卖 put 候选（只读计算，不下单）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        family = (q.get("family") or "BTC-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse({"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        base_px = None
        if q.get("base_px"):
            try:
                base_px = float(q["base_px"])
            except ValueError:
                return JSONResponse({"ok": False, "error": "base_px 参数非法"})
        exp_ms = (q.get("exp_ms") or "").strip() or None
        if exp_ms is not None:
            try:
                exp_ms = int(exp_ms)
            except ValueError:
                return JSONResponse({"ok": False, "error": "exp_ms 参数非法"})
        try:
            res = await asyncio.to_thread(osel.select_puts, family, base_px, None, None, exp_ms)
        except OkxSdkError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **res})

    async def _selector_save(request: Request):
        """保存卖 put 候选选择参数（option_params.json 的 selector 字段）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        raw = body.get("selector") if isinstance(body.get("selector"), dict) else body
        cleaned, verr = osel.validate_selector(raw)
        if verr:
            return JSONResponse({"ok": False, "error": verr})
        try:
            params = await asyncio.to_thread(ot.save_option_params, selector=cleaned)
        except (RuntimeError, OSError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "params": params})

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
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

    async def _sim(request: Request):
        # 盘口吃单模拟（C22a）：sell=卖 put 吃买盘 / buy=平仓吃卖盘
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        inst = (request.query_params.get("inst_id") or "").strip().upper()
        side = (request.query_params.get("side") or "sell").lower()
        try:
            sz = int(request.query_params.get("sz") or 1)
        except ValueError:
            sz = 1
        if not inst:
            return JSONResponse({"ok": False, "error": "inst_id 必填"})
        try:
            sim = await asyncio.to_thread(ot.simulate_fill, inst, side, sz)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "sim": sim})

    async def _suggest_px(request: Request):
        # 定价保护线（C22b）：N 张盘口模拟均价 × (1 ∓ 容忍滑点%)——下单页预填用
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        inst = (q.get("inst_id") or "").strip().upper()
        side = (q.get("side") or "sell").lower()
        try:
            sz = int(q.get("sz") or 1)
        except ValueError:
            sz = 1
        if not inst:
            return JSONResponse({"ok": False, "error": "inst_id 必填"})
        try:
            out = await asyncio.to_thread(ot.suggest_px_for_order, inst, side, sz)
            return JSONResponse(out)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    async def _lifecycle(request: Request):
        # 单合约生命周期：mark 价从上市到现在 + 同时刻标的参考价（表格，无图）
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        inst = (request.query_params.get("inst_id") or "").strip().upper()
        bar = (request.query_params.get("bar") or "15m").strip()
        if not inst:
            return JSONResponse({"ok": False, "error": "inst_id 必填"})
        try:
            data = await asyncio.to_thread(od.fetch_lifecycle, inst, bar)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

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
            # 页面打开顺带触发到期判定（幂等：仅已到期仍 open 的行查账单；
            # 判定失败不阻塞持仓展示，错误单列返回）
            settled_error = ""
            try:
                settled = await asyncio.to_thread(ot.settle_expired_puts)
            except (OkxSdkError, RuntimeError) as se:
                settled = []
                settled_error = str(se)
            puts = await asyncio.to_thread(ot.open_puts, account)
            bal = await asyncio.to_thread(ot.account_balance, account)
            cfg = await asyncio.to_thread(ot.account_config, account)
            open_rows = [e for e in ot.load_ledger()
                         if e.get("kind") in ("open_put", "open_call")
                         and e.get("status") in ("open", "pending", ot.STATUS_SETTLED_REVIEW)]
            resp = {"ok": True, "positions": puts,
                    "balance": bal, "config": cfg,
                    "ledger_open": open_rows, "settled": settled}
            if settled_error:
                resp["settled_error"] = settled_error
            return JSONResponse(resp)
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
        # 先跑一轮到期判定再出提醒，消除「提醒先于 settle 返回」的页面加载竞态
        # （settle 幂等：仅已到期仍 open 的行查账单；失败不阻塞提醒，错误单列返回）
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        settled_error = ""
        try:
            await asyncio.to_thread(ot.settle_expired_puts)
        except (OkxSdkError, RuntimeError) as se:
            settled_error = str(se)
        resp = {"ok": True, "reminders": ot.expiry_reminder()}
        if settled_error:
            resp["settled_error"] = settled_error
        return JSONResponse(resp)

    # ── S3：到期巡检 daemon（option_params.json live 字段）────────

    async def _live_get(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        return JSONResponse({"ok": True, "live": ol.live_state(),
                             "config": ol.live_config(),
                             "strategy_defaults": ol.DEFAULT_STRATEGY,
                             "available_families": list(od.FAMILIES),
                             "events": ol.load_events(30)})

    async def _live_set(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        b = body or {}
        try:
            enabled = None if b.get("enabled") is None else bool(b["enabled"])
            interval_s = None if b.get("interval_s") is None else int(b["interval_s"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False,
                                 "error": "enabled 需布尔、interval_s 需整数"})
        if interval_s is not None and not (ol.MIN_INTERVAL_S <= interval_s
                                           <= ol.MAX_INTERVAL_S):
            return JSONResponse({"ok": False,
                                 "error": f"interval_s 范围 {ol.MIN_INTERVAL_S}–"
                                          f"{ol.MAX_INTERVAL_S} 秒"})
        strategy = b.get("strategy")
        if strategy is not None and not isinstance(strategy, dict):
            return JSONResponse({"ok": False, "error": "strategy 需为对象"})
        if isinstance(strategy, dict) and strategy.get("families") is not None:
            fams = strategy.get("families")
            if isinstance(fams, str):
                strategy["families"] = [f.strip() for f in fams.split(",") if f.strip()]
            if not isinstance(strategy.get("families"), list):
                return JSONResponse({"ok": False,
                                     "error": "strategy.families 需为数组或逗号分隔字符串"})
            unknown = [f for f in strategy["families"] if f not in od.FAMILIES]
            if unknown:
                return JSONResponse({"ok": False,
                                     "error": f"未知标的家族 {unknown}；可选：{list(od.FAMILIES)}"})
        ol.save_live_config(enabled=enabled, interval_s=interval_s, strategy=strategy)
        state = await asyncio.to_thread(ol.sync)
        return JSONResponse({"ok": True, "live": state, "config": ol.live_config()})

    # ── 撤单 / 当前委托（单步，撤单无资金流）────────────

    # ── 盘口采集（研究用 · 只读）────────────────────

    async def _tape_get(request: Request):
        """采集器配置 + 运行状态 + 当天落盘统计（只读）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        return JSONResponse({"ok": True, "config": otp.tape_config(),
                             "state": otp.state(),
                             "defaults": otp.DEFAULT_TAPE,
                             "available_families": list(od.FAMILIES)})

    async def _tape_set(request: Request):
        """保存采集参数并启/停采集（只读采样，不涉及任何交易开关）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        b = body or {}
        families = b.get("families")
        if families is not None:
            if not isinstance(families, (list, tuple)):
                return JSONResponse({"ok": False, "error": "families 需数组"})
            unknown = [f for f in families if f not in od.FAMILIES]
            if unknown:
                return JSONResponse(
                    {"ok": False,
                     "error": f"未知标的家族 {unknown}；可选：{list(od.FAMILIES)}"})
        cur = otp.tape_config()
        for k, v in b.items():
            if k in otp.DEFAULT_TAPE:
                cur[k] = v
        try:
            config = await asyncio.to_thread(otp.save_tape_config, **cur)
            await asyncio.to_thread(otp.sync)
            state = otp.state()
        except Exception as e:  # noqa: BLE001 —— 采集参数问题不应 500
            return JSONResponse({"ok": False,
                                 "error": f"{type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, "config": config, "state": state})

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

    async def _ledger_close(request: Request):
        # 台账手动关账（单步、无资金流）：open 卖 put/call 行 → closed_manual
        # （官方后台手动平仓等系统外操作收尾，仅台账标记，不查 OKX）
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        entry_id = (body or {}).get("id") or ""
        note = (body or {}).get("note") or ""
        if not entry_id:
            return JSONResponse({"ok": False, "error": "缺少 id"})
        try:
            e = await asyncio.to_thread(ot.manual_close_entry, entry_id, note)
        except (okx_sdk.OkxSdkError, RuntimeError) as e2:
            return JSONResponse({"ok": False, "error": str(e2)})
        if e is None:
            return JSONResponse({"ok": False,
                                 "error": "未找到该 open 卖 put/call 行（可能已关账/结算）"})
        return JSONResponse({"ok": True, "entry": e})

    async def _ledger_reopen(request: Request):
        # 撤销手动关账（单步）：closed_manual → open（误关账恢复，交回到期巡检管辖）
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        entry_id = (body or {}).get("id") or ""
        if not entry_id:
            return JSONResponse({"ok": False, "error": "缺少 id"})
        try:
            e = await asyncio.to_thread(ot.reopen_entry, entry_id)
        except (okx_sdk.OkxSdkError, RuntimeError) as e2:
            return JSONResponse({"ok": False, "error": str(e2)})
        if e is None:
            return JSONResponse({"ok": False,
                                 "error": "未找到该 closed_manual 卖 put/call 行（仅手动关账行可撤销）"})
        return JSONResponse({"ok": True, "entry": e})

    async def _ledger_backfill(request: Request):
        # 历史遗留行赔付回填（单步、纯本地台账写入、无资金流）：
        # settled_* 但缺 settle_px/settle_pnl 的行 → 按 OKX 交割账单补回填
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        try:
            res = await asyncio.to_thread(ot.backfill_settlements)
        except (okx_sdk.OkxSdkError, RuntimeError) as e2:
            return JSONResponse({"ok": False, "error": str(e2)})
        return JSONResponse({"ok": True, **res})

    async def _cover_prefill(request: Request):
        # 到期 ITM 补买预填：数量 = 面值(lot)×张数、价格默认 = 现货现价（可改）
        inst_id = request.query_params.get("inst_id") or ""
        try:
            sz = max(int(request.query_params.get("sz") or "1"), 1)
        except ValueError:
            sz = 1
        if not inst_id:
            return JSONResponse({"ok": False, "error": "缺少 inst_id"})
        # instId → 家族全名（含 _UM，_SPOT/_INDEX 表键），现货价取价须匹配
        fam = od.family_of(inst_id)
        try:
            spot = await asyncio.to_thread(od.spot_price, fam)
        except (okx_sdk.OkxSdkError, RuntimeError):
            spot = None
        ent = await asyncio.to_thread(
            ot.find_entry, lambda x: x.get("kind") == "open_put"
            and x.get("inst_id") == inst_id
            and x.get("status") == ot.STATUS_SETTLED_ITM)
        spot_inst = ot.spot_pair_of(inst_id)
        limits = None
        if spot_inst:
            try:
                limits = await asyncio.to_thread(
                    ot.spot_limits, request.query_params.get("account") or "",
                    spot_inst, spot)
            except (okx_sdk.OkxSdkError, RuntimeError):
                limits = None
        try:
            pre = await asyncio.to_thread(ot.cover_prefill_defaults,
                                          inst_id, sz, spot, ent, limits)
        except (okx_sdk.OkxSdkError, RuntimeError) as e2:
            return JSONResponse({"ok": False, "error": str(e2)})
        pre.update({"inst_id": inst_id, "sz": sz, "spot_inst": spot_inst,
                    "amount": round((pre["qty"] or 0) * (pre["px"] or 0), 2)})
        return JSONResponse({"ok": True, **pre})

    async def _exit_prefill(request: Request):
        # 到期 ITM（call 被行权）现货出货预填：数量 = 面值(lot)×张数、价格默认 = 现货现价
        inst_id = request.query_params.get("inst_id") or ""
        try:
            sz = max(int(request.query_params.get("sz") or "1"), 1)
        except ValueError:
            sz = 1
        if not inst_id:
            return JSONResponse({"ok": False, "error": "缺少 inst_id"})
        spot_inst = ot.spot_pair_of(inst_id)
        if not spot_inst:
            return JSONResponse({"ok": False,
                                 "error": "无法解析该标的的现货对，无法现货出货"})
        fam = od.family_of(inst_id)
        try:
            spot = await asyncio.to_thread(od.spot_price, fam)
        except (okx_sdk.OkxSdkError, RuntimeError):
            spot = None
        ent = await asyncio.to_thread(
            ot.find_entry, lambda x: x.get("kind") == "open_call"
            and x.get("inst_id") == inst_id
            and x.get("status") == ot.STATUS_SETTLED_ITM)
        try:
            limits = await asyncio.to_thread(
                ot.spot_limits, request.query_params.get("account") or "",
                spot_inst, spot)
        except (okx_sdk.OkxSdkError, RuntimeError):
            limits = None
        try:
            pre = await asyncio.to_thread(ot.exit_prefill_defaults,
                                          inst_id, sz, spot, ent, limits)
        except (okx_sdk.OkxSdkError, RuntimeError) as e2:
            return JSONResponse({"ok": False, "error": str(e2)})
        pre.update({"inst_id": inst_id, "sz": sz, "spot_inst": spot_inst})
        return JSONResponse({"ok": True, **pre})

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
        fields: dict = {}
        if body.get("collateral_ratio_pct") is not None:
            try:
                ratio = int(body.get("collateral_ratio_pct"))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "担保比例必须为整数（0–200）"})
            if not 0 <= ratio <= 200:
                return JSONResponse({"ok": False, "error": "担保比例须在 0–200 之间"})
            fields["collateral_ratio_pct"] = ratio
        if body.get("px_tolerance_pct") is not None:
            try:
                tol = float(body.get("px_tolerance_pct"))
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "容忍滑点必须为数字（0–50）"})
            if not 0 <= tol <= 50:
                return JSONResponse({"ok": False, "error": "容忍滑点须在 0–50 之间"})
            fields["px_tolerance_pct"] = tol
        if not fields:
            return JSONResponse({"ok": False, "error": "无可保存字段"})
        return JSONResponse({"ok": True, "params": ot.save_option_params(**fields)})

    # ── 批次 C：下单（预览 → start → confirm 两步确认）─────────

    async def _covered(request: Request) -> JSONResponse:
        """卖 call（covered）上下文：现货可用/可卖张数 + 成本锚 C 建议（只读）。"""
        try:
            q = request.query_params
            family = q.get("family") or ""
            account = q.get("account") or ""
            if not family:
                return JSONResponse({"ok": False, "error": "缺少 family 参数"})
            out = await asyncio.to_thread(ot.covered_context, account, family)
            return JSONResponse(out)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

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
        cost_basis = _num(body, "cost_basis")
        try:
            out = await asyncio.to_thread(
                _dispatch_preview, inst_id, sz, ord_type, px, cost_basis)
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
        cost_basis = _num(body, "cost_basis")
        if sz <= 0:
            return JSONResponse({"ok": False, "error": "张数必须为正整数"})
        try:
            prev = await asyncio.to_thread(
                _dispatch_preview, inst_id, sz, ord_type, px, cost_basis)
        except (OkxSdkError, RuntimeError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "stage": _stage("sell", {
            "account": account, "inst_id": inst_id, "sz": sz,
            "ord_type": ord_type, "px": px if ord_type != "market" else None,
            "cost_basis": cost_basis,
            "opt_type": prev.get("opt_type", "P"),
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
                _dispatch_sell, p["account"], p["inst_id"], p["sz"],
                p["ord_type"], p.get("px"), p.get("cost_basis"))
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
                _dispatch_close, p["account"], p["inst_id"], p["sz"],
                p["ord_type"], p.get("px"))
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

    async def _exit_start(request: Request):
        """出货两步确认第一步：生成一次性令牌（下发前不碰资金）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        account = (body.get("account") or "").strip()
        inst_id = (body.get("inst_id") or "").strip().upper()
        spot_inst = (body.get("spot_inst") or "").strip().upper()
        base_qty = _num(body, "base_qty")
        quote_amt = _num(body, "quote_amt")
        if not spot_inst:
            return JSONResponse({"ok": False, "error": "缺少 spot_inst（现货交易对，如 SOL-USD）"})
        if (base_qty is None or base_qty <= 0) and (quote_amt is None or quote_amt <= 0):
            return JSONResponse({"ok": False, "error": "需指定 base_qty 或 quote_amt"})
        prev = {"spot_inst": spot_inst, "base_qty": base_qty, "quote_amt": quote_amt,
                "note": ("到期 ITM（call 被行权）现金结算后的现货出货（市价卖出）——"
                         "U 本位不交币，市价卖出现货 ≈ 等效按 K 出货，把 covered 组合闭回无持仓。")}
        return JSONResponse({"ok": True, "stage": _stage("exit", {
            "account": account, "inst_id": inst_id, "spot_inst": spot_inst,
            "base_qty": base_qty, "quote_amt": quote_amt, "preview": prev})})

    async def _exit_confirm(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        body, jerr = await _json_body(request)
        if jerr:
            return JSONResponse({"ok": False, "error": jerr})
        act, perr = _consume(body)
        if perr:
            return JSONResponse({"ok": False, "error": perr})
        if act["action"] != "exit":
            return JSONResponse({"ok": False, "error": "动作类型不匹配，请重新发起"})
        p = act["payload"]
        try:
            res = await asyncio.to_thread(
                ot.spot_exit, p["account"], spot_inst=p["spot_inst"],
                base_qty=p.get("base_qty"), quote_amt=p.get("quote_amt"),
                ref_inst=p.get("inst_id") or "")
            return JSONResponse({"ok": True, "entry": res})
        except (OkxSdkError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)})

    app.add_api_route("/config/okx-options", _page, methods=["GET"])
    app.add_api_route("/config/okx-options/expiries", _expiries, methods=["GET"])
    app.add_api_route("/config/okx-options/chain", _chain, methods=["GET"])
    app.add_api_route("/config/okx-options/candidates", _candidates, methods=["GET"])
    app.add_api_route("/config/okx-options/selector", _selector_save, methods=["POST"])
    app.add_api_route("/config/okx-options/ticker", _ticker, methods=["GET"])
    app.add_api_route("/config/okx-options/sim", _sim, methods=["GET"])
    app.add_api_route("/config/okx-options/suggest-px", _suggest_px, methods=["GET"])
    app.add_api_route("/config/okx-options/lifecycle", _lifecycle, methods=["GET"])
    app.add_api_route("/config/okx-options/accounts", _accounts, methods=["GET"])
    app.add_api_route("/config/okx-options/positions", _positions, methods=["GET"])
    app.add_api_route("/config/okx-options/ledger", _ledger, methods=["GET"])
    app.add_api_route("/config/okx-options/ledger/close", _ledger_close, methods=["POST"])
    app.add_api_route("/config/okx-options/ledger/reopen", _ledger_reopen, methods=["POST"])
    app.add_api_route("/config/okx-options/ledger/backfill", _ledger_backfill, methods=["POST"])
    app.add_api_route("/config/okx-options/reminder", _reminder, methods=["GET"])
    app.add_api_route("/config/okx-options/pending", _pending, methods=["GET"])
    app.add_api_route("/config/okx-options/cancel", _cancel, methods=["POST"])
    app.add_api_route("/config/okx-options/params", _params_get, methods=["GET"])
    app.add_api_route("/config/okx-options/params", _params_save, methods=["POST"])
    app.add_api_route("/config/okx-options/live", _live_get, methods=["GET"])
    app.add_api_route("/config/okx-options/live", _live_set, methods=["POST"])
    app.add_api_route("/config/okx-options/tape", _tape_get, methods=["GET"])
    app.add_api_route("/config/okx-options/tape", _tape_set, methods=["POST"])
    async def _backtest_probe(request: Request):
        """期权回测数据层诊断（只读，真实拉数）。

        GET /config/okx-options/backtest-probe?family=SOL-USD_UM&timestep=15m&days=3

        验证两个未实测假设：标的 K 线能否按区间拉到；期权 instId 推算
        （每日到期 + 整数 strike）的 mark 命中率。命中率 0 = 枚举规则要修正。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        family = (q.get("family") or "SOL-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse(
                {"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            days = max(1, min(30, int(q.get("days") or 3)))
        except (TypeError, ValueError):
            days = 3
        try:
            length = max(1, min(300, int(q.get("length") or 120)))
        except (TypeError, ValueError):
            length = 120
        timestep = (q.get("timestep") or "15m").strip()
        try:
            data = await asyncio.to_thread(
                backtest_probe, family, timestep, days, length)
        except Exception as e:  # noqa: BLE001 —— 诊断端点不 500
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

    async def _td_panel(request: Request):
        # 标的 TD 状态（C24 ⑤）：人工卖 put 前看标的是否临近衰竭，只读展示
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        period = (request.query_params.get("period") or "").strip() or None
        try:
            # 自动循环配置的标的家族——面板用它给家族外的行打标记
            fams = (ol.live_config().get("strategy") or {}).get("families") or []
        except Exception:  # noqa: BLE001 —— 配置读不到就退化为「不做家族标记」，不阻展示
            fams = []
        try:
            data = await asyncio.to_thread(otd.panel, period, None, fams)
        except Exception as e:  # noqa: BLE001 —— 展示层，异常回 JSON 不 500
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

    app.add_api_route("/config/okx-options/td", _td_panel, methods=["GET"])
    app.add_api_route("/config/okx-options/backtest-probe", _backtest_probe, methods=["GET"])

    async def _backtest_probe_chain(request: Request):
        """``chain_dict_at`` 真实性校验（只读，真实拉数）。

        GET /config/okx-options/backtest-probe/chain?family=SOL-USD_UM&days=3

        单测里的 mark 是 BS 自己生成的（σ 已知），只能证明反解器自洽；
        这里用**真实链**看反解出的 IV 是否落在市场合理区间、delta 是否单调。
        路径式（非 query 多参）——聊天/文档里裸 ``&`` 常被渲染成 HTML 实体。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        family = (q.get("family") or "SOL-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse(
                {"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            days = max(1, min(30, int(q.get("days") or 3)))
        except (TypeError, ValueError):
            days = 3
        timestep = (q.get("timestep") or "15m").strip()
        try:
            data = await asyncio.to_thread(
                backtest_probe_chain, family, timestep, days)
        except Exception as e:  # noqa: BLE001 —— 诊断端点不 500
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

    app.add_api_route("/config/okx-options/backtest-probe/chain",
                      _backtest_probe_chain, methods=["GET"])

    def _run_options_backtest(family: str, timestep: str, days: int,
                              tp_pct, cash: float) -> dict:
        """跑一次真实期权回测（小参数）—— 串起数据源→选档→记账。"""
        from nanobot_quant.backtest.options_driver import OptionsBacktestDriver

        now = int(time.time())
        drv = OptionsBacktestDriver(
            family, timestep=timestep, start_ts=now - max(1, days) * 86400,
            end_ts=now, td_bars=120, tp_pct=tp_pct, initial_cash=cash)
        res = drv.run()
        # 明细可能很长，探针只回摘要；完整结果走 CLI/页面
        res["fills"] = res.get("fills", [])[:20]
        res["notes"] = res.get("notes", [])[:8]
        return res

    async def _backtest_probe_run(request: Request):
        """真实回测跑一遍（只读，不写任何实盘状态）。

        GET /config/okx-options/backtest-probe/run?family=SOL-USD_UM&days=3

        验证「数据源 → 实盘选档 → 记账」整条链是否跑得通、KPI 是否自洽。
        小区间（3 天 15m）约 30–60s；大区间请用 CLI 或异步入口。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return _deny(err)
        q = request.query_params
        family = (q.get("family") or "SOL-USD_UM").upper()
        if family not in od.FAMILIES:
            return JSONResponse(
                {"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            days = max(1, min(14, int(q.get("days") or 3)))
        except (TypeError, ValueError):
            days = 3
        try:
            cash = max(100.0, float(q.get("cash") or 10000.0))
        except (TypeError, ValueError):
            cash = 10000.0
        timestep = (q.get("timestep") or "15m").strip()
        raw_tp = q.get("tp")
        try:
            tp_pct = float(raw_tp) if raw_tp not in (None, "") else None
        except (TypeError, ValueError):
            tp_pct = None
        try:
            data = await asyncio.to_thread(
                _run_options_backtest, family, timestep, days, tp_pct, cash)
        except Exception as e:  # noqa: BLE001 —— 诊断端点不 500
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        # pandas/numpy 类型不是 JSON 原生类型 —— 统一净化，端点永不因序列化 500
        return JSONResponse({"ok": True, "data": jsonable_encoder(data)})

    app.add_api_route("/config/okx-options/backtest-probe/run",
                      _backtest_probe_run, methods=["GET"])
    app.add_api_route("/config/okx-options/covered", _covered, methods=["GET"])
    app.add_api_route("/config/okx-options/preview", _preview, methods=["POST"])
    app.add_api_route("/config/okx-options/sell/start", _sell_start, methods=["POST"])
    app.add_api_route("/config/okx-options/sell/confirm", _sell_confirm, methods=["POST"])
    app.add_api_route("/config/okx-options/close/start", _close_start, methods=["POST"])
    app.add_api_route("/config/okx-options/close/confirm", _close_confirm, methods=["POST"])
    app.add_api_route("/config/okx-options/cover/start", _cover_start, methods=["POST"])
    app.add_api_route("/config/okx-options/cover/confirm", _cover_confirm, methods=["POST"])
    app.add_api_route("/config/okx-options/cover-prefill", _cover_prefill, methods=["GET"])
    app.add_api_route("/config/okx-options/exit-prefill", _exit_prefill, methods=["GET"])
    app.add_api_route("/config/okx-options/exit/start", _exit_start, methods=["POST"])
    app.add_api_route("/config/okx-options/exit/confirm", _exit_confirm, methods=["POST"])
