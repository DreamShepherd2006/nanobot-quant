"""run_backtest: async backtest as an MCP tool (run_id + poll pattern).

A real backtest (CLI kline fetch + Lumibot startup + strategy loop) takes
longer than the 30s MCP tool hard timeout for any non-trivial range
(measured: 5d ≈ 9s, 180d > 30s).  So the tool returns immediately with a
``run_id`` and runs the backtest in a background daemon thread; the result
is persisted to ``{data_root}/legion/backtests/<run_id>.json`` and fetched
with ``get_backtest_result``.  Same contract as ``run_research_chain`` /
``get_chain_result``.

Two engines:
- ``backtest_runner`` (default, zero behaviour change): legacy lumibot
  StrategyExecutor backtest on a single symbol/range.
- ``driver`` (Step 3 replay driver): scene-based replay on Gate CEX
  history, reusing the live strategy decision code (BacktestBroker for
  simulated fills).  Result carries scene/symbols/fills_detail/net_values/
  ROI — the WebUI /config/backtest page consumes this engine.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from uuid import uuid4


# ── 回测覆盖参数归一化（MCP 与 WebUI 共用同一实现）──────────────────
# 2026-11-05：实现原先只在 backtest_handlers._coerce_overrides，即只有
# WebUI 路径做类型转换；MCP run_backtest 直连时 overrides 原样透传，
# 字符串 "false" 在策略侧 `if parameters.get(...)` 里是真值 ⇒ 静默生效，
# 两个入口可能跑出不同结果。现下沉到此文件，run_backtest 入口统一调用。

#: 回测每 slot 初始资金默认值（页面输入框、MCP 默认、handler 兜底同源）
DEFAULT_INITIAL_QUOTE = 100.0

_OV_INT = {
    "entry_setup", "entry_countdown", "cd_entry_setup_gap",
    "exit_setup", "exit_countdown", "min_hold_bars",
    "cd_stall_n", "td_start_slot",
    # EMA 趋势变量（2026-10-20，C73/C74 研究线，回测侧）
    "ema_n", "ema_k",
    # EMA 折价入场门（2026-11-01，回测侧覆盖）
    "entry_ema_period",
    # 自适应加仓间距（2026-11-03 研究线）
    "adaptive_m",
    # 波锁容量（2026-11-04，0 = 关闭）
    "wave_max_buys",
    # EOD 日界小时（2026-11-05 Step 2）
    "eod_hour_utc",
}
_OV_FLOAT = {
    "sell_only_profit_high", "sell_only_profit_low", "cd_exit_min_profit",
    "stop_loss_pct", "take_profit_pct", "min_account_value",
    "entry_ema_offset_pct",
    # 加仓最小间距闸（2026-11-03）
    "entry_min_gap_pct",
    # 贝叶斯闸门阈值 / 手续费率 / 固定金额（C118，2026-11-06）
    "gate_red_min", "fee_rate", "td_fixed_amount",
}
_OV_BOOL = {
    "momentum_exit", "td_sell_all", "cd_exit_all",
    "ema_gate_enabled", "ema_compare_baseline",
    "entry_ema_enabled",
    # 自适应加仓间距（2026-11-03 研究线）
    "adaptive_gap_enabled",
    # EOD 单双号强平（2026-11-05 Step 2）
    "eod_enabled",
    # 出场首穿（2026-11-06）：高9/cd13 只在首次跨过阈值那根触发
    "exit_first_cross",
    # 多周期信号口径（2026-11-06）：bool 必须走本表，否则字符串 "false"
    # 在策略侧 `if parameters.get(...)` 里是真值 ⇒ 静默按 partial 跑
    "entry_periods_partial",
    # 富途变体无 TDST：该键必须能显式关掉（C119，2026-11-06）
    "tdst_filter",
    # 贝叶斯闸门开关（C119）
    "gate_enabled",
}

#: 枚举类覆盖参数（C119，2026-11-06）——非法值 fail-closed。
#: 此前这些键走 `else: 原样透传`：填错拼写会被策略当默认分支处理
#: （如 eod_parity="evan" 被当 even `or` 回退），结果与参数表不符。
_OV_ENUM: dict[str, set] = {
    "quantity_mode": {"fixed", "fixed_amount", "value"},
    "exit_order": {"fifo", "lifo", "min_entry"},
    "eod_parity": {"even", "odd", "both"},
    "eod_mode": {"all", "profit_only"},
}


def coerce_overrides(raw) -> dict:
    """回测覆盖参数归一化（空串/None 忽略；int/float/bool 按字段转换）。

    MCP ``run_backtest`` 与 WebUI ``/config/backtest/start`` 共用本实现，
    保证两个入口对同一份 overrides 得到同一个 dict（尤其布尔：字符串
    ``"false"`` 必须归一成 ``False``，否则策略侧按真值处理）。
    仅作用于本次回测，绝不回写 exec_params（2026-08-30 拍板）。
    """
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
            elif k in _OV_ENUM:
                # 枚举类：非法值 fail-closed（C119）——填错拼写不能被静默
                # 当默认分支跑，否则参数表与实际行为不符。
                _s = str(v).strip()
                if _s not in _OV_ENUM[k]:
                    raise ValueError(
                        f"回测覆盖参数 {k}={v!r} 非法（可选: "
                        f"{', '.join(sorted(_OV_ENUM[k]))}）"
                    )
                out[k] = _s
            elif k == "entry_periods":
                # 多周期倍数：非法值 fail-closed。策略侧 parse_entry_periods 会
                # 静默忽略非法项并回退 [1]，若不在入口拦住，页面填错就等于
                # 「以为开了多周期、实际只跑单周期」。
                from nanobot_quant.exec_params import (
                    parse_entry_periods,
                    validate_exec_param,
                )

                _err = validate_exec_param(k, v)
                if _err:
                    raise ValueError(f"回测覆盖参数 {k}={v!r} 非法：{_err}")
                out[k] = ",".join(str(x) for x in parse_entry_periods(v))
            else:
                out[k] = v  # exit_order 等字符串透传
        except ValueError as exc:
            # 保留分支里已经写好的详细原因（枚举可选值 / entry_periods
            # 校验器返回的说明），不要统一压成一句话丢上下文。
            if str(exc).startswith("回测覆盖参数"):
                raise
            raise ValueError(f"回测覆盖参数 {k}={v!r} 非法") from None
        except TypeError:
            raise ValueError(f"回测覆盖参数 {k}={v!r} 非法") from None
    return out


def _backtest_log(run_id: str, payload: dict) -> None:
    """Persist backtest outcome to ``{data_root}/legion/backtests``.

    Written to the persistent audit directory (survives Factory Rebuild)
    and mirrored to the MCP server stderr.
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        out_dir = backtests_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — logging must never break the chain
        print(f"[DIAG] _backtest_log failed: {exc}", file=sys.stderr, flush=True)
    print(
        f"[DIAG] run_backtest {run_id}: {json.dumps(payload, ensure_ascii=False)[:800]}",
        file=sys.stderr,
        flush=True,
    )


