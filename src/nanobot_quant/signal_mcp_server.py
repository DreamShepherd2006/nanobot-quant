"""MCP server: structurize VT Swarm debate → TickerSignal JSON.

Provides one tool: ``structurize_signal(debate_text, ticker)``

Called by vt_research after a swarm debate completes.  The tool sends the
debate text to DeepSeek with a structured extraction prompt and returns a
``TickerSignal`` dict ready for the Aggregator pipeline.

Protocol: stdio JSON-RPC (MCP).  No external MCP SDK required.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import subprocess
from urllib.request import Request, urlopen
from urllib.error import URLError

# ── Suppress library stdout during imports ──────────────────────
# lumibot (and other libs) configure StreamHandler→stdout at import
# time.  stdout is the MCP JSON‑RPC channel — any stray log line
# breaks the protocol.  Silence the lumibot logger and redirect
# stdout→stderr for the duration of nanobot_quant imports.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
logging.getLogger("lumibot").handlers.clear()
logging.getLogger("lumibot").propagate = True

SERVER_NAME = "signal-structurizer"
SERVER_VERSION = "1.1.0"

# ── DeepSeek API helpers ──────────────────────────────────────────

def _call_deepseek(api_key: str, system_prompt: str, user_prompt: str) -> str:
    """Call DeepSeek chat API. Returns the assistant's text reply."""
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    url = f"{base_url}/v1/chat/completions"
    body = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
    }).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
    except URLError as exc:
        return json.dumps({"error": f"DeepSeek API unreachable: {exc}"})

# ── Extraction ────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """\
You are a signal extraction engine.  Given a trading debate transcript and a ticker symbol, output a single JSON object with these keys:

