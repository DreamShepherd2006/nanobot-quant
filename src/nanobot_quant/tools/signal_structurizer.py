"""MCP server: structurize VT Swarm debate text into TickerSignal JSON.

Usage (MCP stdio server):
    python3 -m nanobot_quant.tools.signal_structurizer

The server provides a single tool `structurize_signal` that calls DeepSeek
to parse natural-language investment committee output into structured format.
"""

from __future__ import annotations

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("signal-structurizer")

STRUCTURIZE_PROMPT = """You are a trading signal parser. Extract a structured trading signal from the following investment committee debate.

Ticker: {ticker}

Debate text:
{swarm_text}

Return ONLY a JSON object (no markdown, no code fences, no explanation):
{{
  "ticker": "{ticker}",
  "recommendation": "BUY" | "SELL" | "HOLD",
  "confidence": "brief label, e.g. 'Strong Consensus', 'Mixed', 'Weak Signal'",
  "setup_buy": 0,
  "setup_sell": 0,
  "cd_buy": 0,
  "cd_sell": 0,
  "score": null,
  "price": "the PM's recommended entry price as a float, or null if not stated",
  "tdst_support": "the nearest support level mentioned as a float, or null",
  "tdst_resistance": "the nearest resistance level mentioned as a float, or null",
  "rvol": null
}}

RULES:
- recommendation MUST be exactly "BUY", "SELL", or "HOLD" (uppercase).
- price: extract the final recommended entry price from the PM/consensus. If multiple prices are discussed, use the PM's final number. If no specific entry is given, use null.
- tdst_support / tdst_resistance: map debate mentions of support/resistance levels. Use null if not discussed.
- All TD fields (setup_buy, setup_sell, cd_buy, cd_sell, score, rvol) are ALWAYS 0 or null — these do not apply to swarm signals.
- Output ONLY the JSON object, no surrounding text.
"""


@mcp.tool()
async def structurize_signal(swarm_text: str, ticker: str) -> str:
    """Parse VT Swarm investment committee debate text into a structured TickerSignal JSON.

    Args:
        swarm_text: The full debate/conclusion text from VT Swarm (4-agent IC output).
        ticker: Trading pair symbol, e.g. "BTCUSDT", "AAPL".

    Returns:
        JSON string matching TickerSignal schema with recommendation,
        confidence, price, and TD fields (zeroed for non-TD signals).
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
