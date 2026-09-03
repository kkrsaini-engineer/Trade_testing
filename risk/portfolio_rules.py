"""
Portfolio Rules Engine

Institutional Production Version

Responsibilities
----------------
Portfolio-level risk governance.

This engine NEVER creates BUY or SELL signals.

It only decides

ALLOW

or

REJECT

Rules

• Maximum Positions
• Maximum Capital Exposure
• Sector Exposure
• Symbol Exposure
• Correlation
• Daily Risk
• Portfolio Drawdown
• Cash Reserve
• Concentration Risk
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationResult
from risk.risk_manager import RiskResult
from risk.position_sizing import PositionSizingResult
from risk import portfolio_limits

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class PortfolioRuleResult:

    allowed: bool

    allocation_allowed: float

    portfolio_score: float

    rejection_reason: str | None

    warnings: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# ENGINE
# ==========================================================


class PortfolioRulesEngine:
    """
    Portfolio Governance Engine
    """

    MAX_OPEN_POSITIONS = 100

    MAX_PORTFOLIO_EXPOSURE = 0.95

    MAX_SECTOR_EXPOSURE = 0.30

    MAX_SYMBOL_EXPOSURE = 0.10

    MAX_CORRELATION = 0.80

    MIN_CASH_RESERVE = 0.10

    # NOTE (Phase 21 clarification): MAX_PORTFOLIO_EXPOSURE (above) and
    # MIN_CASH_RESERVE are NOT two independent limits — under this engine's
    # accounting (both measured against the same `total_capital`, no
    # leverage anywhere in this codebase), "exposure <= 0.95" and
    # "cash_ratio >= 0.10" are almost the same statement: exposure +
    # cash_ratio == 1.0 when the only two capital buckets are "deployed"
    # and "available". They are kept as two separate checks (see PORTFOLIO
    # EXPOSURE / CASH RESERVE sections in evaluate()) because they read
    # from different portfolio-dict fields (`open_exposure`/`exposure` vs
    # `available_cash`/`available_capital`) and a future caller could feed
    # them from genuinely independent sources (e.g. margin/leverage, where
    # exposure could exceed 100% of capital while cash reserve is a
    # separate constraint) — but under TODAY's inputs, expect these two
    # checks to (almost) always agree. If one is ever changed without the
    # other, re-verify they still express the intended combined ceiling.

    # Phase 22 (see PHASE22_NOTES.md): the old flat MAX_DRAWDOWN=0.20
    # binary gate is replaced by risk/portfolio_limits.py's graduated
    # drawdown bands (0-5% normal / 5-10% reduced / 10-15% heavily reduced
    # / >15% halt — a LOWER halt threshold than the old 20%, by design).
    # No class constant here anymore — see the PORTFOLIO DRAWDOWN section
    # in evaluate() and portfolio_limits.drawdown_band_label()/
    # drawdown_multiplier().

    def evaluate(
        self,
        decision: FinalDecision,
        validation: ValidationResult,
        risk: RiskResult,
        sizing: PositionSizingResult,
        portfolio: dict[str, Any],
    ) -> PortfolioRuleResult:

        warnings = []

        diagnostics = {}

        rejection_reason = None

        logger.info("Starting Portfolio Rules Engine.")

        # --------------------------------------------------
        # PRE-CHECK
        # --------------------------------------------------

        if not validation.passed:

            return PortfolioRuleResult(
                allowed=False,
                allocation_allowed=0.0,
                portfolio_score=0.0,
                rejection_reason=validation.rejection_reason or "Validation failed.",
                warnings=["Trade rejected before portfolio evaluation."],
            )

        if not risk.safe:

            return PortfolioRuleResult(
                allowed=False,
                allocation_allowed=0.0,
                portfolio_score=0.0,
                rejection_reason="Risk manager rejected trade.",
                warnings=["Portfolio evaluation skipped."],
            )

        if sizing.quantity <= 0:

            return PortfolioRuleResult(
                allowed=False,
                allocation_allowed=0.0,
                portfolio_score=0.0,
                rejection_reason="Invalid position size.",
                warnings=["Quantity is zero."],
            )
        # ==========================================================
        # OPEN POSITIONS
        # ==========================================================

        # Phase 26 (see PHASE26_NOTES.md, point 11): "open_positions_count"
        # is not a key any real producer sets — the actual portfolio dict
        # (PortfolioEngine.snapshot()/VirtualPortfolio.snapshot()) carries
        # "open_positions" as a dict of positions. Same class of bug as
        # risk_manager.py's matching fix — see that file's comment.
        open_positions = len(
            portfolio.get(
                "open_positions",
                {},
            ) or {}
        )

        diagnostics["open_positions"] = open_positions

        if open_positions >= self.MAX_OPEN_POSITIONS:

            rejection_reason = "Maximum portfolio positions reached."

        # ==========================================================
        # PORTFOLIO EXPOSURE
        # ==========================================================

        current_exposure = float(
            portfolio.get(
                "open_exposure",
                portfolio.get("exposure", 0.0),
            )
        )

        total_capital = float(
            portfolio.get(
                "total_capital",
                1.0,
            )
        )

        new_position_exposure = sizing.position_value / max(
            total_capital,
            1.0,
        )

        projected_exposure = current_exposure + new_position_exposure

        diagnostics["current_exposure"] = round(
            current_exposure,
            4,
        )

        diagnostics["projected_exposure"] = round(
            projected_exposure,
            4,
        )

        if (
            rejection_reason is None
            and projected_exposure > self.MAX_PORTFOLIO_EXPOSURE
        ):

            rejection_reason = "Portfolio exposure limit exceeded."

        # ==========================================================
        # CASH RESERVE
        # ==========================================================

        available_cash = float(
            portfolio.get(
                "available_cash",
                portfolio.get("available_capital", 0.0),
            )
        )

        cash_after_trade = available_cash - sizing.position_value

        cash_ratio = cash_after_trade / max(
            total_capital,
            1.0,
        )

        diagnostics["cash_after_trade"] = round(
            cash_after_trade,
            2,
        )

        diagnostics["cash_ratio"] = round(
            cash_ratio,
            4,
        )

        if rejection_reason is None and cash_ratio < self.MIN_CASH_RESERVE:

            rejection_reason = "Minimum cash reserve violated."

        # ==========================================================
        # POSITION CONCENTRATION
        # ==========================================================

        position_weight = sizing.position_value / max(
            total_capital,
            1.0,
        )

        diagnostics["position_weight"] = round(
            position_weight,
            4,
        )

        if rejection_reason is None and position_weight > self.MAX_SYMBOL_EXPOSURE:

            rejection_reason = "Single-position exposure exceeds limit."

        if position_weight > 0.08:

            warnings.append("Large position allocation.")
        # ==========================================================
        # SECTOR EXPOSURE
        # ==========================================================
        # Phase 26 (see PHASE26_NOTES.md, point 11): kept as its OWN hard
        # gate, deliberately NOT deduplicated away in favor of
        # validation_engine.py's sector_exposure check — that one only
        # has today's CURRENT sector exposure to check against (it runs
        # before position sizing, so it can't know the trade being
        # sized). This one computes `projected_sector_exposure =
        # sector_exposure + new_position_exposure` — whether THIS
        # SPECIFIC candidate's own size would push the sector over the
        # limit, which validation_engine.py's cruder current-state-only
        # check cannot express. Confirmed via direct read before
        # deciding: not a duplicate of validation_engine.py's check,
        # it's a distinct, more precise one — so it stays.

        sector_exposure = float(
            portfolio.get(
                "sector_exposure",
                0.0,
            )
        )

        projected_sector_exposure = sector_exposure + new_position_exposure

        diagnostics["sector_exposure"] = round(
            sector_exposure,
            4,
        )

        diagnostics["projected_sector_exposure"] = round(
            projected_sector_exposure,
            4,
        )

        if (
            rejection_reason is None
            and projected_sector_exposure > self.MAX_SECTOR_EXPOSURE
        ):

            rejection_reason = "Sector exposure limit exceeded."

        # ==========================================================
        # CORRELATION
        # ==========================================================

        portfolio_correlation = float(
            portfolio.get(
                "correlation",
                0.0,
            )
        )

        diagnostics["portfolio_correlation"] = round(
            portfolio_correlation,
            4,
        )

        # Phase 26 (see PHASE26_NOTES.md, point 11): this used to ALSO
        # hard-reject here (`rejection_reason = "Portfolio correlation
        # exceeds allowed limit."`) — an exact duplicate of
        # decision/validation_engine.py's `checks["correlation"]` gate
        # (same input, same MAX_CORRELATION-equivalent threshold, same
        # CURRENT-state semantics — unlike sector_exposure below, this
        # one genuinely IS a pure duplicate, not a more-precise variant).
        # validation_engine.py is now the SOLE hard gate for correlation
        # (and this method already returns early above if
        # `not validation.passed`, so a correlation-driven rejection
        # still stops the trade — just from one place, not two).
        # portfolio_correlation is still computed/reported here (now
        # from REAL data — see PHASE26_NOTES.md) for audit visibility,
        # and the >0.70 soft warning below is a distinct, non-duplicate
        # informational signal (a WARNING, not a rejection).
        if portfolio_correlation > 0.70:

            warnings.append("Portfolio correlation is elevated.")

        # ==========================================================
        # PORTFOLIO DRAWDOWN
        # ==========================================================

        portfolio_drawdown = float(
            portfolio.get(
                "max_drawdown",
                0.0,
            )
        )

        diagnostics["portfolio_drawdown"] = round(
            portfolio_drawdown,
            4,
        )

        drawdown_band = portfolio_limits.drawdown_band_label(portfolio_drawdown)

        diagnostics["drawdown_band"] = drawdown_band

        if rejection_reason is None and drawdown_band == "halt":

            rejection_reason = "Portfolio drawdown exceeds limit."

        elif drawdown_band in (
            "reduced",
            "heavily_reduced",
        ):

            warnings.append(f"Portfolio drawdown is elevated ({drawdown_band}); allocation reduced.")

        # ==========================================================
        # DAILY RISK
        # ==========================================================

        daily_loss = float(
            portfolio.get(
                "daily_loss",
                0.0,
            )
        )

        diagnostics["daily_loss"] = round(
            daily_loss,
            4,
        )

        daily_loss_stage = portfolio_limits.daily_loss_stage(daily_loss)

        diagnostics["daily_loss_stage"] = daily_loss_stage

        if rejection_reason is None and daily_loss_stage in (
            "trading_halt",
            "emergency",
        ):

            rejection_reason = "Daily loss limit reached."

        elif daily_loss_stage == "risk_reduction":

            warnings.append("Daily loss elevated; allocation reduced.")

        elif daily_loss_stage == "warning":

            warnings.append("Approaching daily loss limit.")

        # ==========================================================
        # WEEKLY RISK
        # ==========================================================

        weekly_loss = float(
            portfolio.get(
                "weekly_loss",
                0.0,
            )
        )

        diagnostics["weekly_loss"] = round(
            weekly_loss,
            4,
        )

        if weekly_loss >= 0.05:

            warnings.append("Weekly loss is elevated.")

        # ==========================================================
        # MONTHLY RISK
        # ==========================================================

        monthly_loss = float(
            portfolio.get(
                "monthly_loss",
                0.0,
            )
        )

        diagnostics["monthly_loss"] = round(
            monthly_loss,
            4,
        )

        if monthly_loss >= 0.10:

            warnings.append("Monthly loss is elevated.")
        # ==========================================================
        # DUPLICATE SYMBOL
        # ==========================================================

        symbol = portfolio.get(
            "candidate_symbol",
            None,
        )

        holdings = portfolio.get(
            "holdings",
            {},
        )

        duplicate_symbol = symbol in holdings

        diagnostics["candidate_symbol"] = symbol

        diagnostics["duplicate_symbol"] = duplicate_symbol

        if rejection_reason is None and duplicate_symbol:

            rejection_reason = "Symbol already exists in portfolio."

        # ==========================================================
        # DIVERSIFICATION
        # ==========================================================

        sector_count = int(
            portfolio.get(
                "sector_count",
                1,
            )
        )

        diagnostics["sector_count"] = sector_count

        if sector_count < 4:

            warnings.append("Portfolio diversification is low.")

        # ==========================================================
        # LIQUIDITY LIMIT
        # ==========================================================

        avg_daily_value = float(
            portfolio.get(
                "average_daily_value",
                0.0,
            )
        )

        participation_rate = sizing.position_value / max(
            avg_daily_value,
            1.0,
        )

        diagnostics["participation_rate"] = round(
            participation_rate,
            4,
        )

        if rejection_reason is None and participation_rate > 0.05:

            rejection_reason = "Trade exceeds liquidity participation limit."

        elif participation_rate > 0.03:

            warnings.append("High participation rate.")

        # ==========================================================
        # CONCENTRATION RISK
        # ==========================================================

        top5_weight = float(
            portfolio.get(
                "top5_weight",
                0.0,
            )
        )

        projected_top5 = top5_weight + position_weight

        diagnostics["top5_weight"] = round(
            top5_weight,
            4,
        )

        diagnostics["projected_top5_weight"] = round(
            projected_top5,
            4,
        )

        if rejection_reason is None and projected_top5 > 0.60:

            rejection_reason = "Top-5 concentration exceeds limit."

        elif projected_top5 > 0.50:

            warnings.append("Top-5 concentration is high.")

        # ==========================================================
        # CAPITAL EFFICIENCY
        # ==========================================================

        capital_efficiency = sizing.position_value / max(
            available_cash,
            1.0,
        )

        diagnostics["capital_efficiency"] = round(
            capital_efficiency,
            4,
        )

        if capital_efficiency > 0.50:

            warnings.append("Trade consumes a large portion of available cash.")
        # ==========================================================
        # PORTFOLIO GOVERNANCE SCORE
        # ==========================================================

        governance_checks = {
            "positions": open_positions < self.MAX_OPEN_POSITIONS,
            "exposure": projected_exposure <= self.MAX_PORTFOLIO_EXPOSURE,
            "cash": cash_ratio >= self.MIN_CASH_RESERVE,
            "symbol": position_weight <= self.MAX_SYMBOL_EXPOSURE,
            "sector": projected_sector_exposure <= self.MAX_SECTOR_EXPOSURE,
            "correlation": portfolio_correlation <= self.MAX_CORRELATION,
            "drawdown": drawdown_band != "halt",
            "liquidity": participation_rate <= 0.05,
            "concentration": projected_top5 <= 0.60,
        }

        diagnostics["governance_checks"] = governance_checks

        # ==========================================================
        # PORTFOLIO SCORE
        # ==========================================================

        passed_checks = sum(governance_checks.values())

        total_checks = len(governance_checks)

        portfolio_score = (passed_checks / total_checks) * 100.0

        portfolio_score = round(
            portfolio_score,
            2,
        )

        diagnostics["portfolio_score"] = portfolio_score

        # ==========================================================
        # ALLOCATION ADJUSTMENT
        # ==========================================================

        allocation_allowed = sizing.allocation_percent

        if portfolio_score < 95:

            allocation_allowed *= 0.90

        if portfolio_score < 85:

            allocation_allowed *= 0.80

        if portfolio_score < 75:

            allocation_allowed *= 0.70

        if portfolio_score < 65:

            allocation_allowed *= 0.50

        # Phase 22: graduated drawdown/daily-loss throttling, on top of
        # (not instead of) the governance-score cascade above — drawdown
        # is ALSO one of the 9 governance_checks, but that check alone
        # only ever contributes a small, equally-weighted slice of
        # portfolio_score. These two multipliers make severity-specific
        # scaling explicit, per risk/portfolio_limits.py.
        allocation_allowed *= portfolio_limits.drawdown_multiplier(portfolio_drawdown)

        allocation_allowed *= portfolio_limits.daily_loss_multiplier(daily_loss)

        allocation_allowed = round(
            max(
                allocation_allowed,
                0.0,
            ),
            4,
        )

        diagnostics["allocation_allowed"] = allocation_allowed

        # ==========================================================
        # FINAL ALLOW / REJECT
        # ==========================================================

        allowed = rejection_reason is None

        if not allowed:

            allocation_allowed = 0.0

        diagnostics["allowed"] = allowed

        diagnostics["rejection_reason"] = rejection_reason

        # ==========================================================
        # PORTFOLIO STATUS
        # ==========================================================

        if allowed:

            diagnostics["portfolio_status"] = "APPROVED"

            logger.info("Portfolio rules approved.")

        else:

            diagnostics["portfolio_status"] = "REJECTED"

            logger.warning(
                "Portfolio rules rejected trade: %s",
                rejection_reason,
            )
        # ==========================================================
        # PORTFOLIO RECOMMENDATIONS
        # ==========================================================

        recommendations: list[str] = []

        if allowed:

            recommendations.append("Trade is compatible with current portfolio.")

        else:

            recommendations.append(
                "Do not execute until portfolio constraints are resolved."
            )

        # ==========================================================
        # CASH MANAGEMENT
        # ==========================================================

        if cash_ratio < 0.15:

            recommendations.append(
                "Increase available cash before adding new positions."
            )

        elif cash_ratio > 0.40:

            recommendations.append("Cash allocation allows additional opportunities.")

        # ==========================================================
        # DIVERSIFICATION
        # ==========================================================

        if sector_count < 5:

            recommendations.append("Increase sector diversification.")

        if projected_top5 > 0.45:

            recommendations.append("Reduce concentration in the largest holdings.")

        # ==========================================================
        # CORRELATION MANAGEMENT
        # ==========================================================

        if portfolio_correlation > 0.60:

            recommendations.append("Prefer assets with lower correlation.")

        # ==========================================================
        # EXPOSURE BALANCING
        # ==========================================================

        remaining_exposure = max(
            0.0,
            self.MAX_PORTFOLIO_EXPOSURE - projected_exposure,
        )

        diagnostics["remaining_exposure"] = round(
            remaining_exposure,
            4,
        )

        recommendations.append(
            f"Remaining portfolio exposure: {remaining_exposure:.1%}"
        )

        # ==========================================================
        # PORTFOLIO QUALITY
        # ==========================================================

        if portfolio_score >= 95:

            portfolio_grade = "A+"

        elif portfolio_score >= 90:

            portfolio_grade = "A"

        elif portfolio_score >= 80:

            portfolio_grade = "B"

        elif portfolio_score >= 70:

            portfolio_grade = "C"

        else:

            portfolio_grade = "D"

        diagnostics["portfolio_grade"] = portfolio_grade

        # ==========================================================
        # PORTFOLIO ANALYTICS
        # ==========================================================

        diagnostics["analytics"] = {
            "portfolio_score": portfolio_score,
            "portfolio_grade": portfolio_grade,
            "allocation_allowed": allocation_allowed,
            "allowed": allowed,
            "warning_count": len(warnings),
        }

        # ==========================================================
        # STORE AI RECOMMENDATIONS
        # ==========================================================

        diagnostics["recommendations"] = recommendations

        logger.info("Portfolio recommendations generated.")
        # ==========================================================
        # FINAL CONSISTENCY VALIDATION
        # ==========================================================

        validation_errors: list[str] = []

        if not (0.0 <= allocation_allowed <= 1.0):

            validation_errors.append("Allocation allowed out of range.")

        if not (0.0 <= portfolio_score <= 100.0):

            validation_errors.append("Portfolio score out of range.")

        if rejection_reason is not None and allowed:

            validation_errors.append("Trade cannot be both allowed and rejected.")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE
        # ==========================================================

        if validation_errors:

            logger.error(
                "Portfolio rules validation failed: %s",
                validation_errors,
            )

            allowed = False

            allocation_allowed = 0.0

            portfolio_score = 0.0

            rejection_reason = "Internal portfolio validation failure."

            warnings.append("Portfolio rules entered fail-safe mode.")

            diagnostics["fail_safe"] = True

        else:

            diagnostics["fail_safe"] = False

        # ==========================================================
        # BUILD RESULT
        # ==========================================================

        result = PortfolioRuleResult(
            allowed=allowed,
            allocation_allowed=round(
                allocation_allowed,
                4,
            ),
            portfolio_score=round(
                portfolio_score,
                2,
            ),
            rejection_reason=rejection_reason,
            warnings=warnings,
            diagnostics=diagnostics,
        )

        logger.info("PortfolioRuleResult created successfully.")

        return result

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(
        result: PortfolioRuleResult,
    ) -> dict[str, Any]:

        return {
            "allowed": result.allowed,
            "allocation_allowed": result.allocation_allowed,
            "portfolio_score": result.portfolio_score,
            "rejection_reason": result.rejection_reason,
            "warnings": result.warnings,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        result: PortfolioRuleResult,
    ) -> str:

        status = "ALLOW" if result.allowed else "REJECT"

        return (
            f"{status}"
            f" | Score={result.portfolio_score:.2f}"
            f" | Allocation={result.allocation_allowed:.2%}"
            f" | Warnings={len(result.warnings)}"
        )

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        result: PortfolioRuleResult,
    ) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("PORTFOLIO RULES REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Allowed             : {result.allowed}")

        report.append(f"Portfolio Score     : {result.portfolio_score:.2f}")

        report.append(f"Allocation Allowed  : {result.allocation_allowed:.2%}")

        report.append(f"Rejection Reason    : {result.rejection_reason or '-'}")

        report.append("")

        report.append("Warnings")
        report.append("-" * 70)

        if result.warnings:

            for warning in result.warnings:

                report.append(f"• {warning}")

        else:

            report.append("None")

        report.append("")
        report.append("Diagnostics")
        report.append("-" * 70)

        for key, value in sorted(result.diagnostics.items()):

            report.append(f"{key:<30} : {value}")

        report.append("")
        report.append("=" * 70)

        return "\n".join(report)


# ==========================================================
# END OF FILE
# ==========================================================