- recommendation: "BUY", "SELL", or "HOLD" (the PM's final call)
- confidence: string describing confidence level, e.g. "High Consensus", "Mixed", "Weak Signal"
- score: float 0–10 representing conviction (10 = strongest buy, 0 = strongest sell, 5 = neutral)
- price: float or null, current price if mentioned in debate
- reason: string, 1–2 sentence summary of the key argument(s)

Rules:
1. Only output valid JSON. No markdown fences, no explanation.
2. If the debate text is empty or doesn't contain a clear conclusion, return:
   {"recommendation": "HOLD", "confidence": "Insufficient Data", "score": 5, "price": null, "reason": ""}
3. Always include all five keys."""


def _extract_signal(debate_text: str, ticker: str) -> dict:
    """Extract TickerSignal fields from debate text via DeepSeek."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"error": "DEEPSEEK_API_KEY not set"}

    user_prompt = f"Ticker: {ticker}\n\nDebate transcript:\n{debate_text}"
    raw = _call_deepseek(api_key, _EXTRACTION_PROMPT, user_prompt)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown fences
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return {"error": "Failed to parse LLM output as JSON", "raw": raw[:300]}
    return result


def structurize_signal(debate_text: str, ticker: str) -> dict:
    """Extract and return a TickerSignal from debate text.

    The returned dict contains:
      - ticker, recommendation, confidence, score, reason (always)
      - price (float or null)
      - setup_buy, setup_sell, cd_buy, cd_sell (always 0 for VT signals)
      - tdst_support, tdst_resistance, rvol (always null)
      - source: "vt_research"
    """
    extracted = _extract_signal(debate_text, ticker)
    if "error" in extracted:
        return extracted

    result = {
        "ticker": ticker.upper(),
        "recommendation": extracted.get("recommendation", "HOLD"),
        "confidence": extracted.get("confidence", "Unknown"),
        "setup_buy": 0,
        "setup_sell": 0,
        "cd_buy": 0,
        "cd_sell": 0,
        "score": extracted.get("score"),
        "price": extracted.get("price"),
        "tdst_support": None,
        "tdst_resistance": None,
        "rvol": None,
        "reason": extracted.get("reason", ""),
        "source": "vt_research",
    }

    print(
        f"[DIAG] structurize_signal: {ticker.upper()} → {result['recommendation']} "
        f"(score={result['score']}, confidence={result['confidence']})",
        file=sys.stderr, flush=True,
    )
    return result


# ── MCP stdio JSON-RPC loop ───────────────────────────────────────

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
        return None  # notification, no response

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
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
                            "or list), returns risk checks and suggested orders."
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
                            },
                            "required": ["ticker_signal_json"],
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
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
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
                                    "default": "basic",
                                    "description": "Payment tier: basic or premium",
                                },
                            },
                        },
                    },
                    {
                        "name": "wallet_login_status",
                        "description": (
                            "Check onchainos login and payment status without side effects. "
                            "Returns: logged_in, payment_basic, payment_premium booleans."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
                    }
                ]
            },
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        if tool_name == "run_td_sequential":
            result = run_td_sequential(
                address=arguments.get("address", ""),
                chain=arguments.get("chain", "solana"),
                bar=arguments.get("bar", "1D"),
                limit=int(arguments.get("limit", 299)),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "structurize_signal":
            result = structurize_signal(
                debate_text=arguments.get("debate_text", ""),
                ticker=arguments.get("ticker", ""),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "execute_signal":
            result = execute_signal(
                ticker_signal_json=arguments.get("ticker_signal_json", ""),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "wallet_login_init":
            result = wallet_login_init()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "wallet_login_poll":
            result = wallet_login_poll(
                session_id=arguments.get("session_id", ""),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "wallet_payment_set":
            result = wallet_payment_set(
                tier=arguments.get("tier", "basic"),
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                },
            }
        if tool_name == "wallet_login_status":
            result = wallet_login_status()
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

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


# ── run_td_sequential tool ──────────────────────────────────────

def run_td_sequential(
    address: str,
    chain: str = "solana",
    bar: str = "1D",
    limit: int = 299,
) -> dict:
    """Run TD Sequential analysis on an OnchainOS token.

    Fetches K-line data via OnchainOS CLI, calculates TD Sequential
    and returns a TickerSignal dict.
    """
    # ── Guard MCP stdio from library import-time logging ─────────
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from nanobot_quant.onchainos_data import fetch_kline as _fetch_kline
        from nanobot_quant.strategies.td_sequential import calculate as _calculate
    finally:
        sys.stdout = _saved_stdout

    chain_name = chain if chain in ("solana", "arbitrum", "ethereum", "base", "bnb", "optimism", "polygon", "xdai") else "solana"

    print(
        f"[DIAG] run_td_sequential: address={address[:12]}... chain={chain_name} bar={bar} limit={limit}",
        file=sys.stderr, flush=True,
    )

    try:
        df = _fetch_kline(
            chain=chain_name,
            token_address=address,
            bar=bar,
            limit=limit,
        )
    except Exception as exc:
        return {"error": f"Failed to fetch kline data: {exc}"}

    if df is None or df.empty:
        return {"error": "No kline data returned from OnchainOS"}

    print(
        f"[DIAG] run_td_sequential: fetched {len(df)} candles ({df.index[0]} → {df.index[-1]})",
        file=sys.stderr, flush=True,
    )

    try:
        result = _calculate(df)
    except Exception as exc:
        return {"error": f"TD Sequential calculation failed: {exc}"}

    result["source"] = "quant"
    result["ticker"] = address
    result["chain"] = chain_name

    print(
        f"[DIAG] run_td_sequential: {result['recommendation']} "
        f"(setup_buy={result['setup_buy']} cd_buy={result['cd_buy']} score={result['score']})",
        file=sys.stderr, flush=True,
    )
    return result



# ── onchainos wallet login tools ──────────────────────────────────

ONCHAINOS_BIN = "/usr/local/bin/onchainos"

def wallet_login_init() -> dict:
    """Initiate onchainos social login. Returns loginUrl for the user."""
    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "wallet", "login", "init"],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}
    except subprocess.TimeoutExpired:
        return {"error": "onchainos wallet login init timed out (30s)"}

    if proc.returncode != 0:
        return {"error": f"init failed (rc={proc.returncode}): {proc.stderr.strip()}"}

    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": f"unexpected init output: {proc.stdout.strip()[:500]}"}

    print(
        f"[DIAG] wallet_login_init: session={data.get('authSessionId','?')[:12]}...",
        file=sys.stderr, flush=True,
    )
    return {"login_url": data.get("loginUrl", ""), "auth_session_id": data.get("authSessionId", ""), "opened": data.get("opened", False)}


def wallet_login_poll(session_id: str = "") -> dict:
    """Poll for social login completion. Call after user finishes browser login."""
    args = [ONCHAINOS_BIN, "wallet", "login", "poll"]
    if session_id:
        args.append(session_id)

    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=310,
        )
    except FileNotFoundError:
        return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}
    except subprocess.TimeoutExpired:
        return {"error": "onchainos wallet login poll timed out (310s) — user may not have completed browser login"}

    combined = proc.stdout.strip() + proc.stderr.strip()
    if proc.returncode != 0:
        return {"error": f"poll failed (rc={proc.returncode}): {combined[:1000]}"}

    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        data = {"raw": combined[:2000]}

    print(
        "[DIAG] wallet_login_poll: login complete",
        file=sys.stderr, flush=True,
    )
    return data


