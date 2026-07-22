"""Neo ↔ Quant Agent Signal JSON Schema.

The contract between the commander (Neo) and the Quant Agent.
All communication is structured JSON — no natural language in either direction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Request (Neo → Quant) ──────────────────────────────────────────────

@dataclass
class SignalRequest:
    """Neo sends a list of tickers to analyze.
    
    Example::
    
        {
            "request_id": "cafe1234-...",
            "tickers": ["AAPL", "GOOGL"],
            "period": "6mo"
        }
    """
    tickers: list[str]
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    period: str = "6mo"           # yfinance period: 1mo, 3mo, 6mo, 1y, 2y, max


# ── Per-Ticker Signal ───────────────────────────────────────────────────

@dataclass
class TickerSignal:
    """TD Sequential analysis result for a single ticker.

    Example::
    
        {
            "ticker": "AAPL",
            "recommendation": "BUY",
            "confidence": "Setup Complete",
            "setup_buy": 9, "setup_sell": 0,
            "cd_buy": 5, "cd_sell": 0,
            "score": 6.5,
            "price": 150.25,
            "tdst_support": 145.3,
            "tdst_resistance": 155.8,
            "rvol": 1.2
        }
    """
    ticker: str
    recommendation: str
    confidence: str             # subtype: "Setup Complete", "Oversold", "Support Break", etc.
    setup_buy: int
    setup_sell: int
    cd_buy: int
    cd_sell: int
    score: Optional[float]
    price: Optional[float]
    tdst_support: Optional[float]
    tdst_resistance: Optional[float]
    rvol: Optional[float]

    @classmethod
    def from_calculate_result(cls, ticker: str, result: dict) -> "TickerSignal":
        """Build from `td_sequential.calculate()` output dict."""
        recommendation = result.get("recommendation", "HOLD")
        # Split recommendation into primary + confidence
        if " (" in recommendation:
            primary, part2 = recommendation.split(" (", 1)
            confidence = part2.rstrip(")")
        else:
            primary = recommendation
            confidence = recommendation

        return cls(
            ticker=ticker,
            recommendation=primary,
            confidence=confidence,
            setup_buy=result.get("setup_buy", 0),
            setup_sell=result.get("setup_sell", 0),
            cd_buy=result.get("cd_buy", 0),
            cd_sell=result.get("cd_sell", 0),
            score=result.get("score"),
            price=result.get("price"),
            tdst_support=result.get("tdst_support"),
            tdst_resistance=result.get("tdst_resistance"),
            rvol=result.get("rvol"),
        )


# ── Response (Quant → Neo) ─────────────────────────────────────────────

@dataclass
class SignalResponse:
    """Quant Agent returns scored signals back to Neo.

    Example::
    
        {
            "request_id": "cafe1234-...",
            "generated_at": "2026-07-22T16:30:00",
            "signals": [...]
        }
    """
    request_id: str
    signals: list[TickerSignal]
    generated_at: str = field(default_factory=lambda: "")

    def to_summary(self) -> str:
        """Human-readable summary for Neo to review."""
        if not self.signals:
            return "📊 No signals generated."
        lines = [f"📊 Signal Report ({self.generated_at})"]
        lines.append(f"request_id: `{self.request_id}`")
        lines.append("")
        for s in self.signals:
            icon = {"BUY": "🟢", "SELL": "🔴"}.get(s.recommendation, "⚪")
            score_str = f"score={s.score:.1f}" if s.score is not None else ""
            lines.append(
                f"{icon} **{s.ticker}** → {s.recommendation} "
                f"({s.confidence}) | setup={s.setup_buy}/{s.setup_sell} "
                f"cd={s.cd_buy}/{s.cd_sell} | {score_str}"
            )
        return "\n".join(lines)
