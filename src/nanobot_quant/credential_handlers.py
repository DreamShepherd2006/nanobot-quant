"""API Credential management handlers — serve pages and process form submissions.

Used by gatekeeper to register /config/credentials routes.
"""

from __future__ import annotations

import json
import os
from html import escape as html_escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .credential_registry import (
    discover, read_credential, write_credential, delete_credential,
    is_configured, CredentialSpec, FieldSpec,
)

# ── HTML templates ────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_template(filename: str) -> str:
    with open(os.path.join(_HERE, filename), encoding="utf-8") as f:
        return f.read()


_DETAIL_HTML = _load_template("credential_detail.html")
_LIST_HTML = _load_template("credential_page.html")


# ── Page render helpers ───────────────────────────────────────────


def _render_cards(specs: dict[str, CredentialSpec]) -> str:
    """Render credential type cards for the list page."""
    cards: list[str] = []
    for name, spec in specs.items():
        configured = is_configured(name)
        status_class = "status-configured" if configured else "status-pending"
        status_text = "✅ 已配置" if configured else "⚠️ 未配置"
        button = (
            f'<a href="/config/credentials/{html_escape(name)}"><button class="btn-outline">✏️ 编辑</button></a>'
            if configured
            else f'<a href="/config/credentials/{html_escape(name)}"><button class="btn-primary">⚙️ 配置</button></a>'
        )
        cards.append(f"""\
<div class="card">
  <div class="card-header">
    <span class="icon">{html_escape(spec.icon)}</span>
    <span class="name">{html_escape(spec.display)}</span>
    <span class="status {status_class}">{status_text}</span>
  </div>
  <div class="card-desc">{html_escape(spec.description)}</div>
  <div class="card-actions">
    {button}
    {f'<a href="{html_escape(spec.docs_url)}" target="_blank" rel="noopener"><button class="btn-outline">📖 获取凭证</button></a>' if spec.docs_url else ""}
  </div>
</div>""")
    if not cards:
        return '<div class="empty">暂无可用凭证类型。<br><small>安装业务插件后自动显示。</small></div>'
    return "\n".join(cards)


def _render_detail_form(spec: CredentialSpec) -> str:
    """Render credential edit form with current values pre-filled."""
    current = read_credential(spec.name)

    fields_html_parts: list[str] = []
    for f in spec.fields:
        val = html_escape(current.get(f.name, ""))
        required_attr = 'required' if f.required else ''
        placeholder = html_escape(f.placeholder)
        fields_html_parts.append(f"""\
  <div class="form-group">
    <label for="{html_escape(f.name)}">{html_escape(f.label)}</label>
    <input id="{html_escape(f.name)}" name="{html_escape(f.name)}" type="{html_escape(f.type)}"
           value="{val}" placeholder="{placeholder}" {required_attr}>
  </div>""")

    docs_block = ""
    if spec.docs_url:
        docs_block = f'<div class="note">📖 还没有凭证？<a href="{html_escape(spec.docs_url)}" target="_blank" rel="noopener">点击此处获取 API Key</a></div>'

    html = _DETAIL_HTML
    html = html.replace("{display}", html_escape(spec.display))
    html = html.replace("{description}", html_escape(spec.description))
    html = html.replace("{docs_block}", docs_block)
    html = html.replace("{fields_html}", "\n".join(fields_html_parts))
    html = html.replace("CRED_NAME_PLACEHOLDER", json.dumps(spec.name))
    return html


# ── Route handlers ────────────────────────────────────────────────


async def credential_list(request: Request) -> HTMLResponse:
    """GET /config/credentials — list all credential types."""
    specs = discover()
    cards_html = _render_cards(specs)
    html = _LIST_HTML.replace("{cards_html}", cards_html)
    return HTMLResponse(html)


async def credential_detail(request: Request) -> HTMLResponse:
    """GET /config/credentials/{name} — edit credential form."""
    name = request.path_params.get("name", "")
    specs = discover()
    spec = specs.get(name)
    if not spec:
        return HTMLResponse("<h2>未知的凭证类型</h2>", status_code=404)
    html = _render_detail_form(spec)
    return HTMLResponse(html)


async def credential_save(request: Request) -> JSONResponse:
    """POST /config/credentials/{name}/save — save credential data."""
    name = request.path_params.get("name", "")
    specs = discover()
    if name not in specs:
        return JSONResponse({"ok": False, "error": "未知的凭证类型"}, status_code=404)
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"}, status_code=400)
    write_credential(name, data)
    return JSONResponse({"ok": True})


async def credential_delete(request: Request) -> JSONResponse:
    """DELETE /config/credentials/{name} — delete stored credential."""
    name = request.path_params.get("name", "")
    specs = discover()
    if name not in specs:
        return JSONResponse({"ok": False, "error": "未知的凭证类型"}, status_code=404)
    deleted = delete_credential(name)
    if deleted:
        return JSONResponse({"ok": True})
    else:
        return JSONResponse({"ok": False, "error": "凭证文件不存在或删除失败"})


# ── Route registration helper ─────────────────────────────────────


def register_credential_routes(app, gatekeeper) -> None:
    """Register all credential management routes on the FastAPI app.

    Called by gatekeeper_routes.py during app creation.
    """
    # Initialize credential storage at platform data_root
    from .credential_registry import init_storage
    init_storage(gatekeeper._platform.data_root)

    try:
        specs = discover()
    except Exception:
        return  # nanobot_quant not installed or import failed

    if not specs:
        return

    # Import the okx_spec to trigger registration
    try:
        from . import okx_spec  # noqa: F401
    except ImportError:
        pass

    # Re-discover after importing specs
    specs = discover()
    if not specs:
        return

    app.get("/config/credentials")(credential_list)
    app.get("/config/credentials/{name}")(credential_detail)
    app.post("/config/credentials/{name}/save")(credential_save)
    app.delete("/config/credentials/{name}")(credential_delete)
