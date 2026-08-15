"""MCP server: signal-structurizer — quant / vt_research execution tools.

Protocol: stdio JSON-RPC (MCP), built on the official `mcp` Python SDK
(mcp.server.fastmcp.FastMCP) — same framework as squad-delegate.

Tool implementations live in tools/:
  tools_wallet.py      wallet_setup, wallet_login_status, wallet_login_init, ...
  tools_analysis.py    run_td_sequential
  tools_backtest.py    run_backtest
  tools_structurize.py structurize_signal
  tools_execute.py     execute_signal
"""

from __future__ import annotations

import logging
import os
import sys

# ── Suppress library stdout during imports ──────────────────────
# Disable lumibot runtime telemetry at the MCP-server process level: the
# emitter spawns a background thread writing LUMIBOT_TELEMETRY lines through
# a stdout-bound logging handler, which corrupts the stdio JSON-RPC channel.
# The env toggle is the only reliable kill-switch (handler-level cleanup is
# order-dependent because lumibot imports lazily inside tool calls).
os.environ.setdefault("LUMIBOT_TELEMETRY", "0")

# Backtest progress bar writes "\rProgress |…" to stdout WITHOUT a trailing
# newline: it shares the MCP stdio buffer with the JSON-RPC response and the
# merged line fails client-side JSON parsing → the response is lost → the
# 30s MCP tool timeout fires even though the backtest finished in 9s.
# Constants are read at lumibot import time, so set these at process level.
# BACKTESTING_QUIET_LOGS silences INFO logs ("LumiBot v4.5.78 starting",
# "Getting historical prices …") that otherwise flood the channel with
# parse-error noise.
os.environ.setdefault("BACKTESTING_SHOW_PROGRESS_BAR", "0")
os.environ.setdefault("BACKTESTING_QUIET_LOGS", "1")

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
# Clear handlers on the ENTIRE lumibot logger tree (sub-loggers like
# lumibot.brokers.broker register their own stdout handlers, polluting
# the MCP stdio JSON-RPC channel).
for _lg_name in list(logging.Logger.manager.loggerDict):
    if _lg_name == "lumibot" or _lg_name.startswith("lumibot."):
        _lg = logging.getLogger(_lg_name)
        _lg.handlers.clear()
        _lg.propagate = True
        _lg.setLevel(logging.WARNING)

SERVER_NAME = "signal-structurizer"
SERVER_VERSION = "2.0.0"

from nanobot_quant.tools.tools_wallet import (
    wallet_login_init,
    wallet_login_poll,
    wallet_payment_set,
    wallet_setup,
    wallet_status,
    wallet_addresses,
    wallet_balance,
    wallet_chains,
    wallet_history,
    wallet_add,
    wallet_switch,
    wallet_login_status,
)
from nanobot_quant.tools.tools_analysis import run_td_sequential
from nanobot_quant.tools.tools_backtest import run_backtest
from nanobot_quant.tools.tools_structurize import structurize_signal
from nanobot_quant.tools.tools_execute import execute_signal, get_execution_outcome
from nanobot_quant.tools.tools_research_chain import get_chain_result, run_research_chain

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(SERVER_NAME, log_level="WARNING")


# ── Tool registry ───────────────────────────────────────────────
# Descriptions are preserved verbatim from the pre-SDK hand-written
# schema (tool prompt quality must not regress).  Input schemas are
# now derived automatically from the function signatures via FastMCP.

