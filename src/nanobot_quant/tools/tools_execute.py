"""execute_signal: signal → pipeline (Risk → Portfolio → Order → optional Broker).

Takes a structured TickerSignal (from run_td_sequential or structurize_signal)
and runs it through the full execution pipeline.

When ``live=True``, orders that pass risk are submitted to OnchainOSBroker
for on-chain swap execution.
"""

from __future__ import annotations

import json
import os
import sys


def execute_signal(ticker_signal_json: str, *, live: bool = False, confirm: bool = False, portfolio_value: float = 100000.0, quantity: float | None = None) -> dict:
    """Execute the trading pipeline on structured signal(s).

    Takes a JSON signal (from structurize_signal or run_td_sequential) and
    passes it through Risk → Position Sizing → Order generation.

    Accepts either a single signal object or a list of signals.

    Args:
        ticker_signal_json: JSON string of signal(s).
        live: If True, submit orders to OnchainOSBroker for on-chain
              execution.  Default False (paper-only).
        confirm: Explicit user confirmation for a questionable tokens.json
                 entry (default False).  When a token needs confirmation
                 this returns error=needs_confirmation without executing;
                 pass confirm=true only after the user confirmed (the
                 confirmation is persisted so later runs pass automatically).
        portfolio_value: Hypothetical portfolio value (USD) used for
                 position sizing (default 100000 → 20% cap ≈ $20k).
                 Pass a small value (e.g. 100 → $20 cap) for manual
                 verification swaps to avoid large live orders.
        quantity: Optional explicit order quantity (float allowed, e.g.
                 0.058).  When given, it overrides position sizing
                 (portfolio_value is then only used for risk checks).
                 Default None keeps the existing sizing behaviour.

    Returns:
        Pipeline execution results with risk checks and suggested orders.
        When live=True, includes tx_hash and broker_status fields.
    """
    # ── Guard MCP stdio from library import-time logging ─────────
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from nanobot_quant.pipeline import run_from_signals
    finally:
        sys.stdout = _saved_stdout

    # ── Silence lumibot logger tree AFTER import ──────────────────
    # lumibot registers its own stdout handlers at import time (e.g.
    # lumibot.brokers.broker telemetry). The startup-time cleanup in
    # signal_mcp_server.py runs BEFORE this lazy import, so it can't
    # see these loggers. Re-clean here once lumibot is loaded.
    _silence_lumibot_loggers()

    # ── WebUI master switch (AND gate) ──────────────────────────
    webui_live = _read_webui_live()
    effective_live = bool(live) and webui_live
    if live and not webui_live:
        print(
            "[DIAG] execute_signal: live requested but WebUI toggle is OFF — forcing paper",
            file=sys.stderr, flush=True,
        )

    print(f"[DIAG] execute_signal: live={live}, webui_live={webui_live}, effective_live={effective_live}", file=sys.stderr, flush=True)

    try:
        raw = json.loads(ticker_signal_json)
        # Defensive: LLM 客户端有时会把 JSON 字符串再包一层引号，
        # 导致 json.loads 返回 str 而非 dict/list — 二次解析兜底。
        if isinstance(raw, str):
            raw = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON input"}

    # Normalise to list of dicts
    signal_list: list[dict] = raw if isinstance(raw, list) else [raw]

    # Validate each signal is a dict (nested-string / scalar input is a
    # caller bug — fail fast with a clear message instead of a confusing
    # AttributeError deep in the pipeline).
    for s in signal_list:
        if not isinstance(s, dict):
            return {
                "error": f"Signal entries must be JSON objects, got {type(s).__name__}"
            }
        if "ticker" not in s:
            return {"error": f"Missing 'ticker' in signal: {s}"}

    ticker_summary = [s.get("ticker", "?") for s in signal_list]
    print(
        f"[DIAG] execute_signal: running pipeline on {ticker_summary}",
        file=sys.stderr, flush=True,
    )

    tokens_json = _load_tokens(effective_live)

    # ── Token confirmation gate (A + C) ──────────────────────────
    # tokens.json entries are trusted but validated: questionable entries
    # (bad address format / EVM address on solana chain) require explicit
    # user confirmation before anything executes.  confirm=True (only after
    # the user confirmed) persists confirmed=true so later runs pass without
    # asking again.  Fail-closed: any unresolved token aborts the whole batch.
    from nanobot_quant.onchainos_cli import confirm_token, resolve_token

    for s in signal_list:
        bare = s.get("ticker", "")
        resolved = resolve_token(bare, tokens_json=tokens_json)
        if not resolved.get("ok"):
            return {
                "error": f"token '{bare}' cannot be resolved on-chain ({resolved.get('category')})",
                "suggestion": resolved.get("suggestion"),
                "hint": resolved.get("hint"),
            }
        if resolved.get("needs_confirmation") and not confirm:
            return {
                "error": "needs_confirmation",
                "token": bare,
                "address": resolved.get("address"),
                "issue": resolved.get("issue"),
                "hint": "If the user confirms this tokens.json entry, re-run with confirm=true.",
            }
        if resolved.get("needs_confirmation") and confirm:
            confirm_token(bare, address=resolved.get("address"))

    try:
        # TD 自主循环状态同步（WebUI 保存之外的兜底：execute_signal 入口
        # 按 exec_params 同步启停，防保存后未触发的窗口期）
        from nanobot_quant.td_live import sync_from_params

        sync_from_params()
        # Route EVERYTHING (import-time AND runtime loggers) to stderr while
        # the pipeline runs. lumibot registers stdout handlers lazily during
        # LumiBot startup, so wrapping the call is the only reliable guard.
        from contextlib import redirect_stdout

        # Disable lumibot runtime telemetry (background thread emitting
        # LUMIBOT_TELEMETRY lines every interval via a stdout-bound handler —
        # redirect_stdout cannot touch it because the StreamHandler captured
        # the original stdout object at import time).
        os.environ.setdefault("LUMIBOT_TELEMETRY", "0")

        _saved_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            results = run_from_signals(
                signal_list,
                live=effective_live,
                tokens_json=tokens_json,
                confirm=confirm,
                portfolio_value=portfolio_value,
                quantity=quantity,
            )
        finally:
            sys.stdout = _saved_stdout
            # lumibot is imported lazily inside run_from_signals; the earlier
            # _silence_lumibot_loggers() call ran before any lumibot logger
            # existed. Clean up now that import + broker construction happened
            # (drops stdout handlers, sets WARNING level).
            _silence_lumibot_loggers()
        summary: dict = {"results": results, "count": len(results)}

        if live and not webui_live:
            summary["live_blocked"] = (
                "live 已请求但 WebUI 实盘开关未开启（/config/live），订单已强制走纸面路径"
            )

        if effective_live:
            submitted = sum(1 for r in results if r.get("tx_hash"))
            summary["submitted_on_chain"] = submitted
            print(
                f"[DIAG] execute_signal done: {submitted}/{len(results)} submitted",
                file=sys.stderr, flush=True,
            )
        return summary
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {"error": f"Pipeline execution failed: {exc}"}


