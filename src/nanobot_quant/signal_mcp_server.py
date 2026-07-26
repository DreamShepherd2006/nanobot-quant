"""Signal Structurizer MCP Server.

A minimal stdio MCP server that exposes a single tool:
  structurize_signal(ticker, swarm_text) -> TickerSignal JSON

Converts VT Swarm natural-language debate conclusions into structured
TickerSignal objects that can feed into the Aggregator pipeline.

Reads DEEPSEEK_API_KEY from environment (injected by squad_config_sync
via the env_provider_keys mechanism).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


# ── JSON-RPC framing ────────────────────────────────────────────

def _send(response: dict) -> None:
    """Write a JSON-RPC response to stdout (one line, flushed)."""
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    print(f"[signal_mcp] {msg}", file=sys.stderr, flush=True)


# ── Tool definition ─────────────────────────────────────────────

TOOL_DEF = {
    "name": "structurize_signal",
    "description": (
        "Convert VT Swarm natural-language debate conclusions into "
        "a structured TickerSignal JSON for the trading pipeline. "
        "Extracts: ticker, recommendation (BUY/SELL/HOLD), confidence, "
        "entry price, and reasoning summary from the swarm output."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "Trading symbol, e.g. BTCUSDT or AAPL",
            },
            "swarm_text": {
                "type": "string",
                "description": "Full VT Swarm debate output text to structure",
            },
        },
        "required": ["ticker", "swarm_text"],
    },
}


# ── DeepSeek API call ───────────────────────────────────────────

def _call_deepseek(prompt: str) -> dict:
    """Call DeepSeek API to structure the swarm output."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    payload = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": (
                "You are a trading signal structurizer. Extract structured "
                "trading signals from VT Swarm debate output. "
                "Return ONLY valid JSON, no markdown, no explanation."
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 500,
    }).encode()

    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"]["content"]
        # Strip possible markdown fences
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(content)
    except urllib.error.HTTPError as e:
        _log(f"DeepSeek API error: {e.code} {e.read().decode()[:200]}")
        raise


# ── Tool implementation ─────────────────────────────────────────

def structurize_signal(ticker: str, swarm_text: str) -> dict:
    """Main tool: convert swarm debate text to TickerSignal JSON.

    Returns a dict compatible with TickerSignal fields:
      ticker, recommendation, confidence, price, reason,
      setup_buy, setup_sell, cd_buy, cd_sell, score, ...
    """
    prompt = (
        f"VT Swarm Investment Committee debate concluded for {ticker}.\n\n"
        f"=== SWARM DEBATE OUTPUT ===\n{swarm_text}\n=== END ===\n\n"
        "Extract the final trading signal as JSON with these fields:\n"
        "- ticker: string (e.g. \"{ticker}\")\n"
        "- recommendation: \"BUY\" | \"SELL\" | \"HOLD\"\n"
        "- confidence: string (e.g. \"Strong Consensus\", \"Mixed\", \"Weak\")\n"
        "- price: number or null (current/reference price if mentioned)\n"
        "- entry_price: number or null (suggested entry if mentioned)\n"
        "- stop_loss: number or null (suggested stop loss if mentioned)\n"
        "- reason: string (one-sentence summary of the committee's conclusion)\n"
        "- setup_buy: 0, setup_sell: 0, cd_buy: 0, cd_sell: 0, score: null\n"
        "- tdst_support: null, tdst_resistance: null, rvol: null\n"
        "\nThe swarm output is a debate among Bull, Bear, Risk, and PM agents. "
        "The PM agent gives the FINAL decision — base recommendation on the PM's conclusion.\n"
        "Return ONLY the JSON object, nothing else."
    )
    result = _call_deepseek(prompt)

    # Ensure required TickerSignal fields
    result.setdefault("ticker", ticker)
    result.setdefault("recommendation", "HOLD")
    result.setdefault("confidence", "Unknown")
    result.setdefault("setup_buy", 0)
    result.setdefault("setup_sell", 0)
    result.setdefault("cd_buy", 0)
    result.setdefault("cd_sell", 0)
    result.setdefault("score", None)
    result.setdefault("price", None)
    result.setdefault("tdst_support", None)
    result.setdefault("tdst_resistance", None)
    result.setdefault("rvol", None)

    return result


# ── MCP stdio loop ──────────────────────────────────────────────

def main() -> None:
    _log("starting signal_mcp server")

    initialized = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _log(f"invalid JSON: {line[:100]}")
            continue

        req_id = request.get("id")
        method = request.get("method", "")

        # ── initialize ──
        if method == "initialize":
            initialized = True
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "signal-structurizer",
                        "version": "0.1.0",
                    },
                },
            })

        # ── tools/list ──
        elif method == "tools/list":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": [TOOL_DEF]},
            })

        # ── tools/call ──
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name != "structurize_signal":
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                })
                continue

            ticker = arguments.get("ticker", "")
            swarm_text = arguments.get("swarm_text", "")

            if not ticker or not swarm_text:
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Missing ticker or swarm_text"},
                })
                continue

            try:
                result = structurize_signal(ticker, swarm_text)
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                        ]
                    },
                })
            except Exception as exc:
                _log(f"tool error: {exc}")
                _send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                })

        # ── notifications (no id) ──
        elif request.get("id") is None:
            # Silently ignore notifications (e.g. notifications/initialized)
            pass

        else:
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            })

    _log("stdin closed, exiting")


if __name__ == "__main__":
    main()
