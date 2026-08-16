"""
Position Sizing Engine

Institutional Production Version

Responsibilities
----------------
Determine the exact position size for every trade.

Consumes

FinalDecision
ValidationResult
RiskResult

Produces

Position Size
Capital Allocation
Risk Per Trade
Quantity

This engine NEVER changes BUY/SELL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import math

import pandas as pd

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationResult
from risk.risk_manager import RiskResult

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class PositionSizingResult:

    quantity: int

    capital_to_use: float

    allocation_percent: float

    risk_per_trade: float

    stop_distance: float

    position_value: float

    kelly_fraction: float

    volatility_adjustment: float

    liquidity_adjustment: float

    confidence_adjustment: float

    warnings: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# ENGINE
# ==========================================================


class PositionSizingEngine:
    """
    Master Position Sizing Engine
    """

    MAX_RISK_PER_TRADE = 0.02

    MAX_CAPITAL_ALLOCATION = 0.20

    MIN_CAPITAL_ALLOCATION = 0.02

    MIN_POSITION_VALUE = 5000.0

    MAX_POSITION_VALUE = 500000.0

    # FIX #6 (architecture review): this used to be force-applied via
    # `max(quantity, MIN_QUANTITY)` in two places below, meaning a
    # position could NEVER size to 0 shares even when the ATR-based
    # risk-budget calc (atr_quantity = risk_per_trade / stop_distance)
    # genuinely computed 0 — i.e. even 1 share's stop-loss distance
    # would risk more than MAX_RISK_PER_TRADE allows. That silently
    # violated the very risk budget this engine just calculated.
    # Confirmed real (not theoretical): traced through to
    # paper_trading_engine.py's "New Virtual Trade Opened" path, which
    # would open a real 1-share paper position in that scenario.
    #
    # Both floors are now removed — executable_quantity can legitimately
    # be 0. This constant is kept only as a documented reference value
    # (no code below reads it), NOT as an enforced floor.
    #
    # No new rejection path was needed: risk/portfolio_rules.py already
    # has `if sizing.quantity <= 0: rejection_reason = "Invalid position
    # size."` (see PortfolioRulesEngine.evaluate()) — a 0-quantity result
    # from here was already a supported, correctly-handled input; this
    # engine was just never allowed to produce one.
    MIN_QUANTITY = 1

    # KELLY CALIBRATION — see the "KELLY FRACTION" NOTE below. Flip to
    # True only once a real (setup_score -> historical win-rate)
    # calibration table exists from enough closed trades (analytics/
    # learning_engine.py or equivalent) — not before.
    KELLY_CALIBRATED = False

    # Fixed, non-discriminating fallback while KELLY_CALIBRATED is False
    # — every qualifying trade gets this SAME base Kelly fraction (before
    # the volatility/liquidity/confidence/risk adjustments below scale it
    # down per-trade). 0.5 = the midpoint of the allowed [0,1] Kelly
    # range, chosen so this doesn't produce a jarring size discontinuity
    # vs. what the old uncalibrated formula typically produced for a
    # barely-qualifying trade (~0.35-0.40) — adjust if a different
    # baseline is preferred; there's no uniquely "correct" value here
    # without calibration data, only a defensible flat default.
    FALLBACK_KELLY_FRACTION = 0.5

    def calculate(
        self,
        decision: FinalDecision,
        validation: ValidationResult,
        risk: RiskResult,
        dataframe: pd.DataFrame,
        portfolio: dict[str, Any],
    ) -> PositionSizingResult:

        latest = dataframe.iloc[-1]

        warnings = []

        diagnostics = {}

        logger.info("Starting Position Sizing Engine.")

        # --------------------------------------------------
        # PRECHECK
        # --------------------------------------------------

        if not validation.passed or not risk.safe:

            return PositionSizingResult(
                quantity=0,
                capital_to_use=0.0,
                allocation_percent=0.0,
                risk_per_trade=0.0,
                stop_distance=0.0,
                position_value=0.0,
                kelly_fraction=0.0,
                volatility_adjustment=0.0,
                liquidity_adjustment=0.0,
                confidence_adjustment=0.0,
                warnings=["Trade rejected before sizing."],
            )
        # ==========================================================
        # CAPITAL
        # ==========================================================

        available_cash = float(
            portfolio.get(
                "available_cash",
                portfolio.get("available_capital", 0.0),
            )
        )

        total_capital = float(
            portfolio.get(
                "total_capital",
                available_cash,
            )
        )

        close = float(
            latest.get(
                "close",
                0.0,
            )
        )

        atr = float(
            latest.get(
                "atr_14",
                0.0,
            )
        )

        diagnostics["available_cash"] = round(
            available_cash,
            2,
        )

        diagnostics["total_capital"] = round(
            total_capital,
            2,
        )

        diagnostics["entry_price"] = round(
            close,
            2,
        )

        # ==========================================================
        # RISK PER TRADE
        # ==========================================================

        risk_per_trade = total_capital * self.MAX_RISK_PER_TRADE

        diagnostics["risk_per_trade"] = round(
            risk_per_trade,
            2,
        )

        # ==========================================================
        # STOP DISTANCE
        # ==========================================================

        stop_distance = max(
            atr * 2.0,
            close * 0.01,
        )

        diagnostics["stop_distance"] = round(
            stop_distance,
            2,
        )

        # ==========================================================
        # KELLY FRACTION
        # ==========================================================
        # NOTE (found during an architecture review, this is a real bug
        # fix, not a style change): decision.buy_probability/
        # sell_probability come from BuyProbabilityEngine._win_probability()
        # — a sigmoid transform of a composite score
        # (100/(1+exp(-(score.overall-50)/10))), NOT a calibrated
        # historical win-rate. It has never seen a single closed trade.
        # decision.expected_return/expected_drawdown (used in reward_risk
        # below) come from that same uncalibrated BuyScore/BuyProbability
        # chain. Feeding either straight into the Kelly formula meant real
        # capital was being sized off fabricated confidence numbers.
        #
        # Until KELLY_CALIBRATED is flipped to True (once a real
        # setup_score -> historical-win-rate table exists from enough
        # closed trades), kelly_fraction is a FIXED constant
        # (FALLBACK_KELLY_FRACTION) — every qualifying trade gets the same
        # base allocation fraction, not one scaled by an uncalibrated
        # per-trade "probability". uncalibrated_win_rate/reward_risk are
        # still computed and logged below (so this can be validated
        # against real Kelly once calibration exists) but do NOT drive
        # sizing while uncalibrated.
        #
        # This does not change the trade's actual risk-per-trade
        # discipline — that already comes from the ATR-based
        # risk_per_trade/stop_distance path further down ("ATR-BASED
        # QUANTITY"), which was never Kelly-driven. This fix only stops
        # the SEPARATE capital-allocation-percentage path (capital_quantity,
        # combined with atr_quantity via min() below) from being scaled by
        # a fabricated probability.

        uncalibrated_win_rate = (
            decision.buy_probability
            if decision.action == "BUY"
            else decision.sell_probability
        ) / 100.0

        reward_risk = max(
            decision.expected_return
            / max(
                decision.expected_drawdown,
                0.10,
            ),
            1.0,
        )

        diagnostics["uncalibrated_win_rate"] = round(
            uncalibrated_win_rate,
            4,
        )

        diagnostics["reward_risk"] = round(
            reward_risk,
            2,
        )

        if self.KELLY_CALIBRATED:

            kelly_fraction = ((uncalibrated_win_rate * (reward_risk + 1)) - 1) / reward_risk

            kelly_fraction = max(
                0.0,
                min(
                    kelly_fraction,
                    1.0,
                ),
            )

        else:

            kelly_fraction = self.FALLBACK_KELLY_FRACTION

        diagnostics["kelly_fraction"] = round(
            kelly_fraction,
            4,
        )

        diagnostics["kelly_calibrated"] = self.KELLY_CALIBRATED

        # ==========================================================
        # BASE CAPITAL ALLOCATION
        # ==========================================================

        allocation_percent = (
            self.MIN_CAPITAL_ALLOCATION
            + (self.MAX_CAPITAL_ALLOCATION - self.MIN_CAPITAL_ALLOCATION)
            * kelly_fraction
        )

        diagnostics["base_allocation"] = round(
            allocation_percent,
            4,
        )
        # ==========================================================
        # VOLATILITY ADJUSTMENT
        # ==========================================================

        atr_percent = (
            atr
            / max(
                close,
                1.0,
            )
        ) * 100.0

        if atr_percent <= 1.0:

            volatility_adjustment = 1.00

        elif atr_percent <= 2.0:

            volatility_adjustment = 0.90

        elif atr_percent <= 3.0:

            volatility_adjustment = 0.75

        elif atr_percent <= 5.0:

            volatility_adjustment = 0.60

        else:

            volatility_adjustment = 0.40

        diagnostics["atr_percent"] = round(
            atr_percent,
            2,
        )

        diagnostics["volatility_adjustment"] = round(
            volatility_adjustment,
            2,
        )

        # ==========================================================
        # LIQUIDITY ADJUSTMENT
        # ==========================================================

        average_volume = float(
            latest.get(
                "volume_sma_20",
                0.0,
            )
        )

        if average_volume >= 5_000_000:

            liquidity_adjustment = 1.00

        elif average_volume >= 2_000_000:

            liquidity_adjustment = 0.90

        elif average_volume >= 1_000_000:

            liquidity_adjustment = 0.80

        elif average_volume >= 500_000:

            liquidity_adjustment = 0.65

        else:

            liquidity_adjustment = 0.40

        diagnostics["average_volume"] = int(average_volume)

        diagnostics["liquidity_adjustment"] = round(
            liquidity_adjustment,
            2,
        )

        # ==========================================================
        # CONFIDENCE ADJUSTMENT
        # ==========================================================

        confidence = decision.confidence

        confidence_adjustment = min(
            max(
                confidence / 100.0,
                0.50,
            ),
            1.00,
        )

        diagnostics["confidence_adjustment"] = round(
            confidence_adjustment,
            2,
        )

        # ==========================================================
        # RISK ADJUSTMENT
        # ==========================================================

        risk_adjustment = max(
            0.20,
            1.0 - (risk.total_risk / 100.0),
        )

        diagnostics["risk_adjustment"] = round(
            risk_adjustment,
            2,
        )

        # ==========================================================
        # COMBINED ADJUSTMENT
        # ==========================================================

        adjustment_factor = (
            volatility_adjustment
            * liquidity_adjustment
            * confidence_adjustment
            * risk_adjustment
        )

        diagnostics["adjustment_factor"] = round(
            adjustment_factor,
            4,
        )

        if adjustment_factor < 0.50:

            warnings.append("Position size reduced due to combined risk factors.")
        # ==========================================================
        # FINAL CAPITAL ALLOCATION
        # ==========================================================

        allocation_percent *= adjustment_factor

        allocation_percent = max(
            self.MIN_CAPITAL_ALLOCATION,
            min(
                allocation_percent,
                self.MAX_CAPITAL_ALLOCATION,
            ),
        )

        diagnostics["final_allocation_percent"] = round(
            allocation_percent,
            4,
        )

        # ==========================================================
        # CAPITAL TO USE
        # ==========================================================

        capital_to_use = available_cash * allocation_percent

        capital_to_use = min(
            capital_to_use,
            available_cash,
        )

        diagnostics["capital_to_use"] = round(
            capital_to_use,
            2,
        )

        # ==========================================================
        # POSITION VALUE
        # ==========================================================

        position_value = max(
            self.MIN_POSITION_VALUE,
            capital_to_use,
        )

        position_value = min(
            position_value,
            self.MAX_POSITION_VALUE,
            available_cash,
        )

        diagnostics["position_value"] = round(
            position_value,
            2,
        )

        # ==========================================================
        # QUANTITY
        # ==========================================================

        if close > 0:

            quantity = math.floor(position_value / close)

        else:

            quantity = 0

        # FIX #6: was `quantity = max(quantity, self.MIN_QUANTITY)` —
        # see the MIN_QUANTITY class-attribute NOTE above. This
        # capital-allocation-based quantity can legitimately be 0 (the
        # capital budget can't afford even 1 share at this allocation);
        # forcing it to 1 here would let a capital-constrained account
        # buy a position bigger than its own allocation limit intended.

        diagnostics["quantity"] = quantity

        # ==========================================================
        # POSITION LIMIT
        # ==========================================================

        max_quantity = int(
            portfolio.get(
                "max_quantity",
                quantity,
            )
        )

        if quantity > max_quantity:

            quantity = max_quantity

            warnings.append("Quantity reduced to portfolio limit.")

        diagnostics["max_quantity"] = max_quantity

        # ==========================================================
        # CAPITAL CONSISTENCY
        # ==========================================================

        actual_position_value = quantity * close

        diagnostics["actual_position_value"] = round(
            actual_position_value,
            2,
        )

        if actual_position_value > available_cash:

            warnings.append("Position exceeds available cash.")
        # ==========================================================
        # ATR-BASED QUANTITY
        # ==========================================================

        if stop_distance > 0:

            atr_quantity = math.floor(risk_per_trade / stop_distance)

        else:

            atr_quantity = 0

        diagnostics["atr_quantity"] = atr_quantity

        # ==========================================================
        # CAPITAL-BASED QUANTITY
        # ==========================================================

        capital_quantity = quantity

        diagnostics["capital_quantity"] = capital_quantity

        # ==========================================================
        # FINAL EXECUTABLE QUANTITY
        # ==========================================================

        executable_quantity = min(
            atr_quantity,
            capital_quantity,
        )

        # FIX #6: was `executable_quantity = max(executable_quantity,
        # self.MIN_QUANTITY)` — this was the critical instance (see the
        # MIN_QUANTITY class-attribute NOTE above). If atr_quantity
        # computed to 0 (the trade's own stop distance would risk more
        # than MAX_RISK_PER_TRADE at even 1 share), this floor
        # overrode that and forced a 1-share position anyway — taking a
        # trade that violates the risk budget the engine itself just
        # computed. executable_quantity now legitimately stays 0 in
        # that case; risk/portfolio_rules.py's PortfolioRulesEngine
        # already rejects `sizing.quantity <= 0` with reason "Invalid
        # position size.", so this flows into an existing, already-
        # correct rejection path with no new logic required elsewhere.

        diagnostics["executable_quantity"] = executable_quantity

        # ==========================================================
        # POSITION VALUE UPDATE
        # ==========================================================

        position_value = executable_quantity * close

        diagnostics["final_position_value"] = round(
            position_value,
            2,
        )

        # ==========================================================
        # PORTFOLIO EXPOSURE CHECK
        # ==========================================================

        total_capital = float(
            portfolio.get(
                "total_capital",
                available_cash,
            )
        )

        current_exposure = float(
            portfolio.get(
                "open_exposure",
                0.0,
            )
        )

        new_exposure = current_exposure + (
            position_value
            / max(
                total_capital,
                1.0,
            )
        )

        diagnostics["new_exposure"] = round(
            new_exposure,
            4,
        )

        if new_exposure > 1.00:

            warnings.append("Trade would exceed 100% portfolio exposure.")

        # ==========================================================
        # POSITION UTILIZATION
        # ==========================================================

        utilization = position_value / max(
            available_cash,
            1.0,
        )

        diagnostics["capital_utilization"] = round(
            utilization,
            4,
        )

        if utilization > 0.50:

            warnings.append("Large percentage of available cash allocated.")

        # ==========================================================
        # EXECUTION QUALITY
        # ==========================================================

        execution_quality = (
            confidence_adjustment * liquidity_adjustment * volatility_adjustment
        ) * 100

        diagnostics["execution_quality"] = round(
            execution_quality,
            2,
        )

        if execution_quality < 60:

            warnings.append("Execution quality is below preferred threshold.")
        # ==========================================================
        # POSITION CLASSIFICATION
        # ==========================================================

        if allocation_percent >= 0.18:

            position_class = "FULL"

        elif allocation_percent >= 0.12:

            position_class = "LARGE"

        elif allocation_percent >= 0.08:

            position_class = "MEDIUM"

        elif allocation_percent >= 0.04:

            position_class = "SMALL"

        else:

            position_class = "MICRO"

        diagnostics["position_class"] = position_class

        # ==========================================================
        # AI RECOMMENDATIONS
        # ==========================================================

        recommendations: list[str] = []

        if risk.total_risk <= 15:

            recommendations.append("Normal position sizing.")

        elif risk.total_risk <= 30:

            recommendations.append("Reduce position size.")

        elif risk.total_risk <= 45:

            recommendations.append("Trade only if setup is exceptional.")

        else:

            recommendations.append("Avoid initiating this trade.")

        if confidence_adjustment >= 0.90:

            recommendations.append("High confidence setup.")

        elif confidence_adjustment <= 0.60:

            recommendations.append("Low confidence setup.")

        # ==========================================================
        # POSITION SCORE
        # ==========================================================

        position_score = (
            execution_quality * 0.40
            + decision.confidence * 0.30
            + (100 - risk.total_risk) * 0.30
        )

        position_score = round(
            min(
                position_score,
                100.0,
            ),
            2,
        )

        diagnostics["position_score"] = position_score

        # ==========================================================
        # SIZING QUALITY
        # ==========================================================

        if position_score >= 90:

            sizing_grade = "A+"

        elif position_score >= 80:

            sizing_grade = "A"

        elif position_score >= 70:

            sizing_grade = "B"

        elif position_score >= 60:

            sizing_grade = "C"

        else:

            sizing_grade = "D"

        diagnostics["sizing_grade"] = sizing_grade

        # ==========================================================
        # RECOMMENDED MAX EXPOSURE
        # ==========================================================

        recommended_exposure = min(
            allocation_percent,
            1.0 - current_exposure,
        )

        recommended_exposure = max(
            recommended_exposure,
            0.0,
        )

        diagnostics["recommended_max_exposure"] = round(
            recommended_exposure,
            4,
        )

        recommendations.append(
            f"Maximum recommended exposure: {recommended_exposure:.1%}"
        )

        # ==========================================================
        # STORE AI OUTPUT
        # ==========================================================

        diagnostics["recommendations"] = recommendations

        logger.info("Position sizing recommendations generated.")
        # ==========================================================
        # FINAL CONSISTENCY VALIDATION
        # ==========================================================

        validation_errors: list[str] = []

        if executable_quantity < 0:

            validation_errors.append("Negative quantity.")

        if position_value < 0:

            validation_errors.append("Negative position value.")

        if allocation_percent < 0:

            validation_errors.append("Negative allocation.")

        if allocation_percent > 1:

            validation_errors.append("Allocation exceeds 100%.")

        if risk_per_trade < 0:

            validation_errors.append("Negative risk per trade.")

        if stop_distance <= 0:

            validation_errors.append("Invalid stop distance.")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE
        # ==========================================================

        if validation_errors:

            logger.error(
                "Position sizing validation failed: %s",
                validation_errors,
            )

            executable_quantity = 0

            capital_to_use = 0.0

            allocation_percent = 0.0

            position_value = 0.0

            warnings.append("Position sizing failed internal validation.")

            diagnostics["fail_safe"] = True

        else:

            diagnostics["fail_safe"] = False

        # ==========================================================
        # BUILD RESULT
        # ==========================================================

        result = PositionSizingResult(
            quantity=executable_quantity,
            capital_to_use=round(
                capital_to_use,
                2,
            ),
            allocation_percent=round(
                allocation_percent,
                4,
            ),
            risk_per_trade=round(
                risk_per_trade,
                2,
            ),
            stop_distance=round(
                stop_distance,
                2,
            ),
            position_value=round(
                position_value,
                2,
            ),
            kelly_fraction=round(
                kelly_fraction,
                4,
            ),
            volatility_adjustment=round(
                volatility_adjustment,
                2,
            ),
            liquidity_adjustment=round(
                liquidity_adjustment,
                2,
            ),
            confidence_adjustment=round(
                confidence_adjustment,
                2,
            ),
            warnings=warnings,
            diagnostics=diagnostics,
        )

        logger.info("PositionSizingResult created successfully.")

        return result

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(
        result: PositionSizingResult,
    ) -> dict[str, Any]:

        return {
            "quantity": result.quantity,
            "capital_to_use": result.capital_to_use,
            "allocation_percent": result.allocation_percent,
            "risk_per_trade": result.risk_per_trade,
            "stop_distance": result.stop_distance,
            "position_value": result.position_value,
            "kelly_fraction": result.kelly_fraction,
            "volatility_adjustment": result.volatility_adjustment,
            "liquidity_adjustment": result.liquidity_adjustment,
            "confidence_adjustment": result.confidence_adjustment,
            "warnings": result.warnings,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        result: PositionSizingResult,
    ) -> str:

        return (
            f"Qty={result.quantity}"
            f" | Value={result.position_value:.2f}"
            f" | Allocation={result.allocation_percent:.2%}"
            f" | Risk={result.risk_per_trade:.2f}"
            f" | Kelly={result.kelly_fraction:.2f}"
        )

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        result: PositionSizingResult,
    ) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("POSITION SIZING REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Quantity             : {result.quantity}")

        report.append(f"Capital Used         : {result.capital_to_use:.2f}")

        report.append(f"Position Value       : {result.position_value:.2f}")

        report.append(f"Allocation           : {result.allocation_percent:.2%}")

        report.append(f"Risk Per Trade       : {result.risk_per_trade:.2f}")

        report.append(f"Stop Distance        : {result.stop_distance:.2f}")

        report.append(f"Kelly Fraction       : {result.kelly_fraction:.4f}")

        report.append(
            f"Kelly Calibrated     : {result.diagnostics.get('kelly_calibrated')}"
            f" (uncalibrated_win_rate={result.diagnostics.get('uncalibrated_win_rate')},"
            f" logged for reference only while uncalibrated)"
        )

        report.append(f"Volatility Adj.      : {result.volatility_adjustment:.2f}")

        report.append(f"Liquidity Adj.       : {result.liquidity_adjustment:.2f}")

        report.append(f"Confidence Adj.      : {result.confidence_adjustment:.2f}")

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
