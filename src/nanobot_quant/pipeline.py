"""Analysis Pipeline — end-to-end Neo → Quant integration.

Chains: data source → TD Sequential → Aggregator → Risk checks →
suggested Order.

Supported data sources:
  - ``"yahoo"`` (default): yfinance, period-based
  - ``"onchainos"``: OnchainOS DEX K-lines (bar + limit)
  - ``"okx_cex"``: OKX CEX K-lines (bar + limit)

Designed to be called as a quant-agent tool (no live strategy needed).
"""

from __future__ import annotations

import sys

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

import pandas as pd

from nanobot_quant.aggregator import (
    AggregationResult,
    AggregationStats,
    RoutedSignal,
    SignalAggregator,
)
from nanobot_quant.event_bus import (
    AllocationDecidedEvent,
    EventBus,
    OrderSubmittedEvent,
    RiskCheckedEvent,
    SignalCreatedEvent,
    SignalRoutedEvent,
)
from nanobot_quant.portfolio.order_schema import OrderRequest
from nanobot_quant.risk import RiskEngine
from nanobot_quant.signal_schema import SignalRequest, SignalResponse, TickerSignal
from nanobot_quant.strategies.td_sequential import calculate


class _StubStrategy:
    """Minimal strategy stub so Lumibot Order constructor doesn't crash.

    When we call ``broker._submit_order()`` directly we are not running
    inside a full Lumibot strategy loop.  The Order only needs a
    non-None strategy handle; it never calls back into it during
    manual submission.
    """
    name = "pipeline-live"
    quote_asset = None


