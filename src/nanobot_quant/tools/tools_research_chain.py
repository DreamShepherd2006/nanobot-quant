"""run_research_chain: composite tool — swarm debate → structurize → TD → execute.

Direct handoff (no vt_research agent orchestration after the single call):

    run_research_chain(symbol)
      ├─ 1. start VT investment_committee swarm (async, start_only)
      ├─ 2. background thread polls .swarm/runs/<run_id>/run.json
      ├─ 3. on completion: read final_report (natural-language debate)
      ├─ 4. structurize_signal(final_report, symbol)   → TickerSignal (LLM)
      ├─ 5. run_td_sequential(address)                 → TD technical check
      ├─ 6. compare swarm direction vs TD signal direction (must agree)
      └─ 7. execute_signal(signal, live)               → Risk → Portfolio → Order

The tool returns immediately with the swarm run_id; all downstream steps
run deterministically in a background thread, so no LLM turn is needed
between the debate result and the execution chain.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

# ── symbol normalisation (mirrors nanobot-legion squad_delegate) ──

_PAIR_SUFFIXES = ("-USDT", "-USDC", "-USD", "-BTC", "-ETH")
_STOCK_SUFFIXES = (".US", ".HK", ".L")


def _normalize_pair(raw: str) -> str:
    """BTC → BTC-USDT, ETH-USD → ETH-USD, SPCX → SPCX, AAPL.US → AAPL.US."""
    stripped = str(raw or "").strip().upper()
    if not stripped:
        return str(raw or "")
    for suffix in _PAIR_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped
    if any(stripped.endswith(ex) for ex in _STOCK_SUFFIXES):
        return stripped
    return f"{stripped}-USDT"


def _bare_symbol(pair: str) -> str:
    """BTC-USDT → BTC, AAPL.US → AAPL."""
    for sep in ("-", "."):
        if sep in pair:
            return pair.split(sep)[0]
    return pair


# ── background auto-chain ─────────────────────────────────────────

def _auto_chain(run_id: str, symbol: str, chain: str, live: bool) -> None:
    """Background thread: poll swarm run, then run the deterministic chain."""
    from src.swarm.store import SwarmStore, swarm_runs_root  # vibe_trading

    store = SwarmStore(base_dir=swarm_runs_root())
    terminal = {"completed", "failed", "cancelled"}

    # ① poll until the debate finishes (30s interval, up to ~3h)
    deadline = time.time() + 3 * 3600
    run = None
    while time.time() < deadline:
        run = store.load_run(run_id)
        if run is None:
            time.sleep(30)
            continue
        if run.status.value in terminal:
            break
        time.sleep(30)

    if run is None or run.status.value != "completed":
        _chain_log(run_id, {"status": "blocked", "reason": f"swarm not completed: {getattr(run, 'status', 'missing')}"})
        return

    report = (run.final_report or "").strip()
    if not report:
        _chain_log(run_id, {"status": "blocked", "reason": "swarm final_report empty"})
        return

    # ② structurize (LLM extraction: natural language → TickerSignal)
    from nanobot_quant.tools.tools_structurize import structurize_signal

    signal = structurize_signal(report, symbol)
    if not isinstance(signal, dict) or "error" in signal:
        _chain_log(run_id, {"status": "blocked", "reason": f"structurize failed: {signal}"})
        return

    # Normalize recommendation to uppercase (LLM may emit lowercase)
    signal["recommendation"] = str(signal.get("recommendation", "HOLD")).upper()

    # ③ TD technical check (required — fail-closed if unavailable)
    from nanobot_quant.onchainos_cli import resolve_token_address

    addr = resolve_token_address(symbol)
    if not addr:
        _chain_log(run_id, {
            "status": "blocked",
            "reason": f"cannot resolve on-chain address for {symbol}; TD check unavailable",
        })
        return

    from nanobot_quant.tools.tools_analysis import run_td_sequential

    td_signal = run_td_sequential(addr, chain=chain)
    if not isinstance(td_signal, dict) or "error" in td_signal:
        _chain_log(run_id, {
            "status": "blocked",
            "reason": f"TD check failed: {td_signal}",
        })
        return

    # ④ swarm direction must agree with TD signal direction
    def _primary_dir(raw: str) -> str:
        """BUY (Setup Complete) → BUY; hold → HOLD."""
        return str(raw).split(" ")[0].strip().upper()

    swarm_dir = _primary_dir(signal.get("recommendation", "HOLD"))
    td_dir = _primary_dir(td_signal.get("recommendation", "HOLD"))
    if swarm_dir != td_dir:
        _chain_log(run_id, {
            "status": "blocked",
            "reason": f"direction mismatch: swarm={swarm_dir} td={td_dir}",
            "signal": signal,
            "td_signal": td_signal,
        })
        return

    # ⑤ execute through the deterministic pipeline
    from nanobot_quant.tools.tools_execute import execute_signal

    result = execute_signal(json.dumps(signal, ensure_ascii=False), live=live)
    _chain_log(run_id, {"status": "executed", "run_id": run_id, "signal": signal, "result": result})


def _chain_log(run_id: str, payload: dict) -> None:
    """Persist chain outcome to ``{data_root}/legion/research_chains``.

    Written to the persistent audit directory (independent from credentials;
    survives Factory Rebuild) and mirrored to the MCP server stderr.
    """
    try:
        from nanobot_quant.onchainos_cli import chain_results_dir

        out_dir = chain_results_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — logging must never break the chain
        print(f"[DIAG] _chain_log failed: {exc}", file=sys.stderr, flush=True)
    print(f"[DIAG] run_research_chain {run_id}: {json.dumps(payload, ensure_ascii=False)[:800]}", file=sys.stderr, flush=True)


def get_chain_result(run_id: str) -> dict:
    """Return the persisted chain outcome for a swarm ``run_id``.

    Reads ``{data_root}/legion/research_chains/<run_id>.json`` (the file
    written by ``run_research_chain``'s background auto-chain).  Lets agents
    and the WebUI audit whether the debate was executed, blocked, or still
    pending — without touching the swarm run directory in site-packages.
    """
    try:
        from nanobot_quant.onchainos_cli import chain_results_dir

        p = chain_results_dir() / f"{run_id}.json"
        if not p.is_file():
            return {
                "error": f"no chain result for run_id={run_id}",
                "hint": "The swarm may still be running, or the auto-chain has not finished yet.",
            }
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"failed to read chain result for {run_id}: {exc}"}


# ── composite tool ────────────────────────────────────────────────

def run_research_chain(
    symbol: str,
    *,
    chain: str = "solana",
    live: bool = False,
    max_iterations: int = 50,
) -> dict:
    """Start a VT investment_committee swarm debate and auto-chain the result.

    Single-call research-to-execution entry point. Returns immediately with
    the swarm ``run_id``; a background thread waits for the debate to finish
    and then runs, entirely in code:

        structurize_signal(final_report) → TickerSignal
        run_td_sequential(address)       → TD technical check
        execute_signal(signal, live)     → Risk → Portfolio → Order

    Args:
        symbol: Token symbol, e.g. "BTC", "SPCX", "ETH-USD".
        chain: Chain for the TD technical check (default "solana").
        live: Request on-chain execution (default False; still gated by the
              WebUI live toggle — AND condition).
        max_iterations: Max swarm iterations (default 50).

    Returns:
        dict with status=started, run_id, auto_chain=True. The final chain
        outcome is written to ``{data_root}/legion/research_chains/<run_id>.json``
        (query it via ``get_chain_result``) and mirrored to the MCP server
        stderr.  If the symbol cannot be resolved on-chain, returns
        status=error immediately WITHOUT starting the swarm.
    """
    # ① start the swarm (direct library use, not the vibe-trading MCP server)
    try:
        from src.config import load_swarm_agent_config
        from src.swarm.runtime import SwarmRuntime
        from src.swarm.store import SwarmStore, swarm_runs_root
    except ImportError as exc:  # pragma: no cover
        return {"status": "error", "error": f"vibe_trading unavailable: {exc}"}

    pair = _normalize_pair(symbol)
    bare = _bare_symbol(pair)

    # ── fail-closed pre-check: token must be resolvable on-chain ──
    # Runs BEFORE starting the swarm so an unsupported symbol fails fast
    # (no 15-40 min debate wasted).  This is an optimisation gate; the
    # authoritative safety gate lives in pipeline.run_from_signals() and
    # covers every execution path.
    from nanobot_quant.onchainos_cli import (
        resolve_token_address,
        supported_symbols,
    )
    addr = resolve_token_address(bare)
    if not addr:
        return {
            "status": "error",
            "error": f"{bare} is not supported on {chain} chain (cannot resolve on-chain address)",
            "supported": supported_symbols(),
            "hint": (
                "Configure a wrapped token address in WebUI 业务管理 → tokens.json, "
                "or use a native token like SOL/USDC/USDT."
            ),
        }

    swarm_dir = swarm_runs_root()
    store = SwarmStore(base_dir=swarm_dir)
    agent_config = load_swarm_agent_config()
    runtime = SwarmRuntime(store=store, agent_config=agent_config)

    variables = {
        "target": pair,
        "market": "stock" if any(bare.endswith(ex) for ex in _STOCK_SUFFIXES) else "crypto",
        "max_iterations": str(max_iterations),
    }
    run = runtime.start_run("investment_committee", variables, include_shell_tools=False)
    run_id = run.id

    # ② register the background auto-chain
    threading.Thread(
        target=_auto_chain,
        args=(run_id, bare, chain, live),
        daemon=True,
    ).start()

    return {
        "status": "started",
        "run_id": run_id,
        "auto_chain": True,
        "message": (
            "Swarm started; auto-chain registered. On completion the code "
            "will structurize → TD check → execute automatically."
        ),
    }
