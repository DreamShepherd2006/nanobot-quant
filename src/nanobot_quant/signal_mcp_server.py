"""MCP server: signal-structurizer — quant / vt_research execution tools.

Protocol: stdio JSON-RPC (MCP).  No external MCP SDK required.

Tool implementations live in tools/:
  tools_wallet.py      wallet_setup, wallet_login_status, wallet_login_init, ...
  tools_analysis.py    run_td_sequential
  tools_backtest.py    run_backtest
  tools_structurize.py structurize_signal
  tools_execute.py     execute_signal
"""

from __future__ import annotations

import json
import logging
import sys

# ── Suppress library stdout during imports ──────────────────────
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
    wallet_login_raw_diag,
    wallet_login_status,
    wallet_payment_set,
    wallet_setup,
)
from nanobot_quant.tools.tools_analysis import run_td_sequential
from nanobot_quant.tools.tools_backtest import run_backtest
from nanobot_quant.tools.tools_structurize import structurize_signal
from nanobot_quant.tools.tools_execute import execute_signal


# ── Tool registry ───────────────────────────────────────────────

_TOOLS = [
    {
        "name": "run_td_sequential",
        "description": (
            "Run TD Sequential analysis on a Solana token. "
            "Fetches daily K-line data from OnchainOS, "
            "computes DeMark TD Setup/Countdown/TDST/score, "
            "and returns a structured TickerSignal with "
            "recommendation (BUY/SELL/HOLD), setup count, "
            "countdown count, score, support/resistance levels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "Token contract address, e.g. XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
                },
                "chain": {
                    "type": "string",
                    "default": "solana",
                    "description": "Chain: solana, arbitrum, ethereum, base, bnb, optimism, polygon",
                },
                "bar": {
                    "type": "string",
                    "default": "1D",
                    "description": "Candle interval: 1m, 5m, 15m, 1H, 4H, 1D, 1W",
                },
                "limit": {
                    "type": "integer",
                    "default": 299,
                    "description": "Number of candles to fetch (max 299 per call)",
                },
            },
            "required": ["address"],
        },
    },
    {
        "name": "structurize_signal",
        "description": (
            "Convert VT Swarm investment committee debate transcript "
            "into a structured TickerSignal JSON. Call this after every "
            "swarm analysis to produce machine-readable signals for "
            "the Aggregator pipeline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "debate_text": {
                    "type": "string",
                    "description": "The full swarm debate transcript",
                },
                "ticker": {
                    "type": "string",
                    "description": "Ticker symbol analyzed, e.g. BTCUSDT, AAPL",
                },
            },
            "required": ["debate_text", "ticker"],
        },
    },
    {
        "name": "execute_signal",
        "description": (
            "Execute the trading pipeline on structured signal(s). "
            "Passes signal through Risk → Position Sizing → Order "
            "generation. Accepts a JSON signal string (single object "
            "or list), returns risk checks and suggested orders. "
            "Pass live=true to attempt on-chain execution — this only "
            "works if the WebUI live trading toggle (/config/live) is "
            "enabled; otherwise the order stays paper-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker_signal_json": {
                    "type": "string",
                    "description": (
                        "JSON string of signal(s) — a single TickerSignal "
                        "object or list of them. Expected fields: ticker, "
                        "recommendation, score, price."
                    ),
                },
                "live": {
                    "type": "boolean",
                    "description": (
                        "Request real on-chain execution (default false). "
                        "Effective only when the WebUI live trading toggle "
                        "is enabled; otherwise forced to paper."
                    ),
                    "default": False,
                },
            },
            "required": ["ticker_signal_json"],
        },
    },
    {
        "name": "run_backtest",
        "description": (
            "Run a full backtest on a token symbol. "
            "Resolves ticker → fetches historical K-lines → runs TD Sequential "
            "strategy → Lumibot backtest engine → returns performance metrics. "
            "One-shot: all steps run in a single call, no LLM orchestration needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Token symbol, e.g. SOL, CRCLx",
                },
                "start": {
                    "type": "string",
                    "description": "Start date YYYY-MM-DD, e.g. 2026-04-01",
                },
                "end": {
                    "type": "string",
                    "description": "End date YYYY-MM-DD, e.g. 2026-07-29",
                },
                "quantity": {
                    "type": "integer",
                    "default": 10,
                    "description": "Trade quantity per signal",
                },
                "source": {
                    "type": "string",
                    "default": "onchainos",
                    "description": "Data source: onchainos or yfinance",
                },
            },
            "required": ["symbol", "start", "end"],
        },
    },
    {
        "name": "wallet_login_init",
        "description": (
            "Initiate onchainos social (Google/Apple/email) wallet login. "
            "Returns a loginUrl that the user must open in a browser. "
            "After browser confirmation, call wallet_login_poll to complete. "
            "Required after every Factory Rebuild (session data lost). "
            "The keyring data is stored in ~/.onchainos/ (file-based on Linux)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wallet_login_poll",
        "description": (
            "Poll for social login completion. Blocks up to 310 seconds. "
            "Call this after the user confirms login in their browser. "
            "Returns session data on success."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "authSessionId from wallet_login_init (optional, auto-detected if omitted)",
                },
            },
        },
    },
    {
        "name": "wallet_payment_set",
        "description": (
            "Set onchainos payment default tier. Must call AFTER wallet login "
            "is complete (wallet_login_poll succeeded). Required for Market API "
            "tools (market_kline etc.) to work without QUOTA errors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "description": "Payment tier: basic or premium",
                },
            },
            "required": ["tier"],
        },
    },
    {
        "name": "wallet_setup",
        "description": (
            "One-shot onchainos wallet bootstrap. Call this REPEATEDLY until phase=done. "
            "First call starts login (returns login_url). After user authorizes in browser, "
            "call again to complete poll + payment setup. When fully done, returns phase=done."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wallet_login_status",
        "description": (
            "Check onchainos login and payment status without side effects. "
            "Returns: logged_in, payment_basic, payment_premium booleans."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_TOOL_DISPATCH = {
    "run_td_sequential": run_td_sequential,
    "structurize_signal": structurize_signal,
    "execute_signal": execute_signal,
    "run_backtest": run_backtest,
    "wallet_login_init": wallet_login_init,
    "wallet_login_poll": wallet_login_poll,
    "wallet_payment_set": wallet_payment_set,
    "wallet_setup": wallet_setup,
    "wallet_login_status": wallet_login_status,
}


# ── MCP stdio JSON-RPC ──────────────────────────────────────────

def _handle_request(msg: dict) -> dict | None:
    """Handle a single JSON-RPC request. Returns response dict or None for notifications."""
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = _TOOL_DISPATCH.get(tool_name)
        if handler is not None:
            result = handler(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def main() -> None:
    """Run the MCP stdio loop."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
