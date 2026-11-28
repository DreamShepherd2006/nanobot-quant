"""A 股 ETF 期权页面与 API（``/config/ashare``）。

只读采集器的控制面：开关 / 间隔 / 标的 / 虚值带 + 立即采样 + 合约清单刷新 +
当天落盘统计。数据面在 :mod:`nanobot_quant.ashare.tape`。

权限：仅 commander（与其余业务页一致）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from nanobot_quant.ashare import chain_workbench as cwb
from nanobot_quant.ashare import contracts as ac
from nanobot_quant.ashare import iv_panel as ivp
from nanobot_quant.ashare import params_md as pm
from nanobot_quant.ashare import paper_strategy as ps
from nanobot_quant.ashare import trades_md as tm
from nanobot_quant.ashare import option_chain as oc
from nanobot_quant.ashare import paper_replay as prp
from nanobot_quant.ashare import paper_runner as pr
from nanobot_quant.ashare import tape
from nanobot_quant.ashare import td_panel as tp
from nanobot_quant import credential_registry as creg

_HERE = Path(__file__).resolve().parent


def _load_template(name: str) -> str:
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


def _authorized(request: Request, gatekeeper) -> tuple[str | None, bool]:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not user:
        return "请先登录", False
    try:
        if not gatekeeper._platform.is_commander(user):
            return "仅指挥官（owner）可访问", False
    except Exception:  # noqa: BLE001
        return "权限校验失败", False
    return None, True
def _state_payload() -> dict:
    storage = creg._get_storage_dir()
    c = ac.contracts(storage)
    items = c.get("items") or []
    return {
        "config": tape.tape_config(),
        "status": tape.state(),
        "contracts": {
            "count": len(items),
            "fetched_at": c.get("fetched_at"),
            "age_s": c.get("age_s"),
            "retry_in_s": c.get("retry_in_s") or 0,
            "source": c.get("source"),
            "error": c.get("error") or "",
            "underlyings": sorted({i["undl"] for i in items}),
        },
        "options": {
            # 只列池内标的（588080 流动性太差，故意不给选项）
            "underlyings": [{"code": c, "name": n} for c, n in tape.POOL_UNDERLYINGS],
            "interval_range": list(tape.INTERVAL_RANGE),
        },
    }


def _trim_row(r: dict) -> dict:
    """只回传页面要用的字段（快照行有 20+ 键，全传会拖慢轮询）。"""
    keys = ("code", "right", "strike", "dte", "bid", "ask", "mid",
            "iv", "iv_bid", "iv_ask", "delta", "otm_pct", "spread_pct",
            "f_src")
    return {k: r.get(k) for k in keys}


def _expiry_summary(snap: dict) -> list[dict]:
    """每个到期一条摘要：DTE / 隐含远期 / ATM IV（call·put）/ 配对质量。"""
    rows = snap.get("rows") or []
    out = []
    for expiry in snap.get("expiries") or []:
        rs = [r for r in rows if str(r.get("expiry") or "") == expiry]
        fw = (snap.get("forwards") or {}).get(expiry) or {}
        f = fw.get("f")
        dtes = [r["dte"] for r in rs if r.get("dte") is not None]
        dte = sorted(dtes)[len(dtes) // 2] if dtes else None
        atm_c = atm_p = strike = None
        if f:
            strikes = sorted({r["strike"] for r in rs if r.get("strike")},
                             key=lambda k: (abs(k - f), k))[:1]
            if strikes:
                strike = strikes[0]
                for r in rs:
                    if r.get("strike") != strike:
                        continue
                    if r.get("right") == "C":
                        atm_c = r.get("iv")
                    elif r.get("right") == "P":
                        atm_p = r.get("iv")
        out.append({"expiry": expiry, "dte": dte, "forward": f,
                    "pairs": fw.get("n_pairs"), "mad_pct": fw.get("mad_pct"),
                    "atm_strike": strike, "atm_iv_c": atm_c, "atm_iv_p": atm_p})
    return out


_LAST_CAND_DIAG = ""


def _cand_diag(symbol: str, iv_src: str, refs: list) -> None:
    """候选表分位来源诊断（仅状态变化时打一行，不刷屏）。

    页面「分位全是 —」这类问题必须能自证原因（哪一层缺数据），否则只能靠
    重建重试猜。日志走 stderr → Runtime logs。
    """
    global _LAST_CAND_DIAG
    vals = sum(1 for r in refs if r.get("values"))
    qs = sum(1 for r in refs
             if any(v is not None for v in (r.get("q") or {}).values()))
    key = f"{symbol}|{iv_src}|buckets={len(refs)}|vals={vals}|q={qs}"
    if key == _LAST_CAND_DIAG:
        return
    _LAST_CAND_DIAG = key
    print(f"[ASHARE-CAND] 分位来源 {key}", file=sys.stderr, flush=True)


def register_ashare_routes(app, gatekeeper) -> None:

    @app.get("/config/ashare", response_class=HTMLResponse)
    async def _page(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return HTMLResponse(f"<h3>{err}</h3>", status_code=403)
        return HTMLResponse(_load_template("ashare_page.html"))

    @app.get("/config/ashare/state")
    async def _state(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        return JSONResponse({"ok": True, **_state_payload()})

    @app.get("/config/ashare/params.md")
    async def _params_md(request: Request):
        """页面参数快照（Markdown）——「📋 复制参数」按钮的唯一实现。

        只读：不写文件、不改配置、不触发 IV 重算（``iv_panel.cached_state``）。
        与 ``/config/ashare`` 页共用同一份渲染，将来 MCP/CLI 也直接读它。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return PlainTextResponse(f"{err}", status_code=403)
        symbol = request.query_params.get("symbol")
        try:
            text = await asyncio.to_thread(pm.build, symbol)
        except Exception as e:  # noqa: BLE001
            return PlainTextResponse(f"生成失败：{type(e).__name__}: {e}", status_code=500)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/config/ashare/trades.md")
    async def _trades_md(request: Request):
        """模拟账本成交与持仓（Markdown）——「📋 复制成交」按钮的唯一实现。

        ``?limit=`` 每臂最近 N 笔（默认 50，0 = 全部保留的成交）。只读。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return PlainTextResponse(f"{err}", status_code=403)
        raw = request.query_params.get("limit")
        try:
            limit = tm.DEFAULT_LIMIT if raw in (None, "") else int(raw)
        except ValueError:
            return PlainTextResponse(f"limit 必须是整数（收到 {raw!r}）", status_code=400)
        try:
            text = await asyncio.to_thread(tm.build, limit)
        except Exception as e:  # noqa: BLE001
            return PlainTextResponse(f"生成失败：{type(e).__name__}: {e}", status_code=500)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/config/ashare/td")
    async def _td(request: Request):
        """A 股 ETF TD 计数看板（只读、与策略同源、10s 缓存）。

        ``?symbol=588000.SH`` 切换标的（只限注册表内标的，未知值 400 并列可选值）；
        缺省 = ``510050.SH``。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        force = request.query_params.get("force") == "1"
        symbol = (request.query_params.get("symbol")
                  or request.query_params.get("code")
                  or tp.DEFAULT_CODE)
        if symbol not in tp.SYMBOLS:
            return JSONResponse({"ok": False,
                                 "error": f"未知标的 {symbol!r}",
                                 "available": list(tp.SYMBOLS)}, status_code=400)
        try:
            # 同步网络请求放到线程池，避免阻塞网关事件循环（页面 10s 轮询）
            return JSONResponse(await asyncio.to_thread(tp.panel, symbol, force=force))
        except Exception as e:  # noqa: BLE001 — 看板失败不能拖垮页面
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)

    # ── 期权链 · IV 分位（只读；实时链 + 历史分位）──────────────

    @app.get("/config/ashare/chain")
    async def _chain(request: Request):
        """实时期权链（parity 远期 + 自算 IV）+ 当前 30d 常熟 IV。

        ``?symbol=510050.SH``（限池内标的）``&expiry=20261223``（缺省=最近到期）
        ``&band=12``（虚值带，3–30）``&force=1``（绕过 15s 缓存）。
        """
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        pool = [c for c, _ in tape.POOL_UNDERLYINGS]
        symbol = request.query_params.get("symbol") or pool[0]
        if symbol not in pool:
            return JSONResponse({"ok": False, "error": f"未知标的 {symbol!r}",
                                 "available": pool}, status_code=400)
        try:
            band = float(request.query_params.get("band") or oc.DEFAULT_BAND_PCT)
        except ValueError:
            band = oc.DEFAULT_BAND_PCT
        band = min(30.0, max(3.0, band))
        force = request.query_params.get("force") == "1"
        want = request.query_params.get("expiry") or ""
        try:
            snap = await asyncio.to_thread(
                oc.chain_snapshot, symbol, creg._get_storage_dir(),
                expiries=3, band_pct=band, force=force)
        except Exception as e:  # noqa: BLE001 — 拉链失败要如实回报，不静默留白
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)

        exps = _expiry_summary(snap)
        expiry = want if any(e["expiry"] == want for e in exps) else (
            exps[0]["expiry"] if exps else "")
        rows = [r for r in (snap.get("rows") or [])
                if str(r.get("expiry") or "") == expiry]
        live = ivp.live_cm_iv(snap, 30.0)
        iv_state = ivp.panel(symbol)
        pct = None
        if iv_state.get("status") == "ready" and live.get("iv") is not None:
            pct = ivp.pctile(live["iv"], (iv_state.get("cm30") or {}).get("values") or [])
        # 🎯 卖出候选：选档规则与模拟账本同一份（rank_puts）；IV 分位只读已算好的缓存，
        # 不触发重算。算不出来也要让页面能打开，但错误必须回传（不静默留白）。
        cand: dict = {"rows": [], "pick": "", "pick_other_expiry": "", "params": {}}
        try:
            pcfg = pr.load_config()
            pparams = ps.PaperParams(
                **{k: v for k, v in (pcfg.get("params") or {}).items()
                   if k in pr.PARAM_FIELDS})
            refs, iv_src = ivp.bucket_refs(symbol)
            cand = cwb.build_candidates(
                snap.get("rows") or [], float(snap.get("spot") or 0.0),
                expiry=expiry, underlying=symbol, params=pparams,
                arm_shorts=pr.open_shorts_by_arm(),
                iv_src=iv_src,
                iv_pct_fn=lambda iv, dte: ivp.percentile_approx(refs, dte, iv))
            # 诊断：分位为空时页面要能说清是哪一层缺数据（桶数/有样本桶数）
            cand.setdefault("params", {}).update({
                "iv_buckets": len(refs),
                "iv_vals": sum(1 for r in refs if r.get("values")),
                "iv_q": sum(1 for r in refs
                            if any(v is not None
                                   for v in (r.get("q") or {}).values())),
            })
            _cand_diag(symbol, iv_src, refs)
        except Exception as e:  # noqa: BLE001
            cand["error"] = f"{type(e).__name__}: {e}"
        return JSONResponse({
            "ok": True,
            "symbol": symbol,
            "spot": snap.get("spot"),
            "asof": snap.get("asof"),
            "in_session": snap.get("in_session"),
            "frozen": snap.get("frozen"),
            "cached": snap.get("cached"),
            "age_s": snap.get("age_s"),
            "source": snap.get("source"),
            "errors": snap.get("errors") or [],
            "note": snap.get("note") or "",
            "band_pct": band,
            "expiry": expiry,
            "expiries": exps,
            "rows": [_trim_row(r) for r in rows],
            "live_cm30": {**live, "pct": pct},
            "candidates": cand,
        })

    @app.get("/config/ashare/iv")
    async def _iv(request: Request):
        """IV 分位面板（QMT 历史序列；首次请求起后台线程算，其余返回 computing）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        pool = [c for c, _ in tape.POOL_UNDERLYINGS]
        symbol = request.query_params.get("symbol") or pool[0]
        if symbol not in pool:
            return JSONResponse({"ok": False, "error": f"未知标的 {symbol!r}",
                                 "available": pool}, status_code=400)
        force = request.query_params.get("force") == "1"
        try:
            return JSONResponse(await asyncio.to_thread(ivp.panel, symbol, force=force))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)

    @app.post("/config/ashare/save")
    async def _save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "无效的请求体"}, status_code=400)
        cfg = tape.save_tape_config(
            enabled=bool(body.get("enabled")),
            interval_s=body.get("interval_s"),
            underlyings=body.get("underlyings"),
            expiries=body.get("expiries"),
            band_pct=body.get("band_pct"),
            max_per_side=body.get("max_per_side"),
        )
        try:
            tape.sync()
        except Exception as e:  # noqa: BLE001 — 保存成功、启停失败也要如实回报
            return JSONResponse({"ok": False, "config": cfg,
                                 "error": f"保存成功但启停失败: {type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, "config": cfg, "status": tape.state()})

    @app.post("/config/ashare/sample")
    async def _sample(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        persist = bool((body or {}).get("persist"))
        rec = tape.sample_now(persist=persist)
        rows = rec.get("rows") or []
        preview = []
        for r in rows[:5]:
            item = dict(r)
            item["qt"], item["qa"] = tape.quote_meta(item.get("ts"), rec.get("ts_ms"))
            preview.append(item)
        return JSONResponse({
            "ok": True,
            "v": rec.get("v"),
            "persisted": bool(persist and rows),
            "skipped": rec.get("skipped") or "",
            "error": rec.get("error") or "",
            "ts": rec.get("ts"),
            "spot": rec.get("spot") or {},
            "picked": rec.get("picked"),
            "rows": len(rows),
            # 诊断字段必须真正下发：历史上 sample_once 算好了 mult_src/mult_missing，
            # 却被这层挑字段吞掉 ⇒ 页面无法判断选了哪份合约表、是否漏挂乘数。
            "mult_src": rec.get("mult_src") or "",
            "mult_missing": rec.get("mult_missing"),
            "mult_error": rec.get("mult_error") or "",
            "preview": preview,
        })

    @app.post("/config/ashare/contracts/refresh")
    async def _refresh(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        c = ac.contracts(creg._get_storage_dir(), force=True)
        return JSONResponse({"ok": True, "count": len(c.get("items") or []),
                             "source": c.get("source"),
                             "age_s": c.get("age_s"),
                             "retry_in_s": c.get("retry_in_s") or 0,
                             "error": c.get("error") or ""})

    # 开机自恢复：采集是只读的，enabled=true 时随网关启动。
    # 与交易循环不同——不涉及资金与订单，中断只丢样本，故允许自动拉起。
    try:
        tape.sync()
    except Exception as e:  # noqa: BLE001
        print(f"[ASHARE-TAPE] 启动同步失败: {type(e).__name__}: {e}", flush=True)

    # ── 模拟账本（纸上交易，不接券商） ──────────────────────

    @app.get("/config/ashare/paper/state")
    async def _paper_state(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            return JSONResponse({"ok": True, **(await asyncio.to_thread(pr.state))})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)

    @app.post("/config/ashare/paper/save")
    async def _paper_save(request: Request):
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "无效的请求体"}, status_code=400)
        try:
            cfg = pr.save_config(
                enabled=body.get("enabled"),
                interval_s=body.get("interval_s"),
                symbols=body.get("symbols"),
                initial_cash=body.get("initial_cash"),
                second_arm=body.get("second_arm"),
                params=body.get("params") if isinstance(body.get("params"), dict) else None,
            )
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        try:
            st = pr.sync()
        except Exception as e:  # noqa: BLE001 — 保存成功、启停失败也要如实回报
            return JSONResponse({"ok": False, "config": cfg,
                                 "error": f"保存成功但启停失败: {type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, "config": cfg, "runner": st})

    @app.post("/config/ashare/paper/reset")
    async def _paper_reset(request: Request):
        """清空模拟账本（先关循环，避免与正在跑的一轮抢写）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        try:
            pr.save_config(enabled=False)
            pr.stop()
            pr.reset_state(initial_cash=(body or {}).get("initial_cash"))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, **(await asyncio.to_thread(pr.state))})

    @app.post("/config/ashare/paper/round")
    async def _paper_round(request: Request):
        """立即跑一轮（手动验证，不改变开关状态）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            out = await asyncio.to_thread(pr.round_once)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, "round": out,
                             **(await asyncio.to_thread(pr.state))})

    # ── 历史回放（离线；只读 CSV，不写实时账本 state） ──────
    # 与实时模拟账本**物理隔开**：结果单独落 ashare_paper/replay/，两套数字
    # 口径不同（历史无盘口、bid 用 1m close 代理），页面也不并列展示。

    @app.get("/config/ashare/paper/replay/datasets")
    async def _replay_datasets(request: Request):
        """可用数据集（ETF 批次 / 期权批次 / 合约清单）——页面下拉的数据来源。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        force = request.query_params.get("force") == "1"
        try:
            data = await asyncio.to_thread(prp.list_datasets, None, force=force)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, **data})

    @app.post("/config/ashare/paper/replay/start")
    async def _replay_start(request: Request):
        """启动后台回放（同一时刻只允许一个；重复点击 fail-closed 报错）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "无效的 JSON"}, status_code=400)
        try:
            kw = prp.resolve_inputs(body or {})
            st = prp.start_job(**kw)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, "job": st})

    @app.get("/config/ashare/paper/replay/state")
    async def _replay_state(request: Request):
        """当前任务进度 + 最近结果列表（轮询用；纯内存，2s 一次）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        return JSONResponse({"ok": True, **prp.job_state()})

    @app.get("/config/ashare/paper/replay/result")
    async def _replay_result(request: Request):
        """某次回放的全量结果（含 markdown，供复制进对话）。"""
        err, ok = _authorized(request, gatekeeper)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        rid = request.query_params.get("run_id") or ""
        try:
            data = await asyncio.to_thread(prp.load_result, rid)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        return JSONResponse({"ok": True, **data})

    # 模拟账本同样自动恢复（enabled=true 时）：只读行情 + 本地账本，不下单、
    # 不碰资金，中断只丢纸上样本，无资金风险。
    try:
        pr.sync()
    except Exception as e:  # noqa: BLE001
        print(f"[ASHARE-PAPER] 启动同步失败: {type(e).__name__}: {e}", flush=True)


__all__ = ["register_ashare_routes"]
