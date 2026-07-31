"""structurize_signal: VT Swarm debate → TickerSignal JSON.

Called by vt_research after a swarm debate completes.  The tool sends the
debate text to DeepSeek with a structured extraction prompt and returns a
TickerSignal dict ready for the Aggregator pipeline.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import URLError

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
