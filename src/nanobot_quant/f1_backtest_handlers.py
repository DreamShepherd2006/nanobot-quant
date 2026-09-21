"""F1 模式 WebUI 后端（Commander only，§33.36 S1）。

页面挂在 ``/config/backtest`` 的「📊 F1 模式回测」分栏（由
``backtest_page.html`` 的前端渲染），这里提供该分栏需要的端点：

POST /config/backtest/f1/start   — 起一轮 F1 分析（异步 run_id）
GET  /config/backtest/f1/result  — 轮询结果（run_id）
GET  /config/backtest/f1/runs    — 历史记录（只列 f1- 前缀的 run）
GET  /config/backtest/f1/meta    — 页面初始化（可用源 / 周期 / 默认值）

与现货、期权回测共用同一套 run_id + 轮询契约，结果同样落在
``{data_root}/legion/backtests/<run_id>.json``（前缀 ``f1-``）。

**只读**：F1 分析不下单、不改任何配置、不回写任何参数文件
（§33.36.1 定位 —— 回测是实验场，验证有明确结果才挪实盘）。
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from nanobot_quant.backtest_handlers import _authorized, _body

# 各源可用周期（与 §33.36.2 实测一致；界面按源动态过滤）
SOURCE_PERIODS: dict[str, tuple[str, ...]] = {
    # Gate CEX：16 周期全覆盖（1m 深度约 6.9 天）
    "gate_cex": ("1m", "3m", "5m", "15m", "30m", "1H", "2H", "4H", "6H", "12H", "1D"),
    # OKX CEX：research 源
    "okx_cex": ("1m", "5m", "15m", "30m", "1H", "4H", "1D"),
    # 新浪：**无 1m**（接口返回空）——实测 601127 2026-09-21
    "sina": ("5m", "15m", "30m", "1H", "1D"),
    # 东财：**云端数据中心 IP 被封**（三入口全 RemoteDisconnected）
    # —— 入口保留、选中时 fail-closed 报错，不静默降级
    "eastmoney": ("1m", "5m", "15m", "30m", "1H", "1D"),
}

FALLBACK_SOURCES: tuple[str, ...] = ("gate_cex", "okx_cex", "sina", "eastmoney")

# 源的中文备注（页面提示用）
SOURCE_NOTES: dict[str, str] = {
    "sina": "无 1m 周期（接口返回空）",
    "eastmoney": "⚠️ 云端数据中心 IP 被封（push2his/82./1. 三入口均不可达），选中将报错",
    "gate_cex": "16 周期；1m 深度约 6.9 天",
    "okx_cex": "research 源（回测/展示，不参与执行）",
}


def available_sources() -> list[str]:
    """页面下拉可用源 = 注册表里可解析到的 ∪ 兜底列表。"""
    out: list[str] = []
    try:
        from nanobot_quant.data_sources import list_data_sources

        for spec in list_data_sources() or []:
            name = getattr(spec, "name", None) or (spec.get("name") if isinstance(spec, dict) else None)
            if name and str(name) not in out:
                out.append(str(name))
    except Exception:  # noqa: BLE001 — 注册表读不到不阻塞页面
        pass
    for s in FALLBACK_SOURCES:
        if s not in out:
            out.append(s)
    return out


def _f1_runs(limit: int = 20) -> list[dict]:
    """历史 run 里只挑 F1 分析（run_id 前缀 ``f1-``）。"""
    from nanobot_quant.backtest_handlers import _recent_runs

    return [
        r for r in _recent_runs(limit=limit * 2) if r["run_id"].startswith("f1-")
    ][:limit]


def register_f1_routes(app, gatekeeper) -> None:  # noqa: ANN001
    """挂 F1 分析的四个端点（页面由 backtest_handlers 渲染）。"""

    async def _start(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        data = await _body(request)
        if data is None:
            gatekeeper._log("[F1-PAGE] start 请求体无效（非 JSON）")
            return JSONResponse(
                {"ok": False, "error": "无效的 JSON 数据"}, status_code=400
            )

        kind = str(data.get("kind") or "f1_td").strip().lower()
        if kind not in ("f1_td", "f1_drawdown"):
            return JSONResponse(
                {"ok": False, "error": f"不支持的 kind：{kind}"}, status_code=400
            )

        raw_symbols = data.get("symbols")
        if isinstance(raw_symbols, str):
            symbols = [s.strip() for s in raw_symbols.replace("，", ",").split(",")]
        else:
            symbols = list(raw_symbols or [])
        symbols = [s for s in symbols if s]
        if not symbols:
            return JSONResponse(
                {"ok": False, "error": "至少选择一个标的"}, status_code=400
            )

        raw_periods = data.get("periods")
        if isinstance(raw_periods, str):
            periods = [p.strip() for p in raw_periods.split(",")]
        else:
            periods = list(raw_periods or [])
        periods = [p for p in periods if p]
        source = str(data.get("source") or "").strip()

        # 周期可用性按源校验（fail-closed，不静默替换成别的周期）
        if source and source in SOURCE_PERIODS:
            allowed = SOURCE_PERIODS[source]
            bad = [p for p in periods if p not in allowed]
            if bad:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            f"源 {source} 不支持周期 {', '.join(bad)}；"
                            f"可用：{', '.join(allowed)}"
                        ),
                    },
                    status_code=400,
                )

        # 只透传已知参数，防止页面把无关字段塞进分析函数
        extra: dict = {}
        for key in ("k", "ks", "atr_n", "threshold", "limit", "qmin", "qmax", "split"):
            if data.get(key) not in (None, ""):
                extra[key] = data[key]
        for key in ("include_price_td", "include_tail"):
            if isinstance(data.get(key), bool):
                extra[key] = data[key]

        gatekeeper._log(
            f"[F1-PAGE] 启动请求 kind={kind} symbols={symbols} periods={periods} "
            f"source={source or 'auto'} extra={extra}"
        )
        try:
            from nanobot_quant.tools.tools_f1 import run_f1_analysis

            resp = run_f1_analysis(
                kind=kind,
                symbols=symbols,
                periods=periods,
                source=source,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001
            gatekeeper._log(f"[F1-PAGE] 启动异常: {exc}")
            return JSONResponse(
                {"ok": False, "error": f"启动 F1 分析失败: {exc}"}, status_code=400
            )
        if resp.get("error"):
            gatekeeper._log(f"[F1-PAGE] run_f1_analysis 拒绝: {resp['error']}")
            return JSONResponse({"ok": False, "error": resp["error"]}, status_code=400)

        run_id = resp["run_id"]
        gatekeeper._log(f"📊 F1 分析启动 kind={kind} run_id={run_id}")
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
        from nanobot_quant.tools.tools_f1 import get_f1_result

        return JSONResponse(get_f1_result(run_id))

    async def _runs(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        return JSONResponse({"ok": True, "runs": _f1_runs()})

    async def _meta(request: Request):
        """页面初始化：可用源 + 各源周期 + 备注。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": err},
                status_code=403 if "Commander" in err else 401,
            )
        return JSONResponse(
            {
                "ok": True,
                "sources": available_sources(),
                "periods": {k: list(v) for k, v in SOURCE_PERIODS.items()},
                "notes": SOURCE_NOTES,
            }
        )

    app.add_route("/config/backtest/f1/start", _start, methods=["POST"])
    app.add_route("/config/backtest/f1/result", _result, methods=["GET"])
    app.add_route("/config/backtest/f1/runs", _runs, methods=["GET"])
    app.add_route("/config/backtest/f1/meta", _meta, methods=["GET"])
