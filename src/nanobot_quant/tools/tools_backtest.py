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


def _parse_ts(value: str | None):
    """start/end → datetime。无时区输入明确按 UTC
    （页面提交已由前端把本地时间转成 UTC ISO）。"""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
) -> None:
    """New engine (backtest.driver): scene-based replay on Gate CEX history.

    Reuses the live strategy decision code (same StrategyExecutor scene
    construction, BacktestBroker for simulated fills).  Same run_id +
    poll contract as the legacy engine.
    """
    from datetime import datetime

    def _prep() -> None:
        from nanobot_quant.backtest.driver import BacktestDriver  # noqa: F401

    def _run():
        from nanobot_quant.backtest.driver import BacktestDriver
        from nanobot_quant.onchainos_cli import backtests_dir

        # 进度文件 = 结果文件（<run_id>.json）：运行期间 driver 写
        # {status: running, progress}，返回后 _backtest_log 覆写 done/error
        d = BacktestDriver(
            scene=scene,
            symbols=symbols,
            start_ts=_parse_ts(start),
            end_ts=_parse_ts(end),
            initial_quote=initial_quote,
            batches=batches,
            slippage=slippage,
            fixed_amount=fixed_amount,
            overrides=overrides,
            progress_path=backtests_dir() / f"{run_id}.json",
        )
        return d.run()

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
            DEFAULT_SLIPPAGE_PCT,
            OptionsBacktestDriver,
        )

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
            opt_params=_merge_opt_params(overrides, timestep),
            progress_cb=_progress,
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
    "iv_min_percentile", "take_profit_pct",
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
    source: str = "onchainos",
    engine: str = "backtest_runner",
    scene: str = "mid",
    symbols: list[str] | None = None,
    initial_quote: float = 100.0,
    batches: int | None = None,
    slippage: float | None = None,
    fixed_amount: float | None = None,
    overrides: dict | None = None,
    family: str | None = None,
    timestep: str | None = None,
    initial_cash: float | None = None,
    td_bars: int | None = None,
) -> dict:
    """Start a full backtest in the background (run_id + poll contract).

    Args:
        symbol: Token symbol, e.g. "SOL/USDC" or "CRCLX/USDC" (legacy engine)
        start: Start date, e.g. "2026-01-01"
        end: End date, e.g. "2026-07-05"
        quantity: Trade quantity (legacy engine, default 10)
        source: Data source registry name for the legacy engine:
                "onchainos", "okx_cex", "yfinance" (alias "yahoo"), or
                "gate_cex" (not implemented — returns a clear error).
        engine: "backtest_runner" (legacy lumibot engine, default — zero
                behaviour change), "driver" (Step 3 replay driver:
                scene-based, Gate CEX history, same decision code as live),
                or "options" (期权卖 put 归档重放：官方归档成交 → IV 曲面 →
                与实盘期权线同一份策略函数).
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

    Returns:
        dict with status=started and run_id. The backtest runs in a
        background thread (a real run exceeds the 30s MCP tool timeout);
        poll ``get_backtest_result(run_id)`` for the outcome.
    """
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
        syms = list(symbols) if symbols else ([symbol] if symbol else None)
        threading.Thread(
            target=_auto_backtest_driver,
            args=(run_id, scene, syms, start, end, initial_quote, batches, slippage, fixed_amount, overrides),
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
            args=(run_id, symbol, start, end, quantity, source),
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


def get_backtest_result(run_id: str) -> dict:
    """Return the persisted outcome of a background backtest ``run_id``.

    Reads ``{data_root}/legion/backtests/<run_id>.json``.  Returns a hint
    when the file is not there yet (still running / never started).
    """
    try:
        from nanobot_quant.onchainos_cli import backtests_dir

        p = backtests_dir() / f"{run_id}.json"
        if not p.is_file():
            return {
                "error": f"no backtest result for run_id={run_id}",
                "hint": "The backtest may still be running, or the run_id is wrong.",
            }
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read backtest result for {run_id}: {exc}"}
