"""Signal generation: batch ticker analysis via TD Sequential.

Provides `batch_calculate()` — the entry point called by the Quant Agent
when Neo sends a ticker list via relay.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .signal_schema import SignalRequest, SignalResponse, TickerSignal

logger = logging.getLogger(__name__)


def batch_calculate(
    tickers: list[str],
    period: str = "6mo",
    request_id: str = "",
) -> SignalResponse:
    """Run TD Sequential on a batch of tickers.

    This is the primary function the Quant Agent calls.  It wraps
    the per-ticker `calculate()` into a bulk pipeline and returns a
    structured `SignalResponse` ready for relay back to Neo.

    Args:
        tickers: Stock symbols (e.g. ``["AAPL", "GOOGL"]``).
        period: yfinance history period (default ``"6mo"``).
        request_id: Correlation ID echoed back in the response.

    Returns:
        ``SignalResponse`` with one ``TickerSignal`` per ticker.
    """
    import yfinance as yf
    from .strategies.td_sequential import calculate

    signals: list[TickerSignal] = []

    for ticker in tickers:
        try:
            df = yf.download(ticker, period=period, progress=False)
            if df is None or len(df) < 30:
                logging.warning(f"{ticker}: insufficient data ({len(df)} bars), skipping")
                continue
            result = calculate(df)
            signal = TickerSignal.from_calculate_result(ticker, result)
            signals.append(signal)
        except Exception as exc:
            logging.error(f"{ticker}: download/calculation failed: {exc}")

    return SignalResponse(
        request_id=request_id,
        signals=signals,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
