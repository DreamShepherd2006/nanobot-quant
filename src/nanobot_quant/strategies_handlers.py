"""Strategy selection — WebUI 策略选择页.

Registered conditionally by gatekeeper_routes.py (like mode_handlers).
Selection persisted to {data_root}/legion/strategy.json and takes effect
immediately on the next quant-route tool call (see registry.resolve_signal_fn).
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from nanobot_quant.strategies.registry import (
    get_strategy,
    list_strategies,
    load_selected,
    save_selected,
)

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


_PAGE_HTML = _load_template("strategy_page.html")


def _strategy_path(gatekeeper) -> str:
    """Resolve strategy.json path from gatekeeper's data_root."""
    return os.path.join(gatekeeper._platform.data_root, "legion", "strategy.json")


def _card_html(spec, current: str) -> str:
    active = " active" if spec.name == current else ""
    chips = []
    if spec.variant_of:
        chips.append(f'<span class="chip">变体：{spec.variant_of}</span>')
    chips.append(f'<span class="chip">数据源：{spec.data_source}</span>')
    chips.append(f'<span class="chip code">{spec.name}</span>')
    return (
        f'<div class="strategy-card{active}" onclick="switchStrategy(\'{spec.name}\')">'
        f'<div class="icon">📈</div>'
        f'<h2>{spec.label}</h2>'
        f'<p>{spec.description}</p>'
        f'<div class="meta">{"".join(chips)}</div>'
        f'<span class="badge">✓ 当前</span>'
        f'</div>'
    )


def _render_page(current: str) -> str:
    cards = "".join(_card_html(s, current) for s in list_strategies())
    current_spec = get_strategy(current)
    return (
        _PAGE_HTML
        .replace("{cards}", cards)
        .replace("{current_label}", f"{current_spec.label}（{current}）")
    )


# ── Route registration ──────────────────────────────────────────────────


def register_strategy_routes(app, gatekeeper) -> None:
    """Register /config/strategy page and selection endpoint.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """

    async def _page(request: Request):
        _u = request.session.get("user")
        if not _u:
            return RedirectResponse("/")
        if not gatekeeper._platform.is_commander(_u):
            return HTMLResponse(
                "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 仅 Commander 可访问</h3>",
                status_code=403,
            )
        return HTMLResponse(_render_page(load_selected(_strategy_path(gatekeeper))))

    async def _save(request: Request):
        _u = request.session.get("user")
        if not _u:
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        if not gatekeeper._platform.is_commander(_u):
            return JSONResponse({"ok": False, "error": "仅 Commander 可操作"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        name = body.get("strategy", "").strip()
        old = load_selected(_strategy_path(gatekeeper))
        try:
            save_selected(_strategy_path(gatekeeper), name)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        print(f"[gatekeeper] 📈 策略选择: {old} → {name}", flush=True)
        return JSONResponse({"ok": True, "old": old, "strategy": name})

    app.add_route("/config/strategy", _page, methods=["GET"])
    app.add_route("/config/strategy", _save, methods=["POST"])