def wallet_payment_set(tier: str = "basic") -> dict:
    """Set onchainos payment default tier (requires prior wallet login)."""
    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "set", "--tier", tier],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return {"error": f"onchainos binary not found at {ONCHAINOS_BIN}"}
    except subprocess.TimeoutExpired:
        return {"error": "payment default set timed out (30s)"}

    combined = proc.stdout.strip() + proc.stderr.strip()
    if proc.returncode != 0:
        return {"error": f"payment default set failed (rc={proc.returncode}): {combined[:1000]}"}

    return {"status": "ok", "tier": tier, "output": combined[:500]}


def wallet_login_status() -> dict:
    """Check onchainos login / payment status without side effects."""
    status = {"logged_in": False, "payment_basic": False, "payment_premium": False}

    # Check if session.json exists (indicates prior login)
    session_path = os.path.expanduser("~/.onchainos/session.json")
    if os.path.isfile(session_path):
        status["logged_in"] = True

    # Check payment defaults
    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "get", "--tier", "basic"],
            capture_output=True, text=True, timeout=15,
        )
        status["payment_basic"] = proc.returncode == 0
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [ONCHAINOS_BIN, "payment", "default", "get", "--tier", "premium"],
            capture_output=True, text=True, timeout=15,
        )
        status["payment_premium"] = proc.returncode == 0
    except Exception:
        pass

    return status



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



# ── execute_signal tool ─────────────────────────────────────────

def execute_signal(ticker_signal_json: str) -> dict:
    """Execute the trading pipeline on structured signal(s).

    Takes a JSON signal (from structurize_signal) and passes it
    through Risk → Position Sizing → Order generation.

    Accepts either a single signal object or a list of signals.

    Returns pipeline execution results with risk checks and
    suggested orders.
    """
    # ── Guard MCP stdio from library import-time logging ─────────
    _saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        from nanobot_quant.pipeline import run_from_signals
    finally:
        sys.stdout = _saved_stdout

    try:
        raw = json.loads(ticker_signal_json)
    except json.JSONDecodeError:
        return {"error": "Invalid JSON input"}

    # Normalise to list of dicts
    signal_list: list[dict] = raw if isinstance(raw, list) else [raw]

    # Validate each dict has required fields
    for s in signal_list:
        if "ticker" not in s:
            return {"error": f"Missing 'ticker' in signal: {s}"}

    ticker_summary = [s.get("ticker", "?") for s in signal_list]
    print(
        f"[DIAG] execute_signal: running pipeline on {ticker_summary}",
        file=sys.stderr, flush=True,
    )

    try:
        results = run_from_signals(signal_list)
        return {"results": results, "count": len(results)}
    except Exception as exc:
        return {"error": f"Pipeline execution failed: {exc}"}


if __name__ == "__main__":
    main()