def _run_guarded(run_id: str, prep, run) -> None:
    """Background thread: guard MCP stdio, run the backtest, persist outcome.

    Guards the MCP stdio channel exactly like the sync path used to:
    1) env toggles BEFORE any lumibot import (constants read at import
       time) — kill the \\r progress bar and silence INFO logs;
    2) clear stdout-bound handlers on the lumibot logger tree (sub-loggers
       register their own StreamHandler at import time) — ``prep`` imports
       the lumibot-dependent modules first, then silence runs;
    3) redirect the runner's own print() progress lines (CLI-facing) to
       stderr; the result is persisted as a value, never via stdout.

    起手先落一条 ``status=running``：否则 run 在跑的那几分钟里，历史记录
    完全看不到它（结果文件尚未写出），刷新页面后更像「什么都没发生」。
    """
    _backtest_log(run_id, {"status": "running", "run_id": run_id})
    _saved_stdout = sys.stdout
    try:
        os.environ.setdefault("LUMIBOT_TELEMETRY", "0")
        os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "0")
        os.environ.setdefault("BACKTESTING_QUIET_LOGS", "true")

        from nanobot_quant.tools.tools_execute import (
            _redirect_lumibot_console_to_stderr,
            _silence_lumibot_loggers,
        )

        # "LumiBot vX starting" is logged AT import time via a stdout-bound
        # StreamHandler — pre-register a stderr console handler so the banner
        # reuses it and never touches stdout. Then silence the tree.
        _redirect_lumibot_console_to_stderr()
        prep()
        _silence_lumibot_loggers()

        sys.stdout = sys.stderr
        try:
            result = run()
            _backtest_log(run_id, {"status": "done", "run_id": run_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            _backtest_log(run_id, {"status": "error", "run_id": run_id, "error": str(exc)})
        finally:
            sys.stdout = _saved_stdout
    finally:
        sys.stdout = _saved_stdout


def _auto_backtest(
    run_id: str,
    symbol: str,
    start: str,
    end: str,
    quantity: int,
    source: str,
) -> None:
    """Legacy engine (backtest_runner): single-symbol range backtest."""

    def _prep() -> None:
        # Import the lumibot-dependent module inside the guard so its
        # import-time stdout handlers are silenced afterwards.
        from nanobot_quant.backtest_runner import run as _backtest_run  # noqa: F401

    def _run():
        from nanobot_quant.backtest_runner import run as _backtest_run

        return _backtest_run(
            symbol=symbol,
            start=start,
            end=end,
            quantity=quantity,
            source=source,
        )

    _run_guarded(run_id, prep=_prep, run=_run)


def _arm_summary(res: dict) -> dict:
    """单臂摘要（只取可比项；不包含逐 bar / 成交明细等大字段）。"""
    skips = res.get("skip_counts") or {}
    eg = res.get("ema_gate") or {}
    return {
        "roi_pct": (res.get("roi") or 0.0) * 100,
        "final_net": res.get("final_net"),
        "fills": res.get("fills"),
        "ema_pass": eg.get("pass"),
        "ema_block": eg.get("block"),
        "ema_na": eg.get("na"),
        "skip_total": sum(int(v or 0) for v in skips.values()),
        "skip": dict(skips),
    }


def _arms_compare(gated: dict, baseline: dict) -> dict:
    """两臂对照（EMA 闸门开 vs 关）。

    **仅展示**：两臂跑的是同一段行情、同一批信号，是两条路径而不是两个
    独立样本 ⇒ 不得做配对推断，也不得把差值当成闸门效应量（C73 要求
    单规则预注册 + 组合层 walk-forward 样本外裁决）。
    """
    g = _arm_summary(gated)
    b = _arm_summary(baseline)
    return {
        "gated": g,
        "baseline": b,
        "delta_roi_pct": round((g["roi_pct"] or 0.0) - (b["roi_pct"] or 0.0), 4),
        "delta_fills": (g.get("fills") or 0) - (b.get("fills") or 0),
        "note": (
            "仅展示：两臂为同一段行情的两条路径（非独立样本），"
            "差值不得当成闸门效应量、禁配对推断"
        ),
    }


def _parse_ts(value: str | None):
    """start/end → datetime。无时区输入明确按 UTC
    （页面提交已由前端把本地时间转成 UTC ISO）。"""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _period_summary(period: str, res: dict) -> dict:
    """单周期摘要（与两臂摘要同口径，只取可比项）。"""
    s = _arm_summary(res)
    cs = res.get("capital_stats") or {}
    # TD / EMA 分段（2026-10-21）：对比表把两套指标的量分开列，不再合成一格。
    # 候选买入 = 通过 + 拦（候选买入口径，与页面逐 bar 闸门列同口径）。
    _p, _b = s.get("ema_pass"), s.get("ema_block")
    _cand = None if (_p is None and _b is None) else int((_p or 0) + (_b or 0))
    return {
        "period": period,
        "status": "ok",
        "timestep": res.get("timestep"),
        "bars": res.get("bars"),
        "fetched_bars": res.get("fetched_bars"),
        "initial_total": res.get("initial_total"),
        "turnover": cs.get("turnover"),
        "turnover_two_side": cs.get("turnover_two_side"),
        "utilization": cs.get("utilization"),
        "candidates": _cand,
        "ema_pass_rate": (
            round((_p or 0) / _cand * 100, 2) if _cand else None
        ),
        **s,
    }


def _validate_periods(periods: list[str]) -> dict | None:
    """回测周期入参校验：非法返回错误 dict，合法返回 None。

    2026-10-22：可接受集合 = 原生 ∪ 合成（``backtest_periods()``），与页面
    下拉、``driver._timestep_for`` 共用同一份来源。此前这里用的是仅含原生
    档的 ``PERIODS``——页面能选 2m/4m、提交却报“不支持的周期”（已修）。
    """
    if not periods:
        return None
    from nanobot_quant.data_sources.periods import backtest_periods

    allowed = backtest_periods()
    bad = [p for p in periods if p not in allowed]
    if bad:
        return {
            "error": f"不支持的周期: {bad}",
            "hint": "可用周期: " + ", ".join(allowed),
        }
    return None


def _run_multi_period(periods, overrides, build, progress_path) -> dict:
    """多周期独立回放（2026-10-21 拍板：并列独立，不做组合）。

    每档一个独立 driver（各自 broker / 台账 / 资金），**共享同一区间与同一份
    参数**：参数是 bar 计数语义 ⇒ “同参数 = 同根数，不是同时间”
    （min_hold_bars=10 在 1m 是 10 分钟、在 5m 是 50 分钟）。

    单档失败（如该周期数据深度不够）不拖垮其它档：记 insufficient 并继续，
    显式可见（fail-closed 不静默截断）。

    两臂对照在多周期下不跑（否则 2N 次回放），结果里显式注明。
    """
    per: dict = {}
    compare: list[dict] = []
    for i, p in enumerate(periods):
        ov = dict(overrides or {})
        ov["sleeptime"] = p
        # 诊断（2026-10-22）：多周期时“哪一档卡住/失败”必须从 Runtime logs
        # 直接看到（此前 2m/4m 报错只体现在报告里，需要靠用户回贴）。stderr
        # 对齐 [OPT-BT] 一类后台日志风格，静态静默不可接受。
        print(
            f"[BACKTEST-PAGE] 多周期 {i + 1}/{len(periods)} 周期 {p} 开始",
            file=sys.stderr,
            flush=True,
        )
        try:
            r = build(
                ov,
                progress_path,
                {
                    "period": p,
                    "period_index": i + 1,
                    "period_total": len(periods),
                },
            ).run()
        except Exception as exc:  # noqa: BLE001
            per[p] = {"error": str(exc)}
            compare.append(
                {"period": p, "status": "insufficient", "error": str(exc)}
            )
            print(
                f"[BACKTEST-PAGE] 周期 {p} 失败: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        print(
            f"[BACKTEST-PAGE] 周期 {p} 完成 ROI={(r.get('roi') or 0.0) * 100:.4f}% "
            f"成交={len(r.get('fills_detail') or [])} "
            f"bars={r.get('bars')} 拉取={r.get('fetched_bars')}",
            file=sys.stderr,
            flush=True,
        )
        per[p] = r
        compare.append(_period_summary(p, r))

    ok = [p for p in periods if "error" not in per[p]]
    if not ok:
        raise RuntimeError(
            "所有周期均失败：" + "；".join(f"{p}: {per[p]['error']}" for p in periods)
        )
    base = per[ok[0]]
    res = {
        k: base.get(k)
        for k in (
            "scene",
            "symbols",
            "source",
            "source_label",
            "cross_venue",
            "cross_venue_note",
            "source_note",
            "initial_quote",
            "initial_total",
            "batches",
        )
    }
    res.update(
        {
            "multi_period": True,
            "periods": list(periods),
            "compare": compare,
            "period_results": per,
            "timestep": base.get("timestep"),
            "arms": {
                "skipped": "多周期回测默认不跑两臂对照（否则 2N 次回放）；"
                "如需闸门对照请按单周期单独跑"
            },
        }
    )
    return res


def _auto_backtest_driver(
    run_id: str,
    scene: str,
    symbols: list[str] | None,
    start: str | None,
    end: str | None,
    initial_quote: float,
    batches: int | None,
    slippage: float | None,
    fixed_amount: float | None,
    overrides: dict | None = None,
    source: str | None = None,
    periods: list[str] | None = None,
    shared_cash: bool = False,
    preset_meta: dict | None = None,
    strategy_variant: str | None = None,
) -> None:
    """New engine (backtest.driver): scene-based replay.

    Reuses the live strategy decision code (same StrategyExecutor scene
    construction, BacktestBroker for simulated fills).  Same run_id +
    poll contract as the legacy engine.

    ``source``: ``okx_cex``（默认，与执行通道同源）/ ``gate_cex``（是否跨所以
    ``exec_params.execution_channel`` 解析结果为准；跨所仅作参数粗筛，
    不作裁决依据）。
    """
    from datetime import datetime

    def _prep() -> None:
        from nanobot_quant.backtest.driver import BacktestDriver  # noqa: F401

    def _run():
        from nanobot_quant.backtest.driver import BacktestDriver
        from nanobot_quant.backtest.fetchers import DEFAULT_SOURCE
        from nanobot_quant.onchainos_cli import backtests_dir

        def _build(ov: dict | None, progress_path=None, extra: dict | None = None):
            return BacktestDriver(
                scene=scene,
                symbols=symbols,
                start_ts=_parse_ts(start),
                end_ts=_parse_ts(end),
                initial_quote=initial_quote,
                batches=batches,
                slippage=slippage,
                fixed_amount=fixed_amount,
                overrides=ov,
                source=source or DEFAULT_SOURCE,
                progress_path=progress_path,
                progress_extra=extra,
                shared_cash=shared_cash,
                preset_meta=preset_meta,
                strategy_variant=strategy_variant,
            )

        # 进度文件 = 结果文件（<run_id>.json）：运行期间 driver 写
        # {status: running, progress}，返回后 _backtest_log 覆写 done/error
        progress_file = backtests_dir() / f"{run_id}.json"
        _periods = [str(p) for p in (periods or []) if p]
        if len(_periods) >= 2:
            # 多周期：各档独立回放（不跑两臂，见 _run_multi_period）
            return _run_multi_period(_periods, overrides, _build, progress_file)

        _ov0 = dict(overrides or {})
        if _periods:  # 单周期覆盖（等价于 overrides.sleeptime）
            _ov0["sleeptime"] = _periods[0]
        res = _build(_ov0, progress_file).run()

        # 两臂对照（2026-10-20 拍板）：闸门开时顺带跑一遍无闸门基线。
        # 仅作**展示**——两臂是同一段行情的两条路径，不是独立样本，
        # 禁止配对推断（与 C73 预注册纪律一致）。
        _ov = _ov0
        _gate_on = _ov.get("ema_gate_enabled")
        _want = _ov.get("ema_compare_baseline")
        if _gate_on and (_want is None or bool(_want)):
            try:
                _base = _build(
                    {k: v for k, v in _ov.items() if k != "ema_compare_baseline"}
                    | {"ema_gate_enabled": False},
                    None,
                ).run()
                res["arms"] = _arms_compare(res, _base)
            except Exception as exc:  # noqa: BLE001
                # 基线臂失败不得拖垮主结果——显式记下原因，不静默
                res["arms"] = {"error": f"基线臂失败: {exc}"}
        return res

    _run_guarded(run_id, prep=_prep, run=_run)


def _auto_backtest_options(
    run_id: str,
    family: str,
    timestep: str = "15m",
    start: str | None = None,
    end: str | None = None,
    initial_cash: float = 10000.0,
    td_bars: int | None = None,
    slippage: float | None = None,
    overrides: dict | None = None,
) -> None:
    """期权引擎（``backtest.options_driver``）：卖 put 策略的归档重放。

    与现货 driver 共用同一套 run_id + 轮询契约；数据层走官方归档成交反解 IV
    曲面（``backtest.options_replay_data_source``），决策与实盘期权线共用
    ``okx_options_live`` 的策略函数 —— 回测跑的就是实盘那套逻辑。

    ``overrides`` 只作用于本次回测，**绝不回写** option_params.json
    （沿用现货回测 2026-08-30 拍板的口径）。
    """

    def _prep() -> None:
        from nanobot_quant.backtest.options_driver import (  # noqa: F401
            OptionsBacktestDriver,
        )

    def _run():
        from nanobot_quant.backtest.options_driver import (
            DEFAULT_INITIAL_CASH,
            DEFAULT_SETTLE_WINDOW_MIN,
            DEFAULT_SLIPPAGE_PCT,
            OptionsBacktestDriver,
        )

        # 资金链段开关（§33.43 Step 6 / C41b）不是策略参数，不进 opt_params：
        # 它们是“本次回测怎么建模”的开关，与实盘 option_params 无关。
        ov = dict(overrides or {})

        def _pop_bool(key: str, default: bool = True) -> bool:
            v = ov.pop(key, None)
            if v is None or v == "":
                return default
            return str(v).lower() not in ("0", "false", "off", "no")

        chain_enabled = _pop_bool("chain_enabled", True)
        call_enabled = _pop_bool("call_enabled", True)
        cover_mode = ov.pop("cover_mode", None) or None
        cover_discount = ov.pop("cover_discount_pct", None)
        cover_timeout = ov.pop("cover_timeout_hours", None)
        tp_call = ov.pop("tp_pct_call", None)
        settle_win = ov.pop("settle_window_min", None)

        def _progress(prog):
            """把 driver 进度写进 run 文件（页面轮询读它）；写盘失败不影响回测。"""
            try:
                _backtest_log(
                    run_id,
                    {"status": "running", "run_id": run_id, "progress": prog},
                )
            except Exception:  # noqa: BLE001
                pass

        # 期权 driver 的 start_ts/end_ts 是秒级时间戳（int），与现货 driver
        # 的 datetime 不同 —— 传错会在内部的 int(end_ts) 处报 TypeError。
        d = OptionsBacktestDriver(
            family,
            timestep=timestep or "15m",
            start_ts=_opt_ts_seconds(start),
            end_ts=_opt_ts_seconds(end),
            td_bars=int(td_bars) if td_bars else _DEFAULT_OPT_TD_BARS,
            initial_cash=float(initial_cash or DEFAULT_INITIAL_CASH),
            slippage_pct=float(slippage)
            if slippage is not None and slippage != ""
            else DEFAULT_SLIPPAGE_PCT,
            opt_params=_merge_opt_params(ov, timestep),
            progress_cb=_progress,
            cover_enabled=chain_enabled,
            cover_mode=cover_mode,
            cover_discount_pct=float(cover_discount) if cover_discount not in (None, "") else None,
            cover_timeout_hours=float(cover_timeout) if cover_timeout not in (None, "") else None,
            call_enabled=call_enabled and chain_enabled,
            tp_call_pct=float(tp_call) if tp_call not in (None, "") else None,
            settle_window_min=int(settle_win) if settle_win not in (None, "")
            else DEFAULT_SETTLE_WINDOW_MIN,
        )
        return d.run()

    _run_guarded(run_id, prep=_prep, run=_run)


_DEFAULT_OPT_TD_BARS = 120


def _opt_ts_seconds(value: str | None) -> int | None:
    """ISO 字符串 → 秒级时间戳（期权 driver 的口径）。

    现货 driver 吃 datetime，期权 driver 直接 ``int(end_ts or time.time())``
    —— 两者单位不同，传错就是在引擎内部报一个看不出根因的 TypeError（首版
    实测：``int() argument must be a string ... not 'datetime.datetime'``）。
    """
    d = _parse_ts(value)
    return int(d.timestamp()) if d else None

# 期权回测可覆盖的策略参数键（数值型；键名对齐 DEFAULT_STRATEGY）
_OPT_NUM_KEYS = (
    "td_bars", "entry_setup", "entry_countdown",
    "max_contracts_per_family", "max_contracts_total",
    "iv_min_percentile", "take_profit_pct", "iv_pct_window_days",
)
_OPT_INT_KEYS = (
    "td_bars", "entry_setup", "entry_countdown",
    "max_contracts_per_family", "max_contracts_total",
)
# 选档器参数（进 selector 子字典）
_OPT_SELECTOR_KEYS = (
    "min_distance_pct", "delta_min", "delta_max",
    "expiry_min_days", "expiry_max_days", "min_net_yield_pct", "top_n",
)


def _merge_opt_params(overrides: dict | None, timestep: str | None = None) -> dict:
    """把页面覆盖合并进实盘策略参数的**副本**（不污染全局配置）。

    ``timestep`` 同时写进 ``td_period``：策略算 TD 用的信号周期必须与重放 bar
    粒度一致，否则回测跑的是「15m bar 上算 5m 信号」的错配组合。

    另注单位陷阱：期权 driver 的 ``start_ts`` / ``end_ts`` 是**秒级 int**
    （内部直接 ``int(end_ts)``），现货 driver 用 datetime —— 转换由调用方
    ``_auto_backtest_options`` 负责。
    """
    from nanobot_quant.okx_options_live import _strategy_params, live_config

    try:
        base = dict(_strategy_params(live_config()) or {})
    except Exception:  # noqa: BLE001 —— 配置缺失不阻塞回测
        base = {}
    sel = dict(base.get("selector") or {})
    for k, v in (overrides or {}).items():
        if v is None or v == "":
            continue
        if k in _OPT_SELECTOR_KEYS:
            sel[k] = float(v)
        elif k in _OPT_INT_KEYS:
            base[k] = int(v)
        elif k in _OPT_NUM_KEYS:
            base[k] = float(v)
        else:
            base[k] = v
    if sel:
        base["selector"] = sel
    if timestep:
        base["td_period"] = str(timestep)
    return base


def run_backtest(
    symbol: str | None = None,
    start: str | None = None,
    end: str | None = None,
    quantity: int = 10,
    source: str | None = None,
    engine: str = "backtest_runner",
    scene: str | None = None,
    symbols: list[str] | None = None,
    initial_quote: float | None = None,
    batches: int | None = None,
    slippage: float | None = None,
    fixed_amount: float | None = None,
    overrides: dict | None = None,
    family: str | None = None,
    timestep: str | None = None,
    initial_cash: float | None = None,
    td_bars: int | None = None,
    periods: list[str] | None = None,
    shared_cash: bool | None = None,
    preset: str | None = None,
    quantity_mode: str | None = None,
) -> dict:
    """Start a full backtest in the background (run_id + poll contract).

    Args:
        symbol: Token symbol, e.g. "SOL/USDC" or "CRCLX/USDC" (legacy engine)
        start: Start date, e.g. "2026-01-01"
        end: End date, e.g. "2026-07-05"
        quantity: Trade quantity (legacy engine, default 10)
        source: 数据源（按 engine 语义不同）：
                - legacy engine: registry name "onchainos"（默认）/
                  "okx_cex" / "yfinance"(alias "yahoo")
                - engine="driver": "okx_cex"（默认，与执行通道同源）或
                  "gate_cex"（是否跨所由 execution_channel 解析结果决定；
                  跨所仅作参数粗筛、不作裁决依据）
        engine: "backtest_runner" (legacy lumibot engine, default — zero
                behaviour change), "driver" (Step 3 replay driver:
                scene-based replay on gate_cex/okx_cex history, same decision
                code as live), or "options" (期权卖 put 归档重放：官方归档成交
                → IV 曲面 → 与实盘期权线同一份策略函数).
        scene: Scene name (high/mid/low) — engine="driver" only.
        symbols: Override the scene symbol pool — engine="driver" only.
        initial_quote: Per-slot simulated starting USDT — engine="driver".
        batches: Override scene batch count — engine="driver" only.
        slippage: Override global slippage — engine="driver" only.
        fixed_amount: Override per-trade fixed USDT amount (quantity_mode=
                "fixed_amount") — engine="driver" only; None = scene config.
        overrides: Extra strategy-parameter overrides for this backtest only
                (e.g. {"sell_only_profit_high": 0.005, "exit_setup": 10}).
                Keys absent from the dict fall back to scene config, then
                global exec_params, then class defaults. Never written back
                to exec_params.json — 2026-08-30 拍板.
        family: Option family for engine="options", e.g. "SOL-USD_UM".
        timestep: Bar size for engine="options" (default "15m").
        initial_cash: Simulated starting USDC for engine="options"
                (default 10000).
        td_bars: TD window size for engine="options" (default 120).
        periods: 多周期对比（2026-10-21）——engine="driver" 专用。
                传 ≥2 个周期（如 ["1m","3m","5m"]）→ 各档**独立回放**一次
                （同一区间、同一份参数、各自独立资金/台账），结果里返回
                ``compare`` 对比表 + ``period_results`` 每档完整结果。
                传 1 个 = 单周期覆盖（等价于 overrides.sleeptime）。
                同参数 = **同根数**，不是同时间（min_hold_bars=10 在 1m 是 10 分钟、
                在 5m 是 50 分钟）。跨周期结论不可互推，对比表只作并列展示。
        preset: 规则预设名（C118，2026-11-06），见
                ``nanobot_quant.backtest.presets``。选中后先用 preset 的
                ``overrides`` 填满一套键，再用本次显式 overrides 覆盖；
                ``run`` 段（scene/symbols/区间/周期/数据源/资金/滑点/
                共享池）只在对应参数未传时兜底。实际生效的 preset 名 +
                版本 + 本次覆盖键写入 ``backtest_config``，保证可复现。
        quantity_mode: 数量模式覆盖（C119）：fixed/fixed_amount/value。
                传了会并入 overrides（与 overrides 里的同名字段等价），
                避免「顶层传了但静默忽略、实际跑的是场景值」。

    Returns:
        dict with status=started and run_id. The backtest runs in a
        background thread (a real run exceeds the 30s MCP tool timeout);
        poll ``get_backtest_result(run_id)`` for the outcome.
    """
    # 两个入口（MCP / WebUI）共用同一份归一化，字符串 "false"/"10"
    # 在这里就转成正确类型，绝不再原样透传给策略层。
    try:
        overrides = coerce_overrides(overrides)
        # 数量模式：顶层显式传入优先（C119）——此前顶层根本没有该参数，
        # 调用方传了会被静默忽略，结果按场景值跑，与参数表不符。
        if quantity_mode is not None:
            overrides = {
                **overrides,
                **coerce_overrides({"quantity_mode": quantity_mode}),
            }
    except ValueError as exc:
        return {
            "error": str(exc),
            "hint": "overrides 需为 JSON 对象；int/float/bool 字段可传字符串但要能解析。",
        }

    preset_meta: dict | None = None
    if preset:
        # 规则预设（C118）：preset 打底、显式覆盖优先；run 级参数只在
        # 对应值未传时兜底（「页面填了就以页面为准」）。
        try:
            from nanobot_quant.backtest.presets import expand_preset

            overrides, preset_meta = expand_preset(preset, overrides)
            # 标的池属 run 级（与 batches 同类）：由 run.symbols / 显式 symbols
            # 参数控制，不从 overrides 覆盖，避免 preset 冲掉页面上手选的口径。
            overrides.pop("symbols", None)
            # preset 内部值也要过一遍类型/枚举校验，防 preset 本身写错。
            overrides = coerce_overrides(overrides)
        except ValueError as exc:
            return {
                "error": str(exc),
                "hint": "preset 需为已注册名称；用 presets_meta() 查看可用列表。",
            }
        _run = preset_meta.get("run") or {}
        scene = scene or _run.get("scene")
        symbols = symbols or _run.get("symbols")
        start = start or _run.get("start")
        end = end or _run.get("end")
        periods = periods or _run.get("periods")
        source = source or _run.get("source")
        if initial_quote is None:
            initial_quote = _run.get("initial_quote")
        if batches is None:
            batches = _run.get("batches")
        if fixed_amount is None:
            fixed_amount = _run.get("fixed_amount")
        if slippage is None:
            slippage = _run.get("slippage")
        if shared_cash is None:
            shared_cash = _run.get("shared_cash")

    scene = scene or "mid"
    if initial_quote is None:
        initial_quote = DEFAULT_INITIAL_QUOTE
    shared_cash = bool(shared_cash)

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    if engine == "options":
        # 前缀区分两套引擎的结果，页面历史分开列（见 _opt_runs）
        run_id = f"opt-{run_id}"
        fam = (family or symbol or "").upper().strip()
        if not fam:
            return {
                "error": "engine='options' requires family (e.g. 'SOL-USD_UM')",
                "hint": "示例：family='SOL-USD_UM', timestep='15m', start/end 指定区间。",
            }
        threading.Thread(
            target=_auto_backtest_options,
            args=(run_id, fam, timestep or "15m", start, end,
                  float(initial_cash or 10000.0), td_bars, slippage, overrides),
            daemon=True,
        ).start()
    elif engine == "driver":
        try:
            from nanobot_quant.backtest.fetchers import normalize_source

            driver_source = normalize_source(source)
        except ValueError as exc:
            return {"error": str(exc)}
        syms = list(symbols) if symbols else ([symbol] if symbol else None)
        _periods = [str(p) for p in (periods or []) if p]
        _bad_req = _validate_periods(_periods)
        if _bad_req:
            return _bad_req
        # 策略变体（2026-11-06）：preset 固定则强制生效；未用 preset 时
        # 传 None，由 driver 跟随空间 strategy.json（保持旧行为）。
        _variant = ((preset_meta or {}).get("run") or {}).get("strategy_variant")
        threading.Thread(
            target=_auto_backtest_driver,
            args=(run_id, scene, syms, start, end, initial_quote, batches,
                  slippage, fixed_amount, overrides, driver_source, _periods,
                  shared_cash, preset_meta, _variant),
            daemon=True,
        ).start()
    else:
        if not symbol:
            return {
                "error": "engine='backtest_runner' requires symbol",
                "hint": "Pass symbol (e.g. 'SOL/USDC'), or use engine='driver' with scene.",
            }
        threading.Thread(
            target=_auto_backtest,
            args=(run_id, symbol, start, end, quantity, source or "onchainos"),
            daemon=True,
        ).start()
    return {
        "status": "started",
        "run_id": run_id,
        "engine": engine,
        "message": (
            "Backtest started in background. Poll get_backtest_result("
            f"run_id=\"{run_id}\") for the outcome — a non-trivial range "
            "exceeds the 30s MCP tool timeout, so results are written to "
            "{data_root}/legion/backtests/<run_id>.json."
        ),
    }


def list_backtest_presets() -> dict:
    """List rule presets available to ``run_backtest(preset=…)``（C118）。

    Returns ``{"presets": [{name, version, label, description}, …]}``.
    Preset 是**只读**的代码内置口径：改口径 = 新增版本（v1 → v2），旧版本
    保留，所以写在历史结果里的 ``preset=xxx@v1`` 永远可复现。要临时改
    单个参数，请直接在 ``run_backtest(overrides={…})`` 里覆盖——显式覆盖
    优先级永远高于 preset。
    """
    try:
        from nanobot_quant.backtest.presets import presets_meta

        return {"presets": presets_meta()}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"读取 preset 表失败: {exc}", "presets": []}


def get_backtest_result(run_id: str) -> dict:
    """Return the persisted outcome of a background backtest ``run_id``.

    Reads ``{data_root}/legion/backtests/<run_id>.json``.  Returns a hint
    when the file is not there yet (still running / never started).

    Also attaches a ``markdown`` field with the same outcome rendered as
    markdown (``nanobot_quant.backtest_markdown``) so the WebUI can offer a
    one-click copy and agents can read the summary directly.  Rendering
    failures never affect the result itself, and running/errored payloads
    get no ``markdown`` key.

    Note the persisted file has two shapes and both must work:

    * background runs (the common case) write
      ``{"status": "done", "run_id": ..., "result": {...}}`` — the markdown
      belongs on the **inner** ``result`` dict, because that is what the
      WebUI feeds to its renderer and what agents read as the outcome.
    * bare result dicts (older records / other callers) get it at top level.
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        p = backtests_dir() / f"{run_id}.json"
        if not p.is_file():
            return {
                "error": f"no backtest result for run_id={run_id}",
                "hint": "The backtest may still be running, or the run_id is wrong.",
            }
        payload = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("run_id", run_id)
            inner = payload.get("result")
            target = inner if isinstance(inner, dict) else payload
            target.setdefault("run_id", run_id)
            try:
                from nanobot_quant.backtest_markdown import render_markdown

                md = render_markdown(target)
                if md:
                    target["markdown"] = md
            except Exception:  # noqa: BLE001  # markdown 只是 UX 增强
                pass
        return payload
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read backtest result for {run_id}: {exc}"}
