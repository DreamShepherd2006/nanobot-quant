"""OKX 期权链 WebUI page (Commander only) — 期权线批次 B（只读通道）。

GET /config/okx-options           — 页面（标的/到期/现货 HV/期权链定价表）
GET /config/okx-options/expiries  — 某 family 全部未到期列表（JSON）
GET /config/okx-options/chain     — 链数据 JSON（family/expiries/hv_days/range）

数据全部来自 OKX 公共端点（免 key），只读展示、不含任何下单能力——
卖 put 执行在批次 C 交付。
"""

from __future__ import annotations

import asyncio
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from nanobot_quant import okx_options_data as od
from nanobot_quant.okx_options_data import OkxOptionsError

_HERE = os.path.dirname(os.path.abspath(__file__))

_PAGE_HTML: str = ""


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
            return JSONResponse({"ok": False, "error": err},
                                status_code=403 if "Commander" in err else 401)
        family = (request.query_params.get("family") or "BTC-USD").upper()
        if family not in od.FAMILIES:
            return JSONResponse({"ok": False, "error": f"未知标的 {family}，可选 {od.FAMILIES}"})
        try:
            exps = await asyncio.to_thread(od.list_expiries, family)
        except OkxOptionsError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "expiries": exps})

    async def _chain(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err},
                                status_code=403 if "Commander" in err else 401)
        q = request.query_params
        family = (q.get("family") or "BTC-USD").upper()
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
        # expiries: 逗号分隔 exp_ms；空=默认近 3
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
        except OkxOptionsError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "data": chain})

    app.add_api_route("/config/okx-options", _page, methods=["GET"])
    app.add_api_route("/config/okx-options/expiries", _expiries, methods=["GET"])
    app.add_api_route("/config/okx-options/chain", _chain, methods=["GET"])