_TOOL_DESCRIPTIONS = {
    "run_td_sequential": (
        "Run TD Sequential analysis on a Solana token. "
        "Fetches daily K-line data from OnchainOS, "
        "computes DeMark TD Setup/Countdown/TDST/score, "
        "and returns a structured TickerSignal with "
        "recommendation (BUY/SELL/HOLD), setup count, "
        "countdown count, score, support/resistance levels."
    ),
    "structurize_signal": (
        "Convert VT Swarm investment committee debate transcript "
        "into a structured TickerSignal JSON. Call this after every "
        "swarm analysis to produce machine-readable signals for "
        "the Aggregator pipeline."
    ),
    "execute_signal": (
        "Execute the trading pipeline on structured signal(s). "
        "Passes signal through Risk → Position Sizing → Order "
        "generation. Accepts a JSON signal string (single object "
        "or list), returns risk checks and suggested orders. "
        "Pass live=true to attempt on-chain execution — this only "
        "works if the WebUI live trading toggle (/config/live) is "
        "enabled; otherwise the order stays paper-only."
    ),
    "run_backtest": (
        "Run a full backtest on a token symbol. "
        "Resolves ticker → fetches historical K-lines → runs TD Sequential "
        "strategy → Lumibot backtest engine → returns performance metrics. "
        "One-shot: all steps run in a single call, no LLM orchestration needed."
    ),
    "wallet_login_init": (
        "Initiate onchainos social (Google/Apple/email) wallet login. "
        "Returns a loginUrl that the user must open in a browser. "
        "After browser confirmation, call wallet_login_poll to complete. "
        "Required after every Factory Rebuild (session data lost). "
        "The keyring data is stored in ~/.onchainos/ (file-based on Linux)."
    ),
    "wallet_login_poll": (
        "Poll for social login completion. Blocks up to 310 seconds. "
        "Call this after the user confirms login in their browser. "
        "Returns session data on success."
    ),
    "wallet_payment_set": (
        "Set onchainos payment default tier. Must call AFTER wallet login "
        "is complete (wallet_login_poll succeeded). Required for Market API "
        "tools (market_kline etc.) to work without QUOTA errors."
    ),
    "wallet_setup": (
        "One-shot onchainos wallet bootstrap. Call this REPEATEDLY until phase=done. "
        "First call starts login (returns login_url). After user authorizes in browser, "
        "call again to complete poll + payment setup. When fully done, returns phase=done."
    ),
    "wallet_login_status": (
        "Check onchainos login and payment status without side effects. "
        "Returns: logged_in, payment_basic, payment_premium booleans."
    ),
    "wallet_status": (
        "Show current onchainos wallet status: email, loginType, "
        "currentAccountId, currentAccountName, accountCount, policy."
    ),
    "wallet_addresses": (
        "List wallet addresses for the current account, grouped by chain "
        "category (XLayer, EVM, Solana). Optional --chain filter: chain "
        "name or ID (e.g. 'solana' or '501', 'ethereum' or '1')."
    ),
    "wallet_balance": (
        "Query onchainos wallet balances. Use all_accounts=true to query all "
        "accounts' assets; chain filters by chain name/ID; token_address "
        "filters by token contract (requires chain); force bypasses caches "
        "and re-fetches from API."
    ),
    "wallet_chains": (
        "List all chains supported by onchainos wallet (cached locally)."
    ),
    "wallet_history": (
        "Query onchainos wallet transaction history. Optional filters: chain "
        "(name/ID), address, limit (page size), page_num (page cursor)."
    ),
    "wallet_add": (
        "Create a new sub-wallet account (up to 50 per wallet)."
    ),
    "wallet_switch": (
        "Switch the active wallet account to the given account_id."
    ),
    "run_research_chain": (
        "All-in-one research-to-execution: starts a VT investment_committee "
        "swarm debate, then automatically chains structurize_signal -> "
        "run_td_sequential (TD check) -> execute_signal once the debate "
        "completes. No further agent orchestration needed after this call. "
        "Returns the swarm run_id immediately; the chain runs in a "
        "background thread and its outcome is written to "
        "<data_root>/legion/research_chains/<run_id>.json (query via "
        "get_chain_result). Fails fast (status=error, no swarm started) "
        "if the symbol is not a native/resolvable token on the chain."
    ),
    "get_chain_result": (
        "Return the persisted outcome of a run_research_chain execution: "
        "reads <data_root>/legion/research_chains/<run_id>.json.  Lets "
        "agents/WebUI audit whether the debate was executed, blocked, or "
        "still pending — without touching the swarm run directory."
    ),
    "get_execution_outcome": (
        "[退役] Loop 模式已由 P2 B3 移除 — execute_signal 现在总是同步直调，"
        "结果直接包含在 execute_signal 的响应中。此工具仅返回 retired 说明。"
    ),
}

_TOOL_DISPATCH = {
    "run_td_sequential": run_td_sequential,
    "structurize_signal": structurize_signal,
    "execute_signal": execute_signal,
    "run_research_chain": run_research_chain,
    "get_chain_result": get_chain_result,
    "get_execution_outcome": get_execution_outcome,
    "run_backtest": run_backtest,
    "wallet_login_init": wallet_login_init,
    "wallet_login_poll": wallet_login_poll,
    "wallet_payment_set": wallet_payment_set,
    "wallet_setup": wallet_setup,
    "wallet_login_status": wallet_login_status,
    "wallet_status": wallet_status,
    "wallet_addresses": wallet_addresses,
    "wallet_balance": wallet_balance,
    "wallet_chains": wallet_chains,
    "wallet_history": wallet_history,
    "wallet_add": wallet_add,
    "wallet_switch": wallet_switch,
}

for _tool_name, _tool_fn in _TOOL_DISPATCH.items():
    mcp.add_tool(_tool_fn, name=_tool_name, description=_TOOL_DESCRIPTIONS[_tool_name])


def main() -> None:
    """Run the MCP stdio server (official mcp SDK, no banner output)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
