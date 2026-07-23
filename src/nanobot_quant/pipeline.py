"""Analysis Pipeline — end-to-end Neo → Quant integration.

Chains: yfinance data → TD Sequential → Aggregator → Risk checks →
suggested Order.

Designed to be called as a quant-agent tool (no live strategy needed).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

from nanobot_quant.aggregator import (
    AggregationResult,
    RoutedSignal,
    SignalAggregator,
)
from nanobot_quant.portfolio.order_schema import OrderRequest
from nanobot_quant.risk import RiskEngine
from nanobot_quant.signal_schema import SignalRequest, SignalResponse, TickerSignal
from nanobot_quant.strategies.td_sequential import calculate


@dataclass
class AnalysisResult:
    """Per-ticker result with signal + risk checks + suggested order."""

    ticker: str
    signal: TickerSignal
    risk_passed: bool
    risk_details: dict[str, str] = field(default_factory=dict)
    suggested_order: dict | None = None   # serialized OrderRequest


class AnalysisPipeline:
    """End-to-end analysis: Data → TD → Aggregator → Risk → Portfolio → Result.

    Example:

        pipeline = AnalysisPipeline(stop_loss_pct=0.10)
        results = pipeline.run(["AAPL", "TSLA"], period="6mo")
        for r in results:
            print(f"{r.ticker}: {'✅' if r.risk_passed else '❌'} {r.signal.recommendation}")

        # With aggregation stats:
        pipeline = AnalysisPipeline(use_aggregator=True)
        results, agg = pipeline.run(["AAPL", "TSLA"], return_aggregation=True)
        print(f"Signals: {agg.stats.total_input} → {agg.stats.routed} routed")
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        max_drawdown_pct: float = 0.15,
        stop_loss_pct: float = 0.10,
        use_aggregator: bool = False,
    ) -> None:
        self._risk = RiskEngine(
            max_position_pct=max_position_pct,
            max_drawdown_pct=max_drawdown_pct,
            stop_loss_pct=stop_loss_pct,
        )
        self.max_position_pct = max_position_pct
        self._aggregator = SignalAggregator() if use_aggregator else None

    # ── public API ──────────────────────────────────────────────────

    def run(
        self,
        tickers: list[str],
        period: str = "6mo",
        portfolio_value: float = 100000.0,
        return_aggregation: bool = False,
    ) -> list[AnalysisResult] | tuple[list[AnalysisResult], AggregationResult]:
        """Run full pipeline for a list of tickers.

        Args:
            tickers: Stock symbols, e.g. ``["AAPL", "TSLA"]``.
            period: yfinance period string.
            portfolio_value: Hypothetical portfolio value for position-sizing.
            return_aggregation: If ``True``, return ``(results, aggregation)`` tuple.

        Returns:
            ``list[AnalysisResult]`` by default, or ``(results, aggregation)``
            when ``return_aggregation=True``.
        """
        import yfinance as yf

        raw_signals: list[TickerSignal] = []
        results: list[AnalysisResult] = []
        agg_result: AggregationResult | None = None

        # ── Phase 1: collect raw signals ──
        for ticker in tickers:
            try:
                df = yf.download(ticker, period=period, auto_adjust=True,
                                 progress=False)
                if df.empty:
                    results.append(self._empty(ticker, "no data"))
                    continue

                td = calculate(df)
                price = td.get("price", 0.0)
                if not price:
                    results.append(self._empty(ticker, "no price"))
                    continue

                signal = TickerSignal.from_calculate_result(ticker, td)
                raw_signals.append(signal)
            except Exception as exc:
                results.append(self._empty(ticker, f"error: {exc}"))

        # ── Phase 2: aggregate (deduplicate, detect conflicts, sort) ──
        if self._aggregator is not None and raw_signals:
            agg_result = self._aggregator.aggregate(raw_signals)
            to_check = agg_result.routed
        elif raw_signals:
            # No aggregator: wrap each signal as a clean RoutedSignal
            to_check = [
                RoutedSignal(ticker=s.ticker, signal=s)
                for s in raw_signals
            ]
            agg_result = AggregationResult(
                routed=to_check,
                stats=AggregationStats(
                    total_input=len(raw_signals), routed=len(raw_signals),
                ),
                conflicts=[],
            )
        else:
            to_check = []

        # ── Phase 3: risk checks + order generation ──
        for rt in to_check:
            signal = rt.signal
            avg_price = signal.price or 0.0
            if not avg_price:
                results.append(self._empty(rt.ticker, "no price"))
                continue

            risk_details: dict[str, str] = {}
            risk_passed = True

            if avg_price > 0:
                qty = self._calculate_quantity(portfolio_value, avg_price)
                position_value = avg_price * qty
                pos_check = self._risk.check_position_limit(
                    position_value=position_value,
                    portfolio_value=portfolio_value,
                )
                risk_details["position_limit"] = "ok" if pos_check.approved else pos_check.reason
                if not pos_check.approved:
                    risk_passed = False

            dd_check = self._risk.check_max_drawdown(
                portfolio_value=portfolio_value, peak_portfolio=portfolio_value,
            )
            risk_details["max_drawdown"] = "ok" if dd_check.approved else dd_check.reason
            if not dd_check.approved:
                risk_passed = False

            sl_check = self._risk.check_stop_loss(
                current_price=avg_price, entry_price=avg_price,
            )
            risk_details["stop_loss"] = "ok" if sl_check.approved else sl_check.reason
            if not sl_check.approved:
                risk_passed = False

            # ── Suggested order ──
            order: dict | None = None
            if risk_passed and signal.recommendation in ("BUY", "SELL"):
                req = OrderRequest(
                    asset=rt.ticker,
                    action="buy" if signal.recommendation == "BUY" else "sell",
                    quantity=qty,
                    order_type="market",
                    price=avg_price,
                    reason=f"TD {signal.recommendation} setup_buy={signal.setup_buy} score={signal.score}",
                )
                if rt.conflict:
                    req.reason += " ⚠️ CONFLICT"
                order = req.to_dict()

            results.append(AnalysisResult(
                ticker=rt.ticker,
                signal=signal,
                risk_passed=risk_passed,
                risk_details=risk_details,
                suggested_order=order,
            ))

        if return_aggregation and agg_result is not None:
            return results, agg_result
        return results

    def run_to_response(
        self, tickers: list[str], period: str = "6mo",
        portfolio_value: float = 100000.0,
    ) -> SignalResponse:
        """Run pipeline and return a :class:`SignalResponse` for Neo."""
        results = self.run(tickers, period, portfolio_value)
        signals = [r.signal for r in results]
        return SignalResponse(
            request_id="",
            signals=signals,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── helpers ─────────────────────────────────────────────────────

    def _calculate_quantity(
        self, portfolio_value: float, price: float,
    ) -> int:
        return max(int(portfolio_value * self.max_position_pct / price), 1)

    def _empty(self, ticker: str, reason: str) -> AnalysisResult:
        return AnalysisResult(
            ticker=ticker,
            signal=TickerSignal(
                ticker=ticker,
                recommendation="N/A",
                confidence=reason,
                setup_buy=0, setup_sell=0,
                cd_buy=0, cd_sell=0,
                score=None, price=None,
                tdst_support=None, tdst_resistance=None, rvol=None,
            ),
            risk_passed=False,
            risk_details={"error": reason},
        )
