"""Signal Aggregator — collect, deduplicate, sort, route.

Pure-Python router between signal generation and risk checking.
No AI decisions — deterministic rules only.

Design (from v3 proposal):
    • Group by symbol
    • Same direction → keep highest score
    • Conflicting directions → flag both, don't resolve
    • Sort by score descending → feed Risk Engine in priority order
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .signal_schema import TickerSignal


# ── Output types ────────────────────────────────────────────────────

@dataclass
class RoutedSignal:
    """A signal that has passed aggregation, ready for risk checking."""

    ticker: str
    signal: TickerSignal
    source: str = "quant"
    conflict: bool = False
    conflicting: list[TickerSignal] = field(default_factory=list)


@dataclass
class AggregationStats:
    """Summary of what the aggregator did."""

    total_input: int = 0
    deduplicated: int = 0
    conflict_groups: int = 0
    routed: int = 0

    def to_dict(self) -> dict:
        return {
            "total_input": self.total_input,
            "deduplicated": self.deduplicated,
            "conflict_groups": self.conflict_groups,
            "routed": self.routed,
        }


@dataclass
class AggregationResult:
    """Full aggregation output — sorted signals + conflict audit."""

    routed: list[RoutedSignal]
    stats: AggregationStats
    conflicts: list[list[TickerSignal]]  # raw conflict groups for traceability

    def to_dict(self) -> dict:
        return {
            "routed": [self._serialize_routed(r) for r in self.routed],
            "stats": self.stats.to_dict(),
            "conflicts": [
                [c.ticker for c in group] for group in self.conflicts
            ],
        }

    @staticmethod
    def _serialize_routed(r: RoutedSignal) -> dict:
        return {
            "ticker": r.ticker,
            "recommendation": r.signal.recommendation,
            "score": r.signal.score,
            "confidence": r.signal.confidence,
            "source": r.source,
            "conflict": r.conflict,
            "conflicting": [s.ticker for s in r.conflicting],
        }


# ── Aggregator ──────────────────────────────────────────────────────

class SignalAggregator:
    """Collect, deduplicate, sort, and route trading signals.

    Rules (deterministic, no LLM):
    1.  **Group** by ``ticker``.
    2.  **Deduplicate** same-direction signals: keep the one with the
        highest ``score``, discard the rest.
    3.  **Flag conflicts**: when both BUY and SELL exist for one ticker,
        keep both as ``conflict=True`` — do NOT resolve.
    4.  **Sort** by score descending (conflicts sorted together after
        clean signals).
    5.  **Route** clean signals first, then conflicts — Risk Engine
        makes the final call.

    Example::

        ag = SignalAggregator()
        result = ag.aggregate(signals, source="quant")
        for r in result.routed:
            tag = "⚠️ CONFLICT" if r.conflict else "✅"
            print(f"{tag} {r.ticker}: {r.signal.recommendation} score={r.signal.score}")
    """

    def aggregate(
        self,
        signals: list[TickerSignal],
        source: str = "quant",
    ) -> AggregationResult:
        """Route a batch of signals through aggregation rules.

        Args:
            signals: Raw signals from one or more sources.
            source: Origin label (``"quant"``, ``"research"``).

        Returns:
            ``AggregationResult`` with sorted, deduplicated signals and
            conflict audit trail.
        """
        total = len(signals)
        conflicts: list[list[TickerSignal]] = []
        deduped = 0

        # Step 1: group by ticker
        groups: dict[str, list[TickerSignal]] = {}
        for s in signals:
            groups.setdefault(s.ticker, []).append(s)

        routed: list[RoutedSignal] = []
        clean: list[RoutedSignal] = []

        for ticker, group in groups.items():
            buys = [s for s in group if s.recommendation == "BUY"]
            sells = [s for s in group if s.recommendation == "SELL"]
            others = [s for s in group if s.recommendation not in ("BUY", "SELL")]

            if buys and sells:
                # ── Conflict: both BUY and SELL on same ticker ──
                conflicts.append(group)
                best_buy = max(buys, key=lambda s: s.score or 0)
                best_sell = max(sells, key=lambda s: s.score or 0)
                routed.append(RoutedSignal(
                    ticker=ticker, signal=best_buy, source=source,
                    conflict=True, conflicting=sells,
                ))
                routed.append(RoutedSignal(
                    ticker=ticker, signal=best_sell, source=source,
                    conflict=True, conflicting=buys,
                ))
                # deduplicated = other signals in the group beyond the best two
                deduped += len(group) - 2
            elif buys:
                best = max(buys, key=lambda s: s.score or 0)
                clean.append(RoutedSignal(
                    ticker=ticker, signal=best, source=source,
                ))
                deduped += len(buys) - 1
            elif sells:
                best = max(sells, key=lambda s: s.score or 0)
                clean.append(RoutedSignal(
                    ticker=ticker, signal=best, source=source,
                ))
                deduped += len(sells) - 1

            # HOLD / N/A signals — route through as informational
            for s in others:
                clean.append(RoutedSignal(
                    ticker=ticker, signal=s, source=source,
                ))

        # Step 4: sort — clean first (score desc), conflicts after
        clean.sort(key=lambda r: r.signal.score or 0, reverse=True)
        routed_final = clean + routed

        return AggregationResult(
            routed=routed_final,
            stats=AggregationStats(
                total_input=total,
                deduplicated=deduped,
                conflict_groups=len(conflicts),
                routed=len(routed_final),
            ),
            conflicts=conflicts,
        )
