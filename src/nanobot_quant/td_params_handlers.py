"""TD Sequential parameter handlers — WebUI for td_params.json.

Registered by gatekeeper as ``/config/td-params`` (business management
chat, 📐 TD 参数 entry).  The page renders the three parameter groups
(TD algorithm / scoring weights / strategy layer) as editable cards with
per-field bounds; saving validates everything (incl. weights summing to
1.0) and persists to ``{data_root}/legion/td_params.json``.  Defaults are
the pre-parameterisation hardcoded values, so an unmodified setup behaves
exactly as before.
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .strategies.registry import get_strategy, load_selected
from .td_params import (
    PARAM_META,
    WEIGHT_KEYS,
    load_td_params,
    save_td_params,
    td_params_path,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


_PAGE_HTML = _load_template("td_params_page.html")

_GROUP_TITLES = {
    "td": "① TD 算法参数（DeMark 标准）",
    "weights": "② 评分权重（合计必须 = 1.0）",
    "strategy": "③ 策略层规则",
}


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Page ────────────────────────────────────────────────────────────────


def _field_html(key: str, value: object) -> str:
    """Render one editable field (number input or checkbox)."""
    meta = PARAM_META[key]
    label = meta.get("label", key)
    hint = meta.get("hint", "")
    std = meta.get("std", "")
    if meta.get("type") == "bool":
        checked = ' checked="checked"' if value else ""
        return (
            f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
            f'<input type="checkbox" id="{key}" name="{key}"{checked}>'
            f'<span class="f-std">默认 {std}</span>'
            f'<span class="f-hint">{hint}</span></div>'
        )
    lo, hi, step = meta["min"], meta["max"], meta.get("step", 1)
    step_attr = str(step) if not isinstance(lo, int) else "1"
    return (
        f'<div class="field"><label class="f-label" for="{key}">{label}</label>'
        f'<input type="number" id="{key}" name="{key}" value="{value}" '
        f'min="{lo}" max="{hi}" step="{step_attr}">'
        f'<span class="f-std">默认 {std} · 范围 {lo}–{hi}</span>'
        f'<span class="f-hint">{hint}</span></div>'
    )


def _group_html(group: str, params: dict) -> str:
    fields = "".join(
        _field_html(k, params[k])
        for k in PARAM_META
        if PARAM_META[k].get("group") == group
    )
    extra = ""
    if group == "weights":
        total = sum(float(params[k]) for k in WEIGHT_KEYS)
        badge = "ok" if abs(total - 1.0) <= 1e-6 else "err"
        extra = (
            f'<div class="weight-total {badge}" id="weight-total">'
            f'权重合计：{total:.3f}（需 = 1.000）</div>'
        )
    return f'<div class="card"><h3>{_GROUP_TITLES[group]}</h3>{extra}{fields}</div>'


def _strategy_banner() -> str:
    """Show which strategy the parameter set applies to.

    The registry stores the WebUI label per strategy; two TD variants share
    the same param defaults today, but the banner makes the binding explicit
    so switching strategy on the selection page is visible here too.
    """
    try:
        name = load_selected()
        label = get_strategy(name).label
    except Exception:
        name, label = "td_sequential", "TD Sequential（原版）"
    return (
        f'<div class="banner strategy">🎯 当前策略：{label}'
        '——以下参数仅应用于该策略，按策略独立保存'
        '（可在「📈 策略选择」页切换）</div>'
    )


def _render_page(params: dict, message: str = "") -> str:
    custom = td_params_path().is_file()
    banner = (
        '<div class="banner custom">⚙️ 已自定义参数（td_params.json）——'
        '如需恢复默认，删除该文件或把值改回默认</div>'
        if custom
        else '<div class="banner default">默认参数（= 旧版硬编码行为，零变化）</div>'
    )
    msg = (
        f'<div class="banner msg" id="save-msg">{message}</div>'
        if message
        else '<div class="banner msg hidden" id="save-msg"></div>'
    )
    groups = "".join(_group_html(g, params) for g in ("td", "weights", "strategy"))
    return (
        _PAGE_HTML.replace("{banner}", _strategy_banner() + banner)
        .replace("{msg}", msg)
        .replace("{groups}", groups)
        .replace("{saved_at}", "")
    )


async def td_params_page(request: Request) -> HTMLResponse:
    """GET /config/td-params — editable parameter cards."""
    params = load_td_params()
    return HTMLResponse(_render_page(params))


# ── Save ────────────────────────────────────────────────────────────────


async def td_params_save(request: Request) -> JSONResponse:
    """POST /config/td-params — validate + persist the full parameter set."""
    data = await _body(request)
    if data is None:
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    result = save_td_params(data)
    if not result.get("ok"):
        return JSONResponse({"ok": False, "error": result.get("error", "保存失败")},
                            status_code=400)
    return JSONResponse({"ok": True, "message": "TD 参数已保存并即时生效",
                         "params": result.get("params")})


# ── Route registration helper ───────────────────────────────────────────


def register_td_params_routes(app, gatekeeper) -> None:
    """Register TD parameter routes on the FastAPI app.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """
    app.get("/config/td-params")(td_params_page)
    app.post("/config/td-params")(td_params_save)