def _read_webui_live() -> bool:
    """Read the WebUI live-trading master switch (live.json).

    Stored alongside API credentials at {data_root}/credentials/live.json.
    This is the master gate — execute_signal can only go on-chain when
    both the agent passed live=True AND this file says live=true.
    """
    paths = [
        "/data/legion/credentials/live.json",
        "/mnt/workspace/legion/credentials/live.json",
    ]
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    data = json.load(f)
                return bool(data.get("live", False))
            except Exception as exc:
                print(
                    f"[DIAG] execute_signal: failed to read {p}: {exc}",
                    file=sys.stderr, flush=True,
                )
    print(
        "[DIAG] execute_signal: live.json not found — live trading disabled",
        file=sys.stderr, flush=True,
    )
    return False


def _load_tokens(live: bool) -> list[dict] | None:
    """Load user-configured token mappings when live mode is requested."""
    if not live:
        return None
    from nanobot_quant.tokens_store import load_tokens_json

    data = load_tokens_json()
    if data:
        print(
            f"[DIAG] execute_signal: loaded {len(data)} token(s) from tokens.json",
            file=sys.stderr, flush=True,
        )
        return data
    print(
        "[DIAG] execute_signal: no tokens.json, relying on CLI resolution",
        file=sys.stderr, flush=True,
    )
    return None


def _silence_lumibot_loggers() -> None:
    """Clear stdout-bound handlers on the whole lumibot logger tree.

    lumibot.brokers.broker and friends register their own StreamHandler
    (bound to stdout) at import time. Any log line they emit then lands
    on the MCP stdio JSON-RPC channel and breaks message parsing. Remove
    those handlers and force propagation to the root logger (which the
    MCP server has already pointed at stderr).
    """
    import logging

    for _name in list(logging.Logger.manager.loggerDict):
        if _name == "lumibot" or _name.startswith("lumibot."):
            _lg = logging.getLogger(_name)
            _lg.handlers.clear()
            _lg.propagate = True
            _lg.setLevel(logging.WARNING)

def get_execution_outcome(order_id: str) -> dict:
    """[退役] Loop 模式已由 P2 B3 移除 — execute_signal 仅同步直调。"""
    return {"order_id": order_id, "status": "retired",
            "error": "loop 模式已退役（P2 B3），execute_signal 现在总是同步执行",
            "hint": "结果直接返回在 execute_signal 的响应中"}
