"""A 股 ETF 期权页面与 API（``/config/ashare``）。

只读采集器的控制面：开关 / 间隔 / 标的 / 虚值带 + 立即采样 + 合约清单刷新 +
当天落盘统计。数据面在 :mod:`nanobot_quant.ashare.tape`。

权限：仅 commander（与其余业务页一致）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from nanobot_quant.ashare import contracts as ac
from nanobot_quant.ashare import tape
from nanobot_quant import credential_registry as creg

_HERE = Path(__file__).resolve().parent


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not user:
        return "请先登录", False
    try:
        if not gatekeeper._platform.is_commander(user):
            return "仅指挥官（owner）可访问", False
    except Exception:  # noqa: BLE001
        return "权限校验失败", False
    return None, True


def _state_payload() -> dict:
    storage = creg._get_storage_dir()
    c = ac.contracts(storage)
    items = c.get("items") or []
    return {
        "config": tape.tape_config(),
        "status": tape.state(),
        "contracts": {
            "count": len(items),
            "fetched_at": c.get("fetched_at"),
            "age_s": c.get("age_s"),
            "retry_in_s": c.get("retry_in_s") or 0,
            "source": c.get("source"),
            "error": c.get("error") or "",
            "underlyings": sorted({i["undl"] for i in items}),
        },
        "options": {
            # 只列池内标的（588080 流动性太差，故意不给选项）
            "underlyings": [{"code": c, "name": n} for c, n in tape.POOL_UNDERLYINGS],
            "interval_range": list(tape.INTERVAL_RANGE),
        },
    }


def register_ashare_routes(app, gatekeeper) -> None:

    @app.get("/config/ashare", response_class=HTMLResponse)
    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(f"<h3>{err}</h3>", status_code=403)
        return HTMLResponse(_load_template("ashare_page.html"))

    @app.get("/config/ashare/state")
    async def _state(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        return JSONResponse({"ok": True, **_state_payload()})

    @app.post("/config/ashare/save")
    async def _save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "无效的请求体"}, status_code=400)
        cfg = tape.save_tape_config(
            enabled=bool(body.get("enabled")),
            interval_s=body.get("interval_s"),
            underlyings=body.get("underlyings"),
            expiries=body.get("expiries"),
            band_pct=body.get("band_pct"),
            max_per_side=body.get("max_per_side"),
        )
        try:
            tape.sync()
        except Exception as e:  # noqa: BLE001 — 保存成功、启停失败也要如实回报
            return JSONResponse({"ok": False, "config": cfg,
                                 "error": f"保存成功但启停失败: {type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, "config": cfg, "status": tape.state()})

    @app.post("/config/ashare/sample")
    async def _sample(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        persist = bool((body or {}).get("persist"))
        rec = tape.sample_now(persist=persist)
        rows = rec.get("rows") or []
        return JSONResponse({
            "ok": True,
            "persisted": bool(persist and rows),
            "skipped": rec.get("skipped") or "",
            "error": rec.get("error") or "",
            "ts": rec.get("ts"),
            "spot": rec.get("spot") or {},
            "picked": rec.get("picked"),
            "rows": len(rows),
            "preview": rows[:5],
        })

    @app.post("/config/ashare/contracts/refresh")
    async def _refresh(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        c = ac.contracts(creg._get_storage_dir(), force=True)
        return JSONResponse({"ok": True, "count": len(c.get("items") or []),
                             "source": c.get("source"),
                             "age_s": c.get("age_s"),
                             "retry_in_s": c.get("retry_in_s") or 0,
                             "error": c.get("error") or ""})

    # 开机自恢复：采集是只读的，enabled=true 时随网关启动。
    # 与交易循环不同——不涉及资金与订单，中断只丢样本，故允许自动拉起。
    try:
        tape.sync()
    except Exception as e:  # noqa: BLE001
        print(f"[ASHARE-TAPE] 启动同步失败: {type(e).__name__}: {e}", flush=True)


__all__ = ["register_ashare_routes"]
