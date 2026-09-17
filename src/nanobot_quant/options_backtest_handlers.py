"""期权回测 WebUI 后端（Commander only）。

页面本身挂在 ``/config/backtest`` 的「🟤 期权回测」分栏（由
``backtest_handlers`` 统一渲染），这里只提供该分栏需要的三个数据端点：

POST /config/backtest/options/start   — 起一轮期权回测（异步 run_id）
GET  /config/backtest/options/result  — 轮询结果（run_id）
GET  /config/backtest/options/runs    — 历史记录（只列期权 run）

与现货回测共用同一套 run_id + 轮询契约，结果同样落在
``{data_root}/legion/backtests/<run_id>.json``。覆盖参数只作用于本次回测，
**绝不回写** option_params.json（沿用 2026-08-30 拍板的口径）。

引擎入口是 MCP 工具层 ``tools.tools_backtest.run_backtest(engine="options")``
—— 与 WebUI 共用一条代码路径，不存在「页面跑的和工具跑的不一样」。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from nanobot_quant.backtest_handlers import _authorized, _body

# OKX 期权市场支持的粒度（无 8H；7D/30D 在期权线不常用故不列）
OPT_TIMESTEPS = ("1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D")

# 兜底家族列表（策略配置读不到时用）
FALLBACK_FAMILIES = ("SOL-USD_UM", "BTC-USD_UM", "ETH-USD_UM", "XAU-USD_UM")


def available_families() -> list[str]:
    """页面下拉的家族列表 = 策略配置里的家族 ∪ 已知家族。

    策略配置（option_params.json → ``live.strategy.families``）优先，
    保证「页面上能选的」和「自动循环会跑的」对得上。
    """
    out: list[str] = []
    try:
        from nanobot_quant.okx_options_live import _strategy_params, live_config

        fams = (_strategy_params(live_config()) or {}).get("families") or []
        out.extend(str(f).upper().strip() for f in fams if str(f).strip())
    except Exception:  # noqa: BLE001 —— 配置缺失不阻塞页面
        pass
    for f in FALLBACK_FAMILIES:
        if f not in out:
            out.append(f)
    return out


def _opt_runs(limit: int = 20) -> list[dict]:
    """历史 run 里只挑期权回测（run_id 前缀 ``opt-``）。

    现货 run 由 ``run_id = YYYYMMDD-HHMMSS-hex``；期权 run 在起之前重命名成
    ``opt-<原 id>``（见 ``tools_backtest._auto_backtest_options`` 的调用方），
    避免两套引擎的结果混在同一个历史列表里。
    """
    from nanobot_quant.backtest_handlers import _recent_runs

    return [r for r in _recent_runs(limit=limit * 2) if r["run_id"].startswith("opt-")][
        :limit
    ]


def register_options_backtest_routes(app, gatekeeper) -> None:  # noqa: ANN001
    """挂期权回测的三个端点（页面由 backtest_handlers 渲染）。"""

    async def _start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        data = await _body(request)
        if data is None:
            gatekeeper._log("[OPT-BT] start 请求体无效（非 JSON）")
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 数据"}, status_code=400
            )

        family = str(data.get("family") or "").upper().strip()
        if not family:
            return JSONResponse({"ok": False, "error": "缺少标的家族"}, status_code=400)
        timestep = str(data.get("timestep") or "15m")
        if timestep not in OPT_TIMESTEPS:
            return JSONResponse(
                {"ok": False, "error": f"不支持的周期：{timestep}"}, status_code=400
            )

        overrides = {
            k: v
            for k, v in (data.get("overrides") or {}).items()
            if v not in (None, "")
        }
        gatekeeper._log(
            f"[OPT-BT] 启动请求 family={family} timestep={timestep} "
            f"range={data.get('start') or '默认'}→{data.get('end') or '现在'} "
            f"initial_cash={data.get('initial_cash')} td_bars={data.get('td_bars')} "
            f"slippage={data.get('slippage')} overrides={overrides}"
        )
        try:
            from nanobot_quant.tools.tools_backtest import run_backtest

            resp = run_backtest(
                engine="options",
                family=family,
                timestep=timestep,
                start=data.get("start") or None,
                end=data.get("end") or None,
                initial_cash=float(data.get("initial_cash") or 10000),
                td_bars=int(data["td_bars"]) if data.get("td_bars") else None,
                slippage=float(data["slippage"]) if data.get("slippage") not in (None, "") else None,
                overrides=overrides,
            )
        except Exception as exc:  # noqa: BLE001
            gatekeeper._log(f"[OPT-BT] 启动异常: {exc}")
            return JSONResponse(
                {"ok": False, "error": f"启动期权回测失败: {exc}"}, status_code=400
            )
        if resp.get("error"):
            gatekeeper._log(f"[OPT-BT] run_backtest 拒绝: {resp['error']}")
            return JSONResponse({"ok": False, "error": resp["error"]}, status_code=400)

        run_id = resp["run_id"]
        gatekeeper._log(f"🟤 期权回测启动 family={family} run_id={run_id}")
        return JSONResponse({"ok": True, "run_id": run_id})

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
        return JSONResponse({"ok": True, "runs": _opt_runs()})

    async def _meta(request: Request):
        """页面初始化数据：家族列表 + 周期列表。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        return JSONResponse(
            {"ok": True, "families": available_families(), "timesteps": list(OPT_TIMESTEPS)}
        )

    app.add_route("/config/backtest/options/start", _start, methods=["POST"])
    app.add_route("/config/backtest/options/result", _result, methods=["GET"])
    app.add_route("/config/backtest/options/runs", _runs, methods=["GET"])
    app.add_route("/config/backtest/options/meta", _meta, methods=["GET"])
