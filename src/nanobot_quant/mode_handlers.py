"""Trading mode management — WebUI toggle for Quant vs Research.

Registered conditionally by gatekeeper_routes.py (like credential_handlers).
Mode is persisted to {data_root}/legion/mode.json.
"""

from __future__ import annotations

import json
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


def _mode_path(gatekeeper) -> str:
    """Resolve mode.json path from gatekeeper's data_root."""
    return os.path.join(gatekeeper._platform.data_root, "legion", "mode.json")


def _read_mode(gatekeeper) -> str:
    """Read current mode, default 'quant'."""
    path = _mode_path(gatekeeper)
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("mode", "quant")
    except Exception:
        return "quant"


def _write_mode(gatekeeper, mode: str) -> None:
    path = _mode_path(gatekeeper)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"mode": mode}, f)


_MODE_PAGE = """\
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>交易模式</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f5f5f5; color: #333; padding: 2rem; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
  .sub { color: #666; font-size: 0.875rem; margin-bottom: 1.5rem; }
  .modes { display: flex; gap: 1rem; flex-wrap: wrap; }
  .mode-card { flex: 1; min-width: 200px; background: #fff; border-radius: 12px;
                padding: 1.5rem; text-align: center; cursor: pointer;
                box-shadow: 0 1px 3px rgba(0,0,0,0.08);
                transition: all 0.2s; border: 2px solid transparent; }
  .mode-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); }
  .mode-card.active { border-color: #1a73e8; background: #f0f6ff; }
  .mode-card .icon { font-size: 2.5rem; margin-bottom: 0.75rem; }
  .mode-card h2 { font-size: 1.1rem; margin-bottom: 0.4rem; }
  .mode-card p { font-size: 0.85rem; color: #666; line-height: 1.5; }
  .badge { display: inline-block; background: #1e8e3e; color: #fff; border-radius: 20px;
            padding: 2px 12px; font-size: 0.75rem; margin-top: 0.5rem; }
  .mode-card:not(.active) .badge { display: none; }
  .back { display: inline-block; margin-top: 1.5rem; color: #1a73e8; text-decoration: none;
           font-size: 0.9rem; }
  .back:hover { text-decoration: underline; }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem; padding: 0.75rem 1.25rem;
            border-radius: 8px; color: #fff; font-size: 0.9rem; z-index: 999;
            opacity: 0; transition: opacity 0.3s; }
  .toast.ok { background: #1e8e3e; }
  .toast.err { background: #c5221f; }
  .toast.show { opacity: 1; }
  .info { background: #fff; border-radius: 8px; padding: 1rem 1.25rem; margin-top: 1.5rem;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 0.85rem; color: #555;
          line-height: 1.6; }
  .info code { background: #f1f3f4; padding: 1px 5px; border-radius: 3px; font-size: 0.82rem; }
</style>
</head>
<body>
<h1>⚙️ 交易模式</h1>
<p class="sub">选择 Neo 的分析决策模式。影响 "分析" / "评估" 类指令的路由。</p>

<div class="modes">
  <div class="mode-card{mode_quant}" onclick="switchMode('quant')">
    <div class="icon">📊</div>
    <h2>Quant 模式</h2>
    <p>TD Sequential 确定性信号<br>公式驱动，无 LLM 偏见</p>
    <span class="badge">✓ 当前</span>
  </div>
  <div class="mode-card{mode_research}" onclick="switchMode('research')">
    <div class="icon">🧠</div>
    <h2>Research 模式</h2>
    <p>VT Swarm 投资委员会<br>多空辩论，LLM 驱动推理</p>
    <span class="badge">✓ 当前</span>
  </div>
</div>

<div class="info">
  <p><strong>当前模式</strong>：{current_label}</p>
  <p><strong>影响</strong>：Neo 收到「分析 BTC」「评估 AAPL」等指令时，自动路由到对应 Agent（<code>quant</code> 或 <code>vt_research</code>）。</p>
  <p><strong>手动调用</strong>：无论当前模式，可直接指定 agent（如 <code>@quant 分析 BTC</code>）。</p>
</div>

<a class="back" href="javascript:history.back()">← 返回</a>

<div id="toast" class="toast"></div>

<script>
async function switchMode(mode) {
  const resp = await fetch('/config/mode', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode})
  });
  const data = await resp.json();
  if (data.ok) {
    toast('✅ 已切换到 ' + (mode === 'quant' ? 'Quant 模式' : 'Research 模式'), true);
    setTimeout(() => location.reload(), 600);
  } else {
    toast('❌ ' + data.error, false);
  }
}

function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err') + ' show';
  setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>"""


def register_mode_routes(app, gatekeeper):
    """Register /config/mode page and toggle endpoint."""

    async def _mode_page(request: Request):
        _u = request.session.get("user")
        if not _u:
            return RedirectResponse("/")
        if not gatekeeper._platform.is_commander(_u):
            return HTMLResponse(
                "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 仅 Commander 可访问</h3>",
                status_code=403,
            )
        mode = _read_mode(gatekeeper)
        labels = {"quant": "📊 Quant 模式（TD Sequential）", "research": "🧠 Research 模式（VT Swarm）"}
        html = (
            _MODE_PAGE.replace("{current_label}", labels.get(mode, mode))
            .replace("{mode_quant}", ' active' if mode == "quant" else '')
            .replace("{mode_research}", ' active' if mode == "research" else '')
        )
        return HTMLResponse(html)

    async def _mode_toggle(request: Request):
        _u = request.session.get("user")
        if not _u:
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        if not gatekeeper._platform.is_commander(_u):
            return JSONResponse({"ok": False, "error": "仅 Commander 可操作"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        mode = body.get("mode", "").strip()
        if mode not in ("quant", "research"):
            return JSONResponse({"ok": False, "error": "无效模式（仅支持 quant / research）"}, status_code=400)
        old = _read_mode(gatekeeper)
        _write_mode(gatekeeper, mode)
        print(f"[gatekeeper] 📊 交易模式: {old} → {mode}", flush=True)
        return JSONResponse({"ok": True, "old": old, "mode": mode})

    app.add_route("/config/mode", _mode_page, methods=["GET"])
    app.add_route("/config/mode", _mode_toggle, methods=["POST"])
