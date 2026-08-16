"""Live trading toggle — WebUI checkbox controlling real on-chain execution.

Registered conditionally by gatekeeper_routes.py (like mode_handlers).
State is persisted to {data_root}/credentials/live.json, alongside okx.json.

Security semantics (AND gate):
    effective_live = agent_requested_live AND webui_live_toggle

The WebUI toggle is the master switch: when it is OFF, no agent can
trigger real on-chain execution regardless of what it passes as `live`.
"""

from __future__ import annotations

import json
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


def _live_path(gatekeeper) -> str:
    """Resolve live.json path from gatekeeper's data_root (credentials dir)."""
    return os.path.join(gatekeeper._platform.data_root, "credentials", "live.json")


def _read_live(gatekeeper) -> bool:
    """Read current live toggle, default False (dry-run, no execution)."""
    path = _live_path(gatekeeper)
    try:
        with open(path) as f:
            data = json.load(f)
        return bool(data.get("live", False))
    except Exception:
        return False


def _write_live(gatekeeper, live: bool) -> None:
    path = _live_path(gatekeeper)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"live": bool(live)}, f)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


_LIVE_PAGE = """\
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>实盘交易开关</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f5f5f5; color: #333; padding: 2rem; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
  .sub { color: #666; font-size: 0.875rem; margin-bottom: 1.5rem; }
  .card { background: #fff; border-radius: 12px; padding: 1.5rem;
           box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
  .switch-row { display: flex; align-items: center; gap: 1rem; }
  .switch-label { flex: 1; }
  .switch-label h2 { font-size: 1.1rem; }
  .switch-label p { font-size: 0.85rem; color: #666; margin-top: 0.25rem; }
  .switch { position: relative; display: inline-block; width: 52px; height: 28px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; inset: 0; background: #ccc;
             border-radius: 28px; transition: background 0.3s; }
  .slider:before { content: ""; position: absolute; height: 22px; width: 22px; left: 3px;
                    top: 3px; background: #fff; border-radius: 50%; transition: transform 0.3s; }
  .switch input:checked + .slider { background: #1e8e3e; }
  .switch input:checked + .slider:before { transform: translateX(24px); }
  .status { display: inline-block; border-radius: 20px; padding: 3px 14px;
             font-size: 0.8rem; font-weight: 600; }
  .status.on { background: #e6f4ea; color: #1e8e3e; }
  .status.off { background: #f1f3f4; color: #666; }
  .warn { background: #fef7e0; border: 1px solid #f9ab00; border-radius: 8px;
           padding: 0.9rem 1.1rem; margin-top: 1.25rem; font-size: 0.85rem;
           color: #7a4f01; line-height: 1.6; display: none; }
  .warn.show { display: block; }
  .info { background: #f8f9fa; border-radius: 8px; padding: 1rem 1.25rem; margin-top: 1.25rem;
           font-size: 0.82rem; color: #555; line-height: 1.7; }
  .info code { background: #eef; padding: 1px 5px; border-radius: 3px; font-size: 0.8rem; }
  .btn { display: inline-block; margin-top: 1.5rem; padding: 0.55rem 1.4rem; border: none;
          border-radius: 8px; background: #1a73e8; color: #fff; font-size: 0.95rem;
          cursor: pointer; }
  .btn:hover { background: #1765cc; }
  .back { display: inline-block; margin-top: 1.25rem; color: #1a73e8; text-decoration: none;
           font-size: 0.9rem; margin-left: 1rem; }
  .back:hover { text-decoration: underline; }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem; padding: 0.75rem 1.25rem;
            border-radius: 8px; color: #fff; font-size: 0.9rem; z-index: 999;
            opacity: 0; transition: opacity 0.3s; }
  .toast.ok { background: #1e8e3e; }
  .toast.err { background: #c5221f; }
  .toast.show { opacity: 1; }
</style>
</head>
<body>
<h1>⚡ 实盘交易开关</h1>
<p class="sub">主开关：开启后 agent 传入 <code>live=true</code> 才可触发真实交易；关闭时订单不实际执行（dry-run：仅风控校验 + 生成订单建议）。</p>

<div class="card">
  <div class="switch-row">
    <div class="switch-label">
      <h2>允许实盘交易 (Live)</h2>
      <p>开启后 execute_signal 按当前执行通道真实下单（DEX=链上 swap / CEX=Gate spot）</p>
    </div>
    <span id="status" class="status {status_cls}">{status_text}</span>
    <label class="switch">
      <input type="checkbox" id="live-toggle"{checked}>
      <span class="slider"></span>
    </label>
  </div>

  <div id="warn" class="warn">⚠️ <strong>实盘交易已开启</strong> — execute_signal 收到的信号将按当前执行通道（DEX 链上 swap / CEX Gate spot）真实下单。请确保钱包地址与凭证正确，且仓位受风控约束。</div>
</div>

<div class="info">
  <p><strong>安全模型</strong>：此开关是实盘交易的<b>总闸门</b>。即使 agent 在调用中传 <code>live=true</code>，只要此开关为关，订单仍然不会实际执行（dry-run：仅风控校验 + 生成订单建议，不成交、不记账）。</p>
  <p><strong>配置存储</strong>：<code>{live_path}</code>（与 API 凭证同目录，重启保留）。</p>
</div>

<button class="btn" id="save-btn">💾 保存</button>
<a class="back" href="javascript:history.back()">← 返回</a>

<div id="toast" class="toast"></div>

<script>
const toggle = document.getElementById('live-toggle');
const warn = document.getElementById('warn');
const statusEl = document.getElementById('status');

function refreshUI() {
  const on = toggle.checked;
  warn.classList.toggle('show', on);
  statusEl.textContent = on ? '● 实盘已开启' : '○ 实盘关闭（不成交）';
  statusEl.className = 'status ' + (on ? 'on' : 'off');
}
toggle.addEventListener('change', refreshUI);
refreshUI();

document.getElementById('save-btn').addEventListener('click', async () => {
  const live = toggle.checked;
  const resp = await fetch('/config/live', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({live})
  });
  const data = await resp.json();
  if (data.ok) {
    toast('✅ 已保存：' + (live ? '实盘开启' : '实盘关闭（dry-run）'), true);
  } else {
    toast('❌ ' + (data.error || '保存失败'), false);
  }
});

function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err') + ' show';
  setTimeout(() => t.classList.remove('show'), 2500);
}
</script>
</body>
</html>"""


