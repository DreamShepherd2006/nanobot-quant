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
from .tokens_store import load_token_symbols

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

def _field_html(key: str, value: object, options: list[str] | None = None) -> str:
    meta = PARAM_META[key]
    label = meta.get("label", key)
    hint = meta.get("hint", "")
    std = meta.get("std", "")
    vtype = meta.get("type", "float")
    if vtype == "bool":
        checked = " checked" if value else ""
        return (
            f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
            f'<label class="switch">'
            f'<input type="checkbox" id="{key}" name="{key}"{checked}>'
            f'<span class="slider"></span></label>'
            f'<span class="f-std">默认 {'开' if std else '关'}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "enum":
        choices = meta["enum"]
        opts = "".join(
            f'<option value="{c}" {"selected" if str(value) == c else ""}>{c}</option>'
            for c in choices
        )
        return (
            f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
            f'<select id="{key}" name="{key}">{opts}</select>'
            f'<span class="f-std">默认 {std}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "str":
        choices = options or []
        if choices:
            opts = "".join(
                f'<option value="{c}" {"selected" if str(value) == c else ""}>{c}</option>'
                for c in choices
            )
            if str(value) not in choices:
                opts += f'<option value="{value}" selected>⚙️ 当前: {value}</option>'
            return (
                f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
                f'<select id="{key}" name="{key}">{opts}</select>'
                f'<span class="f-std">默认 {std} · tokens.json 登记代币</span>'
                f'<span class="f-hint">{hint}</span></div>'
            )
        return (
            f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
            f'<input type="text" id="{key}" name="{key}" value="{value}">'
            f'<span class="f-std">默认 {std}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    if vtype == "list":
        # 多选 checkbox（同名 name，collect() 收集为数组）
        choices = options or []
        values = value if isinstance(value, list) else [value]
        boxes = [
            f'<label class="chk"><input type="checkbox" class="multi" name="{key}" '
            f'value="{c}"{" checked" if c in values else ""}>{c}</label>'
            for c in choices
        ]
        # 当前值里不在候选列表的（如旧值）也显示，避免保存时被静默丢弃
        for v in values:
            if v not in choices:
                boxes.append(
                    f'<label class="chk"><input type="checkbox" class="multi" '
                    f'name="{key}" value="{v}" checked>⚙️ {v}</label>'
                )
        return (
            f'<div class="field"><label class="f-label">{label}</label>'
            f'<div class="chk-group">{"".join(boxes)}</div>'
            f'<span class="f-std">默认 {std} · tokens.json 登记代币（多选）</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    lo, hi = meta["min"], meta["max"]
    step = str(meta.get("step", 0.01))
    return (
        f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
        f'<input type="number" id="{key}" name="{key}" value="{value}" '
        f'min="{lo}" max="{hi}" step="{step}">'
        f'<span class="f-std">默认 {std} · 范围 {lo}–{hi}</span>'
        f'<span class="f-hint">{hint}</span></div>'
    )


def _group_html(group: str, params: dict, options: dict[str, list[str]] | None = None) -> str:
    fields = "".join(
        _field_html(k, params[k], (options or {}).get(k))
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
    # TD target options = managed tokens.json entries, minus stablecoins
    # (no analysis value). Native coin SOL appears here once registered
    # via /config/tokens (address auto-filled from builtin whitelist).
    _STABLECOINS = {"USDC", "USDT"}
    token_opts = {"td_symbols": [s for s in load_token_symbols()
                                  if s not in _STABLECOINS]}
    groups = "".join(
        _group_html(g, params, token_opts)
        for g in ("risk", "exec", "td", "batch")
    )
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
        # 批次（子钱包）初始化：td_batches 变更时立即补足子钱包并建映射（每标的独立台账）
        try:
            from nanobot_quant.batches import ensure_batches

            _symbols = result["params"].get("td_symbols") or ["SOL"]
            _msgs = []
            for _sym in _symbols:
                _b, _msg = ensure_batches(
                    int(result["params"].get("td_batches", 1) or 1),
                    _sym,
                )
                _msgs.append(f"{_sym}: {_msg}")
            gatekeeper._log(f"🧩 批次同步: {'; '.join(_msgs)}")
        except Exception as exc:
            gatekeeper._log(f"⚠️ 批次同步失败: {exc}")
        # TD 自主循环按新参数同步启停（td_enabled 开关 + 参数热更新）
        try:
            from nanobot_quant.td_live import sync_from_params

            td_status = sync_from_params(result["params"])
            gatekeeper._log(
                f"🔄 TD live 同步: running={td_status.get('running')} "
                f"thread_alive={td_status.get('thread_alive')}"
            )
        except Exception as exc:
            gatekeeper._log(f"⚠️ TD live 同步失败: {exc}")
        gatekeeper._log(
            f"🛡️ 执行参数已更新: "
            f"position={result['params'].get('max_position_pct')} "
            f"drawdown={result['params'].get('max_drawdown_pct')} "
            f"stop_loss={result['params'].get('stop_loss_pct')} "
            f"slippage={result['params'].get('slippage')} "
            f"sol_buffer={result['params'].get('sol_buffer_pct')} "
            f"td_enabled={result['params'].get('td_enabled')} "
            f"td_symbols={result['params'].get('td_symbols')} "
            f"td_sleeptime={result['params'].get('td_sleeptime')} "
            f"quantity_mode={result['params'].get('quantity_mode')} "
            f"td_quantity={result['params'].get('td_quantity')} "
            f"td_batches={result['params'].get('td_batches')} "
            f"exit_order={result['params'].get('exit_order')} "
            f"take_profit={result['params'].get('take_profit_pct')}"
        )
        return JSONResponse({"ok": True, "message": "执行参数已保存并即时生效",
                             "params": result.get("params")})

    app.add_route("/config/exec", _page, methods=["GET"])
    app.add_route("/config/exec", _save, methods=["POST"])