DataSource = Literal["yahoo", "onchainos", "okx_cex"]


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
        event_bus: EventBus | None = None,
    ) -> None:
        self._risk = RiskEngine(
            max_position_pct=max_position_pct,
            max_drawdown_pct=max_drawdown_pct,
            stop_loss_pct=stop_loss_pct,
        )
        self.max_position_pct = max_position_pct
        self._aggregator = SignalAggregator() if use_aggregator else None
        self._bus = event_bus

    # ── public API ──────────────────────────────────────────────────

    def run(
        self,
        tickers: list[str],
        *,
        source: DataSource = "yahoo",
        period: str = "6mo",
        bar: str = "1D",
        limit: int = 200,
        portfolio_value: float = 100000.0,
        return_aggregation: bool = False,
    ) -> list[AnalysisResult] | tuple[list[AnalysisResult], AggregationResult]:
        """Run full pipeline for a list of tickers.

        Args:
            tickers: Stock symbols, e.g. ``["AAPL", "TSLA"]``.
            source: Data source — ``"yahoo"`` (default), ``"onchainos"``,
                    or ``"okx_cex"``.
            period: yfinance period string (used only when ``source="yahoo"``).
            bar: Bar size for non-yahoo sources (``"1D"``, ``"4H"``, etc.).
            limit: Max bars for non-yahoo sources (default 200).
            portfolio_value: Hypothetical portfolio value for position-sizing.
            return_aggregation: If ``True``, return ``(results, aggregation)`` tuple.

        Returns:
            ``list[AnalysisResult]`` by default, or ``(results, aggregation)``
            when ``return_aggregation=True``.
        """
        raw_signals: list[TickerSignal] = []
        results: list[AnalysisResult] = []
        agg_result: AggregationResult | None = None

        # ── Phase 1: collect raw signals ──
        for ticker in tickers:
            try:
                df = self._fetch_data(ticker, source=source,
                                      period=period, bar=bar, limit=limit)
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
                if self._bus:
                    self._bus.publish(SignalCreatedEvent(ticker=ticker, signal=signal))
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
            agg_result = AggregationResult(
                routed=[],
                stats=AggregationStats(total_input=0, routed=0),
                conflicts=[],
            )

        # ── Phase 3: risk checks + order generation ──
        for rt in to_check:
            signal = rt.signal
            avg_price = signal.price or 0.0

            # emit signal.routed event (after aggregation)
            if self._bus:
                score = signal.score or 0.0
                self._bus.publish(SignalRoutedEvent(
                    routed=rt, ticker=rt.ticker,
                    recommendation=signal.recommendation,
                    score=score, conflict=rt.conflict,
                ))

            if not avg_price:
                results.append(self._empty(rt.ticker, "no price"))
                continue

            risk_details: dict[str, str] = {}
            risk_passed = True
            qty = 0

            if avg_price > 0:
                qty = self._calculate_quantity(portfolio_value, avg_price)
                position_value = avg_price * qty

                # emit allocation event
                if self._bus:
                    alloc_pct = (position_value / portfolio_value * 100) if portfolio_value else 0.0
                    self._bus.publish(AllocationDecidedEvent(
                        ticker=rt.ticker, quantity=qty,
                        position_value=position_value,
                        portfolio_value=portfolio_value,
                        allocation_pct=round(alloc_pct, 2),
                    ))

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

            # emit risk event
            if self._bus:
                self._bus.publish(RiskCheckedEvent(
                    ticker=rt.ticker, passed=risk_passed,
                    details=risk_details,
                ))

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

                if self._bus:
                    self._bus.publish(OrderSubmittedEvent(
                        order=req, ticker=rt.ticker,
                        action=req.action, quantity=qty,
                    ))

            results.append(AnalysisResult(
                ticker=rt.ticker,
                signal=signal,
                risk_passed=risk_passed,
                risk_details=risk_details,
                suggested_order=order,
            ))

        if return_aggregation:
            return results, agg_result
        return results

    def run_to_response(
        self, tickers: list[str],
        *,
        source: DataSource = "yahoo",
        period: str = "6mo",
        bar: str = "1D",
        limit: int = 200,
        portfolio_value: float = 100000.0,
    ) -> SignalResponse:
        """Run pipeline and return a :class:`SignalResponse` for Neo."""
        results = self.run(
            tickers,
            source=source,
            period=period,
            bar=bar,
            limit=limit,
            portfolio_value=portfolio_value,
        )
        # handle both plain results and (results, agg) tuple
        if isinstance(results, tuple):
            results = results[0]
        signals = [r.signal for r in results]
        return SignalResponse(
            request_id="",
            signals=signals,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── data fetching ────────────────────────────────────────────────

    @staticmethod
    def _fetch_data(
        ticker: str,
        *,
        source: DataSource,
        period: str,
        bar: str,
        limit: int,
    ) -> pd.DataFrame:
        """Fetch OHLCV data from the selected source."""
        if source == "yahoo":
            import yfinance as yf

            df = yf.download(ticker, period=period, auto_adjust=True,
                             progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            return df

        if source == "onchainos":
            from nanobot_quant.onchainos_data import fetch_kline

            return fetch_kline(ticker, timeframe=bar, limit=limit)

        if source == "okx_cex":
            from nanobot_quant.okx_cex_data import fetch_kline

            return fetch_kline(ticker, bar=bar, limit=limit)

        raise ValueError(f"Unknown data source: {source!r}")

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

default_pipeline = AnalysisPipeline()


def _position_exists(broker, symbol: str) -> bool:
    """Chain-level position check for same-symbol dedup (P2 B3).

    Returns True when the wallet already holds a non-zero balance of
    ``symbol``.  Gas (SOL) and quote coins (USDC/USDT) are excluded — SOL
    is always held for fees and USDC is the settlement currency, so they
    must never block a BUY.

    Uses ``_pull_positions`` (chain wallet balance) — the single source
    of truth shared with the TD autonomous strategy.
    """
    if symbol.upper() in ("SOL", "USDC", "USDT"):
        return False
    try:
        positions = broker._pull_positions(None)
    except Exception:
        # Dedup check failure must not block trading — treat as no position.
        return False
    target = symbol.upper()
    for pos in positions:
        try:
            if pos.asset.symbol.upper() == target:
                return True
        except Exception:
            continue
    return False


def run_from_signals(
    signals: list[TickerSignal] | list[dict],
    *,
    portfolio_value: float = 100000.0,
    max_position_pct: float | None = None,
    max_drawdown_pct: float | None = None,
    stop_loss_pct: float | None = None,
    live: bool = False,
    tokens_json: list[dict] | None = None,
    confirm: bool = False,
    quantity: float | None = None,
    slippage: float | None = None,
    sol_buffer_pct: float | None = None,
) -> list[dict]:
    """Run risk checks + order generation on pre-computed signals.

    Entry point for vt_research's execute_signal MCP tool.  Signals
    are already structured (from structurize_signal or quant TD),
    so we skip data fetch and TD calculation.

    When ``live=True``, orders that pass risk are forwarded to
    OnchainOSBroker for on-chain swap execution.

    ``confirm`` lifts the confirmation gate for questionable tokens.json
    entries (callers pass confirm=true ONLY after the user explicitly
    confirmed the entry).  Without it, such entries are rejected with
    risk_details.error="needs_confirmation" (fail-closed).

    ``quantity`` optionally overrides position sizing: when given, it is
    used directly as the order quantity (float allowed, e.g. 0.058) instead
    of ``int(portfolio_value * max_position_pct / price)``.  Risk checks
    (position_limit vs portfolio_value) still apply.  Default None keeps
    the existing sizing behaviour.

    Returns list of dicts::

        [{
            "ticker": str,
            "recommendation": str,
            "score": float|None,
            "risk_passed": bool,
            "risk_details": dict,
            "suggested_order": dict|None,
            "position_value": float|None,
            "tx_hash": str|None,          # only when live=True
            "broker_status": str|None,    # only when live=True
        }]
    """
    from nanobot_quant.portfolio.order_schema import OrderRequest
    from nanobot_quant.exec_params import load_exec_params

    # System-level execution policy: values passed explicitly by the caller
    # win; otherwise the WebUI-controlled exec_params.json applies; finally
    # the hardcoded defaults (which match the pre-parameterisation values,
    # so an unconfigured setup behaves exactly as before).
    _exec = load_exec_params()
    max_position_pct = (
        max_position_pct if max_position_pct is not None
        else _exec["max_position_pct"]
    )
    max_drawdown_pct = (
        max_drawdown_pct if max_drawdown_pct is not None
        else _exec["max_drawdown_pct"]
    )
    stop_loss_pct = (
        stop_loss_pct if stop_loss_pct is not None
        else _exec["stop_loss_pct"]
    )
    slippage = slippage if slippage is not None else _exec["slippage"]
    sol_buffer_pct = (
        sol_buffer_pct if sol_buffer_pct is not None
        else _exec["sol_buffer_pct"]
    )

    parsed: list[TickerSignal] = []
    for s in signals:
        if isinstance(s, dict):
            parsed.append(TickerSignal(**s))
        else:
            parsed.append(s)

    tickers = [s.ticker for s in parsed]
    print(f"[DIAG] run_from_signals: {len(parsed)} signal(s) → {tickers}", file=sys.stderr, flush=True)

    pipeline = AnalysisPipeline(
        max_position_pct=max_position_pct,
        max_drawdown_pct=max_drawdown_pct,
        stop_loss_pct=stop_loss_pct,
    )

    results: list[dict] = []
    for signal in parsed:
        ticker = signal.ticker
        avg_price = signal.price or 0.0

        # ── fail-closed safety gate: token must be resolvable on-chain ──
        # Covers EVERY execution path (quant line, vt line, direct
        # execute_signal).  If the ticker is not a native/resolvable token
        # on the target chain, the signal is rejected with an explicit
        # error — never approximated with a look-alike asset.
        from nanobot_quant.onchainos_cli import (
            bare_symbol,
            is_contract_address,
            resolve_token,
            supported_symbols,
        )
        bare = bare_symbol(ticker)
        resolved = resolve_token(bare, tokens_json=tokens_json)
        addr = resolved.get("address")
        # quant line passes ticker=contract-address directly → already resolvable
        if not addr and is_contract_address(bare):
            addr = bare
        if not addr:
            results.append({
                "ticker": ticker,
                "recommendation": signal.recommendation,
                "score": signal.score,
                "risk_passed": False,
                "risk_details": {
                    "error": "unsupported token",
                    "reason": (
                        f"{bare} is not a native/resolvable token on the "
                        "target chain (cannot resolve on-chain address)"
                    ),
                    "category": resolved.get("category"),
                    "suggestion": resolved.get("suggestion"),
                    "hint": resolved.get("hint"),
                    "supported": supported_symbols(tokens_json=tokens_json),
                },
                "suggested_order": None,
                "position_value": None,
                "tx_hash": None,
                "broker_status": None,
            })
            continue
        if resolved.get("needs_confirmation") and not confirm:
            results.append({
                "ticker": ticker,
                "recommendation": signal.recommendation,
                "score": signal.score,
                "risk_passed": False,
                "risk_details": {
                    "error": "needs_confirmation",
                    "reason": resolved.get("issue"),
                    "token": bare,
                    "address": addr,
                    "hint": "Re-run with confirm=true after the user confirms this tokens.json entry.",
                },
                "suggested_order": None,
                "position_value": None,
                "tx_hash": None,
                "broker_status": None,
            })
            continue

        if not avg_price:
            results.append({
                "ticker": ticker,
                "recommendation": signal.recommendation,
                "score": signal.score,
                "risk_passed": False,
                "risk_details": {"error": "no price in signal"},
                "suggested_order": None,
                "position_value": None,
            })
            continue

        risk_details: dict[str, str] = {}
        risk_passed = True
        qty = quantity if quantity is not None else pipeline._calculate_quantity(portfolio_value, avg_price)
        if quantity is not None:
            print(f"[DIAG] run_from_signals: explicit quantity={quantity} (overrides sizing)", file=sys.stderr, flush=True)
        position_value = avg_price * qty

        pos_check = pipeline._risk.check_position_limit(
            position_value=position_value,
            portfolio_value=portfolio_value,
        )
        risk_details["position_limit"] = "ok" if pos_check.approved else pos_check.reason
        if not pos_check.approved:
            risk_passed = False

        dd_check = pipeline._risk.check_max_drawdown(
            portfolio_value=portfolio_value, peak_portfolio=portfolio_value,
        )
        risk_details["max_drawdown"] = "ok" if dd_check.approved else dd_check.reason
        if not dd_check.approved:
            risk_passed = False

        sl_check = pipeline._risk.check_stop_loss(
            current_price=avg_price, entry_price=avg_price,
        )
        risk_details["stop_loss"] = "ok" if sl_check.approved else sl_check.reason
        if not sl_check.approved:
            risk_passed = False

        order: dict | None = None
        tx_hash: str | None = None
        broker_status: str | None = None

        if risk_passed and signal.recommendation in ("BUY", "SELL"):
            req = OrderRequest(
                asset=ticker,
                action="buy" if signal.recommendation == "BUY" else "sell",
                quantity=qty,
                order_type="market",
                price=avg_price,
                reason=f"VT swarm signal score={signal.score}",
            )
            order = req.to_dict()

            # ── Live execution via OnchainOSBroker ──
            if live and order is not None:
                print(f"[DIAG] run_from_signals: submitting {ticker} {req.action} x{req.quantity} live...",
                      file=sys.stderr, flush=True)
                try:
                    _saved_stdout = sys.stdout
                    sys.stdout = sys.stderr

                    from lumibot.entities import Asset, Order as LumibotOrder
                    from nanobot_quant.brokers.onchainos_broker import OnchainOSBroker

                    sys.stdout = _saved_stdout

                    broker = OnchainOSBroker(
                        tokens_json=tokens_json or [],
                        slippage=str(slippage),
                        sol_buffer_pct=float(sol_buffer_pct),
                    )

                    asset = Asset(
                        symbol=req.asset,
                        asset_type="crypto",
                    )
                    quote = Asset(
                        symbol="USDC",
                        asset_type="crypto",
                    )
                    lumibot_order = LumibotOrder(
                        strategy=_StubStrategy(),
                        asset=asset,
                        quote=quote,
                        quantity=req.quantity,
                        side=req.action,
                    )
                    broker._submit_order(lumibot_order)
                    tx_hash = lumibot_order.identifier or ""
                    order_error = (
                        getattr(lumibot_order, 'error', '')
                        or getattr(lumibot_order, '_error', '')
                        or ''
                    )
                    broker_status = lumibot_order.status if hasattr(lumibot_order, 'status') else "unknown"
                    if order_error:
                        broker_status = f"{broker_status}: {order_error}"
                    print(f"[DIAG] run_from_signals: {ticker} → {broker_status} tx={tx_hash} err={order_error!r}",
                          file=sys.stderr, flush=True)
                except Exception as exc:
                    print(f"[DIAG] run_from_signals: {ticker} broker FAILED: {exc}",
                          file=sys.stderr, flush=True)
                    import traceback
                    traceback.print_exc(file=sys.stderr)
                    tx_hash = ""
                    broker_status = f"error: {exc}"

        results.append({
            "ticker": ticker,
            "recommendation": signal.recommendation,
            "score": signal.score,
            "risk_passed": risk_passed,
            "risk_details": risk_details,
            "suggested_order": order,
            "position_value": position_value,
            "tx_hash": tx_hash,
            "broker_status": broker_status,
        })

    passed = sum(1 for r in results if r["risk_passed"])
    orders = sum(1 for r in results if r["suggested_order"] is not None)
    print(f"[DIAG] run_from_signals done: {passed}/{len(results)} risk passed, {orders} order(s)", file=sys.stderr, flush=True)

    return results
