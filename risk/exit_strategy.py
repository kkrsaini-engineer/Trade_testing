"""
Exit Strategy Engine

Institutional Production Version

Responsibilities
----------------
Manage every open position until exit.

This engine NEVER generates BUY or SELL.

It only decides

HOLD
PARTIAL_EXIT
FULL_EXIT

Exit Methods

• Initial Stop Loss
• ATR Stop
• Trailing Stop
• Break-even Stop
• Time Exit
• Volatility Exit
• Profit Target
• Risk Exit
• Emergency Exit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from decision.decision_engine import FinalDecision
from risk.risk_manager import RiskResult
from risk.position_sizing import PositionSizingResult

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# ACTIONS
# ==========================================================

HOLD = "HOLD"

PARTIAL_EXIT = "PARTIAL_EXIT"

FULL_EXIT = "FULL_EXIT"


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class ExitDecision:

    action: str

    exit_percent: float

    stop_loss: float

    trailing_stop: float

    take_profit: float

    break_even_price: float

    expected_rr: float

    confidence: float

    reasons: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# ENGINE
# ==========================================================


class ExitStrategyEngine:
    """
    Institutional Exit Engine
    """

    ATR_STOP = 2.0

    ATR_TRAILING = 3.0

    BREAK_EVEN_TRIGGER = 1.5

    PARTIAL_TARGET = 2.0

    FINAL_TARGET = 3.5

    MAX_HOLD_DAYS = 30

    def evaluate(
        self,
        decision: FinalDecision,
        risk: RiskResult,
        sizing: PositionSizingResult,
        dataframe: pd.DataFrame,
        position: dict[str, Any],
    ) -> ExitDecision:

        latest = dataframe.iloc[-1]

        reasons = []

        diagnostics = {}

        logger.info("Starting Exit Strategy.")

        entry_price = float(
            position.get(
                "entry_price",
                latest["close"],
            )
        )

        current_price = float(latest["close"])

        atr = float(
            latest.get(
                "atr_14",
                0.0,
            )
        )

        holding_days = int(
            position.get(
                "holding_days",
                0,
            )
        )

        highest_price = float(
            position.get(
                "highest_price",
                current_price,
            )
        )

        lowest_price = float(
            position.get(
                "lowest_price",
                current_price,
            )
        )
        # ==========================================================
        # INITIAL STOP LOSS
        # ==========================================================

        if decision.action == "BUY":

            stop_loss = entry_price - (atr * self.ATR_STOP)

        else:

            stop_loss = entry_price + (atr * self.ATR_STOP)

        diagnostics["initial_stop_loss"] = round(
            stop_loss,
            2,
        )

        # ==========================================================
        # TRAILING STOP
        # ==========================================================

        if decision.action == "BUY":

            trailing_stop = max(stop_loss, highest_price - (atr * self.ATR_TRAILING))

        else:

            trailing_stop = min(stop_loss, lowest_price + (atr * self.ATR_TRAILING))

        diagnostics["trailing_stop"] = round(
            trailing_stop,
            2,
        )

        # ==========================================================
        # BREAK-EVEN STOP
        # ==========================================================

        if decision.action == "BUY":

            reward_multiple = current_price - entry_price

            reward_multiple /= max(
                atr,
                0.01,
            )

            if reward_multiple >= self.BREAK_EVEN_TRIGGER:

                stop_loss = max(
                    stop_loss,
                    entry_price,
                )

                reasons.append("Break-even stop activated.")

        else:

            reward_multiple = entry_price - current_price

            reward_multiple /= max(
                atr,
                0.01,
            )

            if reward_multiple >= self.BREAK_EVEN_TRIGGER:

                stop_loss = min(
                    stop_loss,
                    entry_price,
                )

                reasons.append("Break-even stop activated.")

        diagnostics["reward_multiple"] = round(
            reward_multiple,
            2,
        )

        diagnostics["stop_after_break_even"] = round(
            stop_loss,
            2,
        )

        # ==========================================================
        # ACTIVE STOP
        # ==========================================================

        if decision.action == "BUY":

            active_stop = max(
                stop_loss,
                trailing_stop,
            )

        else:

            active_stop = min(
                stop_loss,
                trailing_stop,
            )

        diagnostics["active_stop"] = round(
            active_stop,
            2,
        )
        # ==========================================================
        # PROFIT TARGETS
        # ==========================================================

        if decision.action == "BUY":

            partial_target = entry_price + (atr * self.PARTIAL_TARGET)

            final_target = entry_price + (atr * self.FINAL_TARGET)

        else:

            partial_target = entry_price - (atr * self.PARTIAL_TARGET)

            final_target = entry_price - (atr * self.FINAL_TARGET)

        diagnostics["partial_target"] = round(
            partial_target,
            2,
        )

        diagnostics["final_target"] = round(
            final_target,
            2,
        )

        # ==========================================================
        # PARTIAL EXIT
        # ==========================================================

        partial_exit = False

        if decision.action == "BUY":

            if current_price >= partial_target:

                partial_exit = True

        else:

            if current_price <= partial_target:

                partial_exit = True

        diagnostics["partial_exit"] = partial_exit

        if partial_exit:

            reasons.append("Partial profit target reached.")

        # ==========================================================
        # FINAL EXIT
        # ==========================================================

        final_exit = False

        if decision.action == "BUY":

            if current_price >= final_target:

                final_exit = True

        else:

            if current_price <= final_target:

                final_exit = True

        diagnostics["final_exit"] = final_exit

        if final_exit:

            reasons.append("Final profit target reached.")

        # ==========================================================
        # RISK / REWARD
        # ==========================================================

        risk_distance = abs(entry_price - active_stop)

        reward_distance = abs(final_target - entry_price)

        expected_rr = reward_distance / max(
            risk_distance,
            0.01,
        )

        expected_rr = round(
            expected_rr,
            2,
        )

        diagnostics["risk_distance"] = round(
            risk_distance,
            2,
        )

        diagnostics["reward_distance"] = round(
            reward_distance,
            2,
        )

        diagnostics["expected_rr"] = expected_rr

        if expected_rr >= 3.0:

            reasons.append("Excellent risk/reward profile.")

        elif expected_rr >= 2.0:

            reasons.append("Healthy risk/reward profile.")

        else:

            reasons.append("Below preferred risk/reward profile.")
        # ==========================================================
        # TIME EXIT
        # ==========================================================

        time_exit = holding_days >= self.MAX_HOLD_DAYS

        diagnostics["time_exit"] = time_exit

        if time_exit:

            reasons.append("Maximum holding period reached.")

        # ==========================================================
        # VOLATILITY EXIT
        # ==========================================================

        volatility_state = str(
            latest.get(
                "volatility_state",
                "NORMAL",
            )
        ).upper()

        volatility_exit = volatility_state == "HIGH"

        diagnostics["volatility_exit"] = volatility_exit

        if volatility_exit:

            reasons.append("High market volatility.")

        # ==========================================================
        # NEWS EXIT
        # ==========================================================

        news_exit = bool(
            position.get(
                "negative_news",
                False,
            )
        )

        diagnostics["news_exit"] = news_exit

        if news_exit:

            reasons.append("Negative news detected.")

        # ==========================================================
        # EMERGENCY EXIT
        # ==========================================================

        emergency_exit = bool(
            position.get(
                "emergency_exit",
                False,
            )
        )

        diagnostics["emergency_exit"] = emergency_exit

        if emergency_exit:

            reasons.append("Emergency exit activated.")

        # ==========================================================
        # TREND REVERSAL EXIT
        # ==========================================================

        trend_reversal = False

        if decision.action == "BUY":

            if latest["ema_20"] < latest["ema_50"]:

                trend_reversal = True

        else:

            if latest["ema_20"] > latest["ema_50"]:

                trend_reversal = True

        diagnostics["trend_reversal"] = trend_reversal

        if trend_reversal:

            reasons.append("Trend reversal detected.")

        # ==========================================================
        # STOP HIT
        # ==========================================================

        stop_hit = False

        if decision.action == "BUY":

            stop_hit = current_price <= active_stop

        else:

            stop_hit = current_price >= active_stop

        diagnostics["stop_hit"] = stop_hit

        if stop_hit:

            reasons.append("Active stop-loss triggered.")
        # ==========================================================
        # EXIT PRIORITY ENGINE
        # ==========================================================

        action = HOLD

        exit_percent = 0.0

        confidence = 50.0

        exit_reason = "Continue holding position."

        # ==========================================================
        # EMERGENCY EXIT
        # ==========================================================

        if emergency_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 100.0

            exit_reason = "Emergency exit."

        # ==========================================================
        # STOP LOSS EXIT
        # ==========================================================

        elif stop_hit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 99.0

            exit_reason = "Stop-loss triggered."

        # ==========================================================
        # FINAL TARGET
        # ==========================================================
        # Profit-target checks were moved ABOVE trend_reversal/news/
        # volatility/time exits (they used to sit below all of them).
        # Previously, a noisy single-bar trend_reversal (ema_20 <
        # ema_50 for BUY) could FULL_EXIT a position the moment it
        # crossed, even on the exact bar it also reached final_target —
        # capping the win at whatever trend_reversal's confidence/label
        # was instead of recording it as a target hit. Stop-loss still
        # protects capital first (unchanged above); once capital is
        # safe, "did we already win" is checked before "is the trend
        # getting shaky", so a trade that reached its target is never
        # silently reclassified as a reversal-exit.

        elif final_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 95.0

            exit_reason = "Final target achieved."

        # ==========================================================
        # PARTIAL TARGET
        # ==========================================================

        elif partial_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 85.0

            exit_reason = "Partial target achieved."

        # ==========================================================
        # TREND REVERSAL
        # ==========================================================
        # Still fires BEFORE news/volatility/time exits — a confirmed
        # reversal that hasn't yet reached a profit target is real risk
        # and should exit ahead of softer signals, just not ahead of a
        # target that's already been hit (see above).

        elif trend_reversal:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 90.0

            exit_reason = "Trend reversal confirmed."

        # ==========================================================
        # NEWS EXIT
        # ==========================================================

        elif news_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 90.0

            exit_reason = "Negative news risk."

        # ==========================================================
        # VOLATILITY EXIT
        # ==========================================================

        elif volatility_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 80.0

            exit_reason = "High market volatility."

        # ==========================================================
        # TIME EXIT
        # ==========================================================

        elif time_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 75.0

            exit_reason = "Maximum holding period."

        # ==========================================================
        # HOLD
        # ==========================================================

        reasons.append(exit_reason)

        diagnostics["exit_action"] = action

        diagnostics["exit_percent"] = exit_percent

        diagnostics["exit_confidence"] = confidence

        diagnostics["exit_reason"] = exit_reason
        # ==========================================================
        # DYNAMIC STOP ADJUSTMENT
        # ==========================================================

        if action == HOLD:

            if decision.action == "BUY":

                dynamic_stop = max(
                    active_stop,
                    current_price - (atr * 2.0),
                )

            else:

                dynamic_stop = min(
                    active_stop,
                    current_price + (atr * 2.0),
                )

        else:

            dynamic_stop = active_stop

        diagnostics["dynamic_stop"] = round(
            dynamic_stop,
            2,
        )

        # ==========================================================
        # PROFIT LOCK
        # ==========================================================

        unrealized_return = (
            (current_price - entry_price)
            / max(
                entry_price,
                0.01,
            )
        ) * 100

        if decision.action != "BUY":

            unrealized_return *= -1

        diagnostics["unrealized_return"] = round(
            unrealized_return,
            2,
        )

        profit_locked = False

        if unrealized_return >= 10:

            profit_locked = True

            reasons.append("Profit protection activated.")

        diagnostics["profit_locked"] = profit_locked

        # ==========================================================
        # CONFIDENCE REFINEMENT
        # ==========================================================

        confidence = (
            confidence * 0.60
            + decision.confidence * 0.20
            + (100.0 - risk.total_risk) * 0.20
        )

        confidence = round(
            min(
                confidence,
                100.0,
            ),
            2,
        )

        diagnostics["final_confidence"] = confidence

        # ==========================================================
        # EXIT QUALITY
        # ==========================================================

        exit_quality = (
            expected_rr * 20.0 + confidence * 0.40 + (100.0 - risk.total_risk) * 0.20
        )

        exit_quality = min(
            exit_quality,
            100.0,
        )

        diagnostics["exit_quality"] = round(
            exit_quality,
            2,
        )

        # ==========================================================
        # EXIT ANALYTICS
        # ==========================================================

        diagnostics["holding_days"] = holding_days

        diagnostics["entry_price"] = round(
            entry_price,
            2,
        )

        diagnostics["current_price"] = round(
            current_price,
            2,
        )

        diagnostics["highest_price"] = round(
            highest_price,
            2,
        )

        diagnostics["lowest_price"] = round(
            lowest_price,
            2,
        )

        logger.info("Exit analytics generated.")
        # ==========================================================
        # POSITION HEALTH
        # ==========================================================

        if unrealized_return >= 15:

            position_health = "EXCELLENT"

        elif unrealized_return >= 8:

            position_health = "STRONG"

        elif unrealized_return >= 3:

            position_health = "HEALTHY"

        elif unrealized_return >= 0:

            position_health = "NEUTRAL"

        elif unrealized_return >= -5:

            position_health = "WEAK"

        else:

            position_health = "CRITICAL"

        diagnostics["position_health"] = position_health

        # ==========================================================
        # EXIT GRADE
        # ==========================================================

        if exit_quality >= 95:

            exit_grade = "A+"

        elif exit_quality >= 90:

            exit_grade = "A"

        elif exit_quality >= 80:

            exit_grade = "B+"

        elif exit_quality >= 70:

            exit_grade = "B"

        elif exit_quality >= 60:

            exit_grade = "C"

        else:

            exit_grade = "D"

        diagnostics["exit_grade"] = exit_grade

        # ==========================================================
        # AI RECOMMENDATIONS
        # ==========================================================

        recommendations: list[str] = []

        if action == HOLD:

            recommendations.append("Continue following the exit plan.")

        elif action == PARTIAL_EXIT:

            recommendations.append(
                "Book partial profits and trail the remaining position."
            )

        else:

            recommendations.append("Exit the position completely.")

        if profit_locked:

            recommendations.append("Keep stop-loss above break-even.")

        if expected_rr >= 3.0:

            recommendations.append("Maintain disciplined risk/reward execution.")

        if risk.total_risk >= 40:

            recommendations.append("Elevated portfolio risk detected.")

        diagnostics["recommendations"] = recommendations

        # ==========================================================
        # TRADE LIFECYCLE SUMMARY
        # ==========================================================

        diagnostics["trade_lifecycle"] = {
            "entry_price": round(
                entry_price,
                2,
            ),
            "current_price": round(
                current_price,
                2,
            ),
            "holding_days": holding_days,
            "unrealized_return": round(
                unrealized_return,
                2,
            ),
            "position_health": position_health,
            "exit_action": action,
            "exit_grade": exit_grade,
        }

        # ==========================================================
        # EXIT SCORECARD
        # ==========================================================

        diagnostics["scorecard"] = {
            "confidence": confidence,
            "exit_quality": round(
                exit_quality,
                2,
            ),
            "expected_rr": expected_rr,
            "risk_grade": risk.risk_grade,
        }

        logger.info("Exit recommendations prepared.")
        # ==========================================================
        # EXIT DIAGNOSTICS
        # ==========================================================

        diagnostics["exit_summary"] = {
            "action": action,
            "exit_percent": exit_percent,
            "confidence": confidence,
            "active_stop": round(
                active_stop,
                2,
            ),
            "dynamic_stop": round(
                dynamic_stop,
                2,
            ),
            "take_profit": round(
                final_target,
                2,
            ),
            "break_even": round(
                entry_price,
                2,
            ),
        }

        # ==========================================================
        # CONSISTENCY VALIDATION
        # ==========================================================

        validation_errors = []

        if action not in (
            HOLD,
            PARTIAL_EXIT,
            FULL_EXIT,
        ):

            validation_errors.append("Invalid exit action.")

        if not (0.0 <= exit_percent <= 100.0):

            validation_errors.append("Exit percent out of range.")

        if stop_loss <= 0:

            validation_errors.append("Invalid stop-loss.")

        if trailing_stop <= 0:

            validation_errors.append("Invalid trailing stop.")

        if final_target <= 0:

            validation_errors.append("Invalid take-profit.")

        if confidence < 0 or confidence > 100:

            validation_errors.append("Confidence out of range.")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE
        # ==========================================================

        if validation_errors:

            logger.error(
                "Exit strategy validation failed: %s",
                validation_errors,
            )

            action = HOLD

            exit_percent = 0.0

            confidence = 0.0

            warnings = ["Exit strategy entered fail-safe mode."]

            diagnostics["fail_safe"] = True

        else:

            warnings = []

            diagnostics["fail_safe"] = False

        # ==========================================================
        # BUILD RESULT
        # ==========================================================

        result = ExitDecision(
            action=action,
            exit_percent=round(
                exit_percent,
                2,
            ),
            stop_loss=round(
                dynamic_stop,
                2,
            ),
            trailing_stop=round(
                trailing_stop,
                2,
            ),
            take_profit=round(
                final_target,
                2,
            ),
            break_even_price=round(
                entry_price,
                2,
            ),
            expected_rr=round(
                expected_rr,
                2,
            ),
            confidence=round(
                confidence,
                2,
            ),
            reasons=reasons,
            diagnostics=diagnostics,
        )

        logger.info("ExitDecision created successfully.")

        return result

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(
        result: ExitDecision,
    ) -> dict[str, Any]:

        return {
            "action": result.action,
            "exit_percent": result.exit_percent,
            "stop_loss": result.stop_loss,
            "trailing_stop": result.trailing_stop,
            "take_profit": result.take_profit,
            "break_even_price": result.break_even_price,
            "expected_rr": result.expected_rr,
            "confidence": result.confidence,
            "reasons": result.reasons,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        result: ExitDecision,
    ) -> str:

        return (
            f"{result.action}"
            f" | Exit={result.exit_percent:.0f}%"
            f" | Stop={result.stop_loss:.2f}"
            f" | Target={result.take_profit:.2f}"
            f" | RR={result.expected_rr:.2f}"
            f" | Confidence={result.confidence:.2f}%"
        )

    # ==========================================================
    # SHORT REPORT
    # ==========================================================

    @staticmethod
    def short_report(
        result: ExitDecision,
    ) -> str:

        lines = []

        lines.append(f"Action : {result.action}")

        lines.append(f"Exit : {result.exit_percent:.0f}%")

        lines.append(f"Stop : {result.stop_loss:.2f}")

        lines.append(f"Target : {result.take_profit:.2f}")

        lines.append(f"RR : {result.expected_rr:.2f}")

        lines.append(f"Confidence : {result.confidence:.2f}%")

        return "\n".join(lines)

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        result: ExitDecision,
    ) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("EXIT STRATEGY REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Action               : {result.action}")

        report.append(f"Exit Percent         : {result.exit_percent:.2f}%")

        report.append(f"Stop Loss            : {result.stop_loss:.2f}")

        report.append(f"Trailing Stop        : {result.trailing_stop:.2f}")

        report.append(f"Take Profit          : {result.take_profit:.2f}")

        report.append(f"Break-even Price     : {result.break_even_price:.2f}")

        report.append(f"Expected R:R         : {result.expected_rr:.2f}")

        report.append(f"Confidence           : {result.confidence:.2f}%")

        report.append("")

        report.append("Reasons")
        report.append("-" * 70)

        if result.reasons:

            for reason in result.reasons:

                report.append(f"• {reason}")

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
