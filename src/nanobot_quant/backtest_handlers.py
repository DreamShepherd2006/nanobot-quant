"""Backtest WebUI page (Commander only).

GET  /config/backtest        — page (form + run history + result viewer)
POST /config/backtest/start  — start a driver-engine backtest (async run_id)
GET  /config/backtest/result — poll a run outcome (run_id)
GET  /config/backtest/runs   — recent runs from the persisted audit dir

The page drives the Step 3 replay driver (``backtest.driver.BacktestDriver``)
through the same async run_id + poll contract as the MCP tool
(``tools.tools_backtest``): start returns immediately, results are persisted
to ``{data_root}/legion/backtests/<run_id>.json`` by the background thread.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

_HERE = os.path.dirname(os.path.abspath(__file__))

_PAGE_HTML: str = ""


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    """Return (error_message_or_None, ok)."""
    _u = request.session.get("user")
    if not _u:
        return "请先登录", False
    if not gatekeeper._platform.is_commander(_u):
        return "仅 Commander 可操作", False
    return None, True


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


# ── Data helpers ─────────────────────────────────────────────────────────

#: 回测覆盖字段 → 全局回退来源。
#: (来源, 键, 默认值)：
#:   "td"    = load_td_params 全局 TD 参数（entry/exit 阈值，策略选择页设置）
#:   "flat"  = exec_params 平铺全局键（min_hold_bars/stop_loss_pct/take_profit_pct）
#:   "scene" = 场景默认值（DEFAULT_SCENES，策略类默认回退）
_OV_FIELDS: dict[str, tuple] = {
    "entry_setup": ("td", "entry_setup", 9),
    "entry_countdown": ("td", "entry_countdown", 13),
    "cd_entry_setup_gap": ("scene", None, 5),
    "exit_setup": ("td", "exit_setup", 9),
    "exit_countdown": ("td", "exit_countdown", 13),
    "min_hold_bars": ("flat", "min_hold_bars", 10),
    "exit_order": ("scene", None, "fifo"),
    "stop_loss_pct": ("flat", "stop_loss_pct", 0.10),
    "take_profit_pct": ("flat", "take_profit_pct", 0.0),
    "sell_only_profit_high": ("scene", None, 0.0),
    "sell_only_profit_low": ("scene", None, 0.002),
    "momentum_exit": ("scene", None, True),
    "cd_stall_n": ("scene", None, 3),
    "td_sell_all": ("scene", None, False),
    "cd_exit_min_profit": ("scene", None, 0.0),
    "cd_exit_all": ("scene", None, True),
    "td_start_slot": ("scene", None, 1),
    "min_account_value": ("scene", None, 0),
    # 贝叶斯闸门（2026-08-31，回测侧）：flat 全局（exec_params）。
    # 默认关 → 回测与原版完全一致；阈值 0.45=red-only / 0.20=yellow+red。
    "gate_enabled": ("flat", "gate_enabled", False),
    "gate_red_min": ("flat", "gate_red_min", 0.45),
}


def _scenes() -> dict:
    """exec_params scenes (high/mid/low) 完整配置 + 每键实际生效值。

    返回结构：{name: {enabled, sleeptime, symbols, batches, sub_accounts,
    <全部场景参数字段>, "_effective": {key: 生效值}}}——_effective 为
    场景值回退全局（td_params/平铺/默认）后的最终生效值，供回测页覆盖
    输入框 placeholder 显示「对应场景值」，方便对比。
    """
    try:
        from nanobot_quant.exec_params import load_exec_params

        params = load_exec_params()
    except Exception:  # noqa: BLE001 — page must render even when config broke
        params = {}
    scenes = params.get("scenes") or {}
    try:
        from nanobot_quant.td_params import load_td_params

        td = load_td_params("td_sequential") or {}
    except Exception:  # noqa: BLE001
        td = {}
    result: dict[str, dict] = {}
    for name in ("high", "mid", "low"):
        cfg = scenes.get(name) or {}
        entry = dict(cfg)
        entry.setdefault("enabled", False)
        entry.setdefault("sleeptime", "")
        entry.setdefault("symbols", [])
        entry.setdefault("batches", 0)
        entry.setdefault("sub_accounts", [])
        eff: dict = {}
        for key, (src, src_key, default) in _OV_FIELDS.items():
            v = entry.get(key)
            if v is None or v == "":
                if src == "td":
                    v = td.get(src_key, default)
                elif src == "flat":
                    v = params.get(src_key, default)
                else:
                    v = default
            eff[key] = v
        entry["_effective"] = eff
        result[name] = entry
    return result


def _symbol_candidates() -> list[str]:
    """tokens.json registered symbols (minus stablecoins) as candidates."""
    try:
        from nanobot_quant.tokens_store import load_token_symbols

        syms = load_token_symbols()
    except Exception:  # noqa: BLE001
        syms = []
    return [s for s in syms if s.upper() not in ("USDC", "USDT", "USDG")]


def _recent_runs(limit: int = 20) -> list[dict]:
    """Recent persisted backtest runs (newest first)."""
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        d = backtests_dir()
    except Exception:  # noqa: BLE001
        return []
    runs: list[dict] = []
    if not d.is_dir():
        return runs
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        runs.append(
            {
                "run_id": p.stem,
                "status": payload.get("status", "unknown"),
                "ts": p.stat().st_mtime,
            }
        )
    return runs


# ── Page rendering ───────────────────────────────────────────────────────

def _render_page(scenes: dict, symbols: list[str]) -> str:
    return (
        _PAGE_HTML.replace("__SCENES__", json.dumps(scenes, ensure_ascii=False)).replace(
            "__SYMBOLS__", json.dumps(symbols, ensure_ascii=False)
        )
    )


_OV_INT = {
    "entry_setup", "entry_countdown", "cd_entry_setup_gap",
    "exit_setup", "exit_countdown", "min_hold_bars",
    "cd_stall_n", "td_start_slot",
}
_OV_FLOAT = {
    "sell_only_profit_high", "sell_only_profit_low", "cd_exit_min_profit",
    "stop_loss_pct", "take_profit_pct", "min_account_value",
}
_OV_BOOL = {"momentum_exit", "td_sell_all", "cd_exit_all"}


def _coerce_overrides(raw) -> dict:
    """回测覆盖参数归一化（空串/None 忽略；int/float/bool 按字段转换）。
    仅作用于本次回测，绝不回写 exec_params（2026-08-30 拍板）。"""
    out = {}
    for k, v in (raw or {}).items():
        if v is None or v == "" or v == "null":
            continue
        try:
            if k in _OV_INT:
                out[k] = int(v)
            elif k in _OV_FLOAT:
                out[k] = float(v)
            elif k in _OV_BOOL:
                out[k] = v is True or str(v).lower() in ("1", "true", "on", "yes")
            else:
                out[k] = v  # exit_order 等字符串透传
        except (TypeError, ValueError):
            raise ValueError(f"回测覆盖参数 {k}={v!r} 非法")
    return out


def register_backtest_routes(app, gatekeeper) -> None:
    """Register backtest page routes on the FastAPI app.

    Called by nanobot-legion gatekeeper_routes.py during app creation.
    """
    global _PAGE_HTML
    if not _PAGE_HTML:
        _PAGE_HTML = _load_template("backtest_page.html")

    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(
                f"<h3 style='text-align:center;margin-top:60px;color:#e74c3c;'>🔒 {err}</h3>",
                status_code=403 if "Commander" in err else 401,
            )
        return HTMLResponse(
            _render_page(_scenes(), _symbol_candidates()), status_code=200
        )

    async def _start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        data = await _body(request)
        if data is None:
            gatekeeper._log("[BACKTEST-PAGE] start 请求体无效（非 JSON）")
            return JSONResponse({"ok": False, "error": "无效的 JSON 数据"}, status_code=400)
        scene = data.get("scene") or "mid"
        symbols = data.get("symbols") or []
        try:
            overrides = _coerce_overrides(data.get("overrides"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        gatekeeper._log(
            f"[BACKTEST-PAGE] 启动请求 scene={scene} symbols={symbols} "
            f"range={data.get('start') or '拉满'}→{data.get('end') or '现在'} "
            f"initial_quote={data.get('initial_quote')} batches={data.get('batches')} "
            f"slippage={data.get('slippage')} fixed_amount={data.get('fixed_amount')} "
            f"overrides={overrides}"
        )
        if not symbols:
            return JSONResponse({"ok": False, "error": "至少选择一个标的"}, status_code=400)
        try:
            from nanobot_quant.tools.tools_backtest import run_backtest

            resp = run_backtest(
                engine="driver",
                scene=scene,
                symbols=symbols,
                start=data.get("start") or None,
                end=data.get("end") or None,
                initial_quote=float(data.get("initial_quote") or 1000),
                batches=int(data["batches"]) if data.get("batches") else None,
                slippage=float(data["slippage"]) if data.get("slippage") else None,
                fixed_amount=float(data["fixed_amount"])
                if data.get("fixed_amount")
                else None,
                overrides=overrides,
            )
        except Exception as exc:  # noqa: BLE001
            gatekeeper._log(f"[BACKTEST-PAGE] 启动回测异常: {exc}")
            return JSONResponse(
                {"ok": False, "error": f"启动回测失败: {exc}"}, status_code=400
            )
        if resp.get("error"):
            gatekeeper._log(f"[BACKTEST-PAGE] run_backtest 拒绝: {resp['error']}")
            return JSONResponse({"ok": False, "error": resp["error"]}, status_code=400)
        gatekeeper._log(
            f"📈 回测启动 scene={scene} symbols={symbols} run_id={resp['run_id']}"
        )
        return JSONResponse({"ok": True, "run_id": resp["run_id"]})

    async def _result(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        run_id = request.query_params.get("run_id", "")
        if not run_id:
            return JSONResponse({"ok": False, "error": "缺少 run_id"}, status_code=400)
        from nanobot_quant.tools.tools_backtest import get_backtest_result

        return JSONResponse(get_backtest_result(run_id))

    async def _runs(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        return JSONResponse({"ok": True, "runs": _recent_runs()})

    app.add_route("/config/backtest", _page, methods=["GET"])
    app.add_route("/config/backtest/start", _start, methods=["POST"])
    app.add_route("/config/backtest/result", _result, methods=["GET"])
    app.add_route("/config/backtest/runs", _runs, methods=["GET"])
