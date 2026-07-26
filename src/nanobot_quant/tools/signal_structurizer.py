"""MCP server: structurize VT Swarm PM report into TickerSignal JSON.

Usage (MCP stdio server):
    python3 -m nanobot_quant.tools.signal_structurizer

Typical workflow:
    run_swarm(...) → run_id
    get_run_result(run_id) → final_report (PM's conclusion)
    structurize_signal(final_report, ticker) → TickerSignal JSON

The server provides a single tool `structurize_signal` that calls DeepSeek
to parse the PM's final report into structured signal format.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("signal-structurizer")

STRUCTURIZE_PROMPT = """You are a trading signal parser. Extract a structured signal from the PM's final investment committee report below.

Ticker: {ticker}

PM Report:
{swarm_text}

Return ONLY a JSON object (no markdown, no code fences, no explanation):
{{
  "ticker": "{ticker}",
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": "'Strong Consensus' | 'Mixed' | 'Weak Signal' | 'Unanimous'",
  "setup_buy": 0,
  "setup_sell": 0,
  "cd_buy": 0,
  "cd_sell": 0,
  "score": null,
  "price": "entry price as float from PM's verdict, or null if absent",
  "tdst_support": "support/stop-loss level as float, or null",
  "tdst_resistance": "resistance/take-profit level as float, or null",
  "rvol": null
}}

RULES:
- recommendation: the PM's final direction — exactly "BUY", "SELL", or "HOLD".
- confidence: judge from the PM's language. "Unanimous" if all 4 agents agree, "Strong Consensus" if 3/4, "Mixed" if split.
- price: the PM's recommended entry. Use null only if no number appears.
- tdst_support: the nearest support / stop-loss mentioned.
- tdst_resistance: the nearest resistance / target / take-profit mentioned.
- All TD fields are ALWAYS 0 or null — not applicable to swarm signals.
- Output ONLY the JSON object.
"""


@mcp.tool()
async def structurize_signal(swarm_text: str, ticker: str) -> str:
    """Parse a PM's final report into a structured TickerSignal JSON.

    Workflow: run_swarm → get_run_result (final_report) → structurize_signal.

    Args:
        swarm_text: The PM's final_report from get_run_result.
        ticker: Trading pair symbol, e.g. "BTCUSDT", "AAPL".

    Returns:
        JSON string matching TickerSignal schema.
    """
    import httpx

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "DEEPSEEK_API_KEY not set"})

    prompt = STRUCTURIZE_PROMPT.format(ticker=ticker, swarm_text=swarm_text)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 500,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]

    # Strip markdown code fences if present
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return content.strip()


if __name__ == "__main__":
    mcp.run()