def register_live_routes(app, gatekeeper):
    """Register /config/live page and toggle endpoint."""

    async def _live_page(request: Request):
        _u = request.session.get("user")
        if not _u:
            return RedirectResponse("/")
        if not gatekeeper._platform.is_commander(_u):
            return HTMLResponse(
                "<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 仅 Commander 可访问</h3>",
                status_code=403,
            )
        live = _read_live(gatekeeper)
        html = (
            _LIVE_PAGE
            .replace("{checked}", " checked" if live else "")
            .replace("{status_cls}", "on" if live else "off")
            .replace("{status_text}", "● 实盘已开启" if live else "○ 实盘关闭（不成交）")
            .replace("{live_path}", _live_path(gatekeeper))
        )
        return HTMLResponse(html)

    async def _live_toggle(request: Request):
        _u = request.session.get("user")
        if not _u:
            return JSONResponse({"ok": False, "error": "请先登录"}, status_code=401)
        if not gatekeeper._platform.is_commander(_u):
            return JSONResponse({"ok": False, "error": "仅 Commander 可操作"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "无效请求"}, status_code=400)
        live = bool(body.get("live", False))
        old = _read_live(gatekeeper)
        _write_live(gatekeeper, live)
        print(f"[gatekeeper] ⚡ 实盘开关: {'ON' if old else 'OFF'} → {'ON' if live else 'OFF'}", flush=True)
        return JSONResponse({"ok": True, "old": old, "live": live})

    app.add_route("/config/live", _live_page, methods=["GET"])
    app.add_route("/config/live", _live_toggle, methods=["POST"])
