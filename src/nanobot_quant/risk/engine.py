"""Risk Engine — pure-Python hard gates.

Design principles:
- Fail-closed: any exception → reject the trade.
- No LLM involvement: these are deterministic calculations.
- Stateless: the engine only computes; state (peak, entry price) lives in the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskResult:
    """Outcome of a single risk check.

    Attributes:
        approved: ``True`` if the check passed.
        reason: Human-readable explanation (empty on pass).
        check_name: Short label for logging (e.g. ``"position_limit"``).
    """

    approved: bool
    reason: str = ""
    check_name: str = ""


class RiskEngine:
    """Hard-gate risk checks for entry and exit decisions.

    Parameters:
        max_position_pct: Maximum % of portfolio in a single position (default 20%).
        max_drawdown_pct: Skip new entries when drawdown exceeds this (default 15%).
        stop_loss_pct: Exit position when loss exceeds this % (default 10%).
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        max_drawdown_pct: float = 0.15,
        stop_loss_pct: float = 0.10,
    ):
        self.max_position_pct = max_position_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.stop_loss_pct = stop_loss_pct

    # ── individual checks ──────────────────────────────────────────

    def check_position_limit(
        self, position_value: float, portfolio_value: float
    ) -> RiskResult:
        """Reject entry if the new position would exceed ``max_position_pct`` of portfolio."""
        if portfolio_value <= 0:
            return RiskResult(False, "portfolio value is zero", "position_limit")
        if position_value > portfolio_value * self.max_position_pct:
            return RiskResult(
                False,
                f"pos=${position_value:.0f} > {self.max_position_pct*100:.0f}% "
                f"of pv=${portfolio_value:.0f}",
                "position_limit",
            )
        return RiskResult(True, check_name="position_limit")

    def check_max_drawdown(
        self, portfolio_value: float, peak_portfolio: float
    ) -> RiskResult:
        """Reject entry when drawdown from peak exceeds ``max_drawdown_pct``."""
        if peak_portfolio <= 0:
            return RiskResult(False, "no peak recorded", "max_drawdown")
        dd = (peak_portfolio - portfolio_value) / peak_portfolio
        if dd > self.max_drawdown_pct:
            return RiskResult(
                False,
                f"drawdown={dd*100:.1f}% > {self.max_drawdown_pct*100:.0f}% "
                f"peak={peak_portfolio:.0f}",
                "max_drawdown",
            )
        return RiskResult(True, check_name="max_drawdown")

    def check_stop_loss(
        self, current_price: float, entry_price: float
    ) -> RiskResult:
        """Signal exit when unrealised loss exceeds ``stop_loss_pct``.

        Returns ``approved=False`` when stop-loss is triggered (danger).
        """
        if entry_price <= 0:
            return RiskResult(False, "invalid entry price", "stop_loss")
        loss_pct = (entry_price - current_price) / entry_price
        if loss_pct >= self.stop_loss_pct:
            return RiskResult(
                False,
                f"loss={loss_pct*100:.1f}% >= {self.stop_loss_pct*100:.0f}% "
                f"(entry={entry_price:.2f} current={current_price:.2f})",
                "stop_loss",
            )
        return RiskResult(True, check_name="stop_loss")

    # ── composite gates ────────────────────────────────────────────

    def can_enter(
        self,
        position_value: float,
        portfolio_value: float,
        peak_portfolio: float,
    ) -> RiskResult:
        """Run all entry guards. Returns the *first* failure, or an approved result."""
        for check in (
            lambda: self.check_position_limit(position_value, portfolio_value),
            lambda: self.check_max_drawdown(portfolio_value, peak_portfolio),
        ):
            result = check()
            if not result.approved:
                return result
        return RiskResult(True, check_name="entry")

    def should_exit(
        self, current_price: float, entry_price: float
    ) -> RiskResult:
        """Run all exit guards. Returns approved=True when an exit is required."""
        result = self.check_stop_loss(current_price, entry_price)
        if not result.approved:
            return RiskResult(
                True, result.reason, "should_exit",
            )
        return RiskResult(False, check_name="should_exit")
