"""Execution parameter handlers — WebUI for exec_params.json.

Registered by gatekeeper as ``/config/exec`` (business management chat,
🛡️ 执行参数 entry).  The page renders the two groups (risk control /
execution quality) as editable cards with per-field bounds; saving
validates everything and persists to ``{data_root}/credentials/exec_params.json``.
Defaults are the pre-parameterisation hardcoded values, so an unmodified
setup behaves exactly as before.

Only the Commander may view/change these parameters — they are the
on-chain risk boundary (position limit, slippage, buffers), so the page
and the save endpoint both enforce ``is_commander``.
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .exec_params import (
    GROUP_TITLES,
    PARAM_META,
    load_exec_params,
    save_exec_params,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


_PAGE_HTML = _load_template("exec_params_page.html")


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    """Return (error_message_or_None, ok)."""
    _u = request.session.get("user")
    if not _u:
        return "请先登录", False
    if not gatekeeper._platform.is_commander(_u):
        return "仅 Commander 可操作", False
    return None, True


# ── Page rendering ───────────────────────────────────────────────────────

def _field_html(key: str, value: object) -> str:
    meta = PARAM_META[key]
    label = meta.get("label", key)
    hint = meta.get("hint", "")
    std = meta.get("std", "")
    lo, hi = meta["min"], meta["max"]
    step = str(meta.get("step", 0.01))
    return (
        f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
        f'<input type="number" id="{key}" name="{key}" value="{value}" '
        f'min="{lo}" max="{hi}" step="{step}">'
        f'<span class="f-std">默认 {std} · 范围 {lo}–{hi}</span>'
        f'<span class="f-hint">{hint}</span></div>'
    )


def _group_html(group: str, params: dict) -> str:
    fields = "".join(
        _field_html(k, params[k])
        for k in PARAM_META
        if PARAM_META[k].get("group") == group
    )
    if not fields:
        return ""
    return f'<div class="card"><h3>{GROUP_TITLES[group]}</h3>{fields}</div>'


def _render_page(params: dict, message: str = "") -> str:
    try:
        from .exec_params import exec_params_path
        custom = exec_params_path().is_file()
    except Exception:
        custom = False
    banner = (
        '<div class="banner custom">⚙️ 已自定义执行参数（exec_params.json）——'
        '如需恢复默认，点击「恢复默认」或删除该文件</div>'
        if custom
        else '<div class="banner default">默认参数（= 旧版硬编码行为，零变化）</div>'
    )
    banner += (
        '<div class="banner locked">🔒 系统级风控参数：仅 Commander 可修改，'
        'MCP/LLM 不可传（调用级 portfolio_value / quantity 除外）</div>'
    )
    msg = (
        f'<div class="banner msg" id="msg">{message}</div>'
        if message
        else '<div class="banner msg hidden" id="msg"></div>'
    )
    groups = "".join(_group_html(g, params) for g in ("risk", "exec"))
    return (
        _PAGE_HTML.replace("{banner}", banner)
        .replace("{msg}", msg)
        .replace("{groups}", groups)
    )


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Handlers ─────────────────────────────────────────────────────────────

async def exec_params_page(request: Request) -> HTMLResponse:
    """GET /config/exec — editable parameter cards (Commander only)."""
    _u = request.session.get("user")
    if not _u:
        return HTMLResponse(
            "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 请先登录</h3>",
            status_code=401,
        )
    params = load_exec_params()
    return HTMLResponse(_render_page(params))


async def exec_params_save(request: Request) -> JSONResponse:
    """POST /config/exec — validate + persist (Commander only)."""
    data = await _body(request)
    if data is None:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    result = save_exec_params(data)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error", "保存失败")},
                            status_code=400)
    return JSONResponse({"ok": True, "message": "执行参数已保存并即时生效",
                         "params": result.get("params")})


# ── Route registration helper ───────────────────────────────────────────

def register_exec_params_routes(app, gatekeeper) -> None:
    """Register execution parameter routes on the FastAPI app.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """

    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(
                f"<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 {err}</h3>",
                status_code=403 if "Commander" in err else 401,
            )
        params = load_exec_params()
        return HTMLResponse(_render_page(params))

    async def _save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err},
                                status_code=403 if "Commander" in err else 401)
        data = await _body(request)
        if data is None:
            return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
        result = save_exec_params(data)
        if not result.get("ok"):
            return JSONResponse({"ok": False, "error": result.get("error", "保存失败")},
                                status_code=400)
        gatekeeper._log(
            f"🛡️ 执行参数已更新: "
            f"position={result['params'].get('max_position_pct')} "
            f"drawdown={result['params'].get('max_drawdown_pct')} "
            f"stop_loss={result['params'].get('stop_loss_pct')} "
            f"slippage={result['params'].get('slippage')} "
            f"sol_buffer={result['params'].get('sol_buffer_pct')}"
        )
        return JSONResponse({"ok": True, "message": "执行参数已保存并即时生效",
                             "params": result.get("params")})

    app.add_route("/config/exec", _page, methods=["GET"])
    app.add_route("/config/exec", _save, methods=["POST"])
