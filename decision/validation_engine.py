"""
Validation Engine

Institutional Production Version

Responsibilities
----------------
Validate every trade before it reaches the Risk Engine.

This engine NEVER changes a BUY into SELL.

It can only

PASS

or

REJECT

Validation Layers

1. Market
2. Data
3. Liquidity
4. Portfolio
5. Risk
6. Broker
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from decision.decision_engine import FinalDecision
from risk import portfolio_limits

from core.constants import (
    BUY,
    SELL,
    NO_TRADE,
)

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class ValidationResult:

    passed: bool

    action: str

    confidence: float

    rejection_reason: str | None

    warnings: list[str] = field(default_factory=list)

    checks: dict[str, bool] = field(default_factory=dict)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# ENGINE
# ==========================================================


class ValidationEngine:
    """
    Final gate before Risk Manager.
    """

    MIN_AVG_VOLUME = 500000

    MAX_SPREAD = 0.005

    MAX_OPEN_POSITIONS = 100
    
    MAX_SECTOR_EXPOSURE = 0.30

    MAX_CORRELATION = 0.80

    # Phase 22: daily-loss now uses risk/portfolio_limits.py's shared
    # 4-stage hierarchy instead of this flat constant — see the DAILY LOSS
    # section in validate(). No class constant needed here anymore.

    MAX_WEEKLY_LOSS = 0.06

    MAX_MONTHLY_LOSS = 0.12

    def validate(
        self,
        decision: FinalDecision,
        dataframe: pd.DataFrame,
        portfolio: dict[str, Any],
        broker_status: dict[str, Any],
        market_state: dict[str, Any],
        skip_position_count: bool = False,
    ) -> ValidationResult:

        checks = {}

        warnings = []

        diagnostics = {}

        rejection_reason = None

        logger.info("Starting validation engine.")

        # --------------------------------------------------
        # NO TRADE
        # --------------------------------------------------

        if decision.action == NO_TRADE:

            return ValidationResult(
                passed=False,
                action=NO_TRADE,
                confidence=0.0,
                rejection_reason="Decision engine rejected trade.",
            )

        latest = dataframe.iloc[-1]
        # ==========================================================
        # MARKET VALIDATION
        # ==========================================================

        # --------------------------------------------------
        # MARKET OPEN
        # --------------------------------------------------

        checks["market_open"] = bool(
            market_state.get(
                "market_open",
                False,
            )
        )

        if not checks["market_open"]:

            rejection_reason = "Market is closed."

        # --------------------------------------------------
        # HOLIDAY
        # --------------------------------------------------

        checks["holiday"] = not bool(
            market_state.get(
                "holiday",
                False,
            )
        )

        if rejection_reason is None and not checks["holiday"]:

            rejection_reason = "Market holiday."

        # --------------------------------------------------
        # WEEKEND
        # --------------------------------------------------

        # Use the dataframe's own latest date, not the real current date —
        # important for backtesting, where "today" means the simulated
        # historical date being tested, not whatever day this code
        # actually runs on.
        if "timestamp" in dataframe.columns:
            weekday = pd.Timestamp(latest["timestamp"]).weekday()
        else:
            weekday = datetime.now().weekday()

        checks["weekend"] = weekday < 5

        if rejection_reason is None and not checks["weekend"]:

            rejection_reason = "Weekend trading blocked."

        # --------------------------------------------------
        # CIRCUIT BREAKER
        # --------------------------------------------------
        # Phase 22 (see PHASE22_NOTES.md): was reading `latest.get(...)` —
        # `latest = dataframe.iloc[-1]`, a price/indicator dataframe row,
        # not a market dict. No producer ever wrote a "circuit_breaker"
        # column there, so this could never fire. Reads from
        # `market_state` (this function's actual market-condition dict
        # parameter) instead — same fix as risk/risk_manager.py.
        # Phase 25 (see PHASE25_NOTES.md): now genuinely populated — see
        # risk/risk_manager.py's matching comment for the producer.

        checks["circuit"] = not bool(
            market_state.get(
                "circuit_breaker",
                False,
            )
        )

        if rejection_reason is None and not checks["circuit"]:

            rejection_reason = "Circuit breaker active."

        # --------------------------------------------------
        # EXCHANGE STATUS
        # --------------------------------------------------

        checks["exchange"] = (
            market_state.get(
                "exchange_status",
                "OPEN",
            )
            == "OPEN"
        )

        if rejection_reason is None and not checks["exchange"]:

            rejection_reason = "Exchange unavailable."

        # ==========================================================
        # DATA VALIDATION
        # ==========================================================

        checks["dataframe"] = not dataframe.empty

        if rejection_reason is None and not checks["dataframe"]:

            rejection_reason = "Empty market data."

        # Only check the LATEST (usable) row — not the entire history.
        # Long lookback indicators (SMA200/EMA200 etc.) are legitimately
        # NaN for their warmup period at the START of any dataframe; that
        # doesn't mean today's data is bad. chikou_span is excluded
        # entirely: Ichimoku's lagging span is, by definition, always NaN
        # for the most recent ~26 rows of ANY dataset (live or
        # historical) since it plots the close price shifted backward in
        # time — and it isn't used in any decision logic anyway.
        #
        # "breadth" is excluded for the same class of reason (found via
        # diagnose_buy_pipeline.py — every single candidate was being
        # rejected with "NaN values detected."): execution/scanner.py's
        # FIX #8 deliberately sets dataframe["breadth"] = None (real
        # market-wide advance/decline data isn't wired into this
        # per-symbol pipeline yet — see that comment) instead of a
        # fabricated 50.0, so consumers can tell "unavailable" apart
        # from "actually neutral". strategy/buy_strategy.py's and
        # sell_strategy.py's own Tier2 checks already treat a missing
        # breadth as a no-op (`row.get("breadth", "NEUTRAL") == "STRONG"`
        # is False for None, contributing nothing either way) — so this
        # column being None was never meant to block a trade; it just
        # wasn't excluded here yet, unlike chikou_span, causing EVERY
        # candidate to fail validation regardless of real signal quality.
        latest_check_row = latest.drop(labels=["chikou_span", "breadth"], errors="ignore")
        checks["nan"] = not latest_check_row.isna().any()

        if rejection_reason is None and not checks["nan"]:

            rejection_reason = "NaN values detected."

        checks["duplicate_index"] = not dataframe.index.duplicated().any()

        if rejection_reason is None and not checks["duplicate_index"]:

            rejection_reason = "Duplicate timestamps detected."

        checks["minimum_history"] = len(dataframe) >= 250

        if rejection_reason is None and not checks["minimum_history"]:

            rejection_reason = "Insufficient historical candles."
        # ==========================================================
        # LIQUIDITY VALIDATION
        # ==========================================================

        # --------------------------------------------------
        # AVERAGE VOLUME
        # --------------------------------------------------

        avg_volume = float(
            latest.get(
                "volume_sma_20",
                0.0,
            )
        )

        checks["average_volume"] = avg_volume >= self.MIN_AVG_VOLUME

        if rejection_reason is None and not checks["average_volume"]:

            rejection_reason = "Average trading volume is too low."

        diagnostics["average_volume"] = avg_volume

        # --------------------------------------------------
        # CURRENT VOLUME
        # --------------------------------------------------

        current_volume = float(
            latest.get(
                "volume",
                0.0,
            )
        )

        checks["current_volume"] = current_volume > 0

        if rejection_reason is None and not checks["current_volume"]:

            rejection_reason = "Current volume unavailable."

        diagnostics["current_volume"] = current_volume

        # --------------------------------------------------
        # BID / ASK SPREAD
        # --------------------------------------------------

        spread = float(
            latest.get(
                "spread",
                0.0,
            )
        )

        checks["spread"] = spread <= self.MAX_SPREAD

        if rejection_reason is None and not checks["spread"]:

            rejection_reason = "Spread exceeds allowed limit."

        diagnostics["spread"] = spread

        # --------------------------------------------------
        # TURNOVER
        # --------------------------------------------------

        turnover = float(
            latest.get(
                "turnover",
                0.0,
            )
        )

        checks["turnover"] = turnover > 0

        if not checks["turnover"]:

            warnings.append("Turnover data unavailable.")

        diagnostics["turnover"] = turnover

        # --------------------------------------------------
        # DELIVERY PERCENTAGE
        # --------------------------------------------------

        delivery = float(
            latest.get(
                "delivery_percentage",
                100.0,
            )
        )

        checks["delivery"] = delivery >= 20.0

        if not checks["delivery"]:

            warnings.append("Low delivery percentage.")

        diagnostics["delivery_percentage"] = delivery

        # --------------------------------------------------
        # SLIPPAGE
        # --------------------------------------------------

        slippage = float(
            latest.get(
                "estimated_slippage",
                0.0,
            )
        )

        checks["slippage"] = slippage <= 0.003

        if not checks["slippage"]:

            warnings.append("Estimated slippage is high.")

        diagnostics["estimated_slippage"] = slippage

        # --------------------------------------------------
        # LIQUIDITY SCORE
        # --------------------------------------------------

        liquidity_checks = [
            checks["average_volume"],
            checks["current_volume"],
            checks["spread"],
            checks["turnover"],
            checks["delivery"],
            checks["slippage"],
        ]

        liquidity_score = (sum(liquidity_checks) / len(liquidity_checks)) * 100

        diagnostics["liquidity_score"] = round(
            liquidity_score,
            2,
        )

        if liquidity_score < 60:

            warnings.append("Overall liquidity quality is weak.")
        # ==========================================================
        # PORTFOLIO VALIDATION
        # ==========================================================

        # --------------------------------------------------
        # DUPLICATE POSITION
        # --------------------------------------------------

        symbol = latest.get(
            "symbol",
            None,
        )

        open_positions = portfolio.get(
            "open_positions",
            {},
        )

        checks["duplicate_position"] = symbol not in open_positions

        if rejection_reason is None and not checks["duplicate_position"]:

            rejection_reason = "Position already exists."

        diagnostics["symbol"] = symbol

        # --------------------------------------------------
        # MAX POSITIONS
        # ENTRY-ONLY concept ("is there room to add ONE MORE
        # position") — meaningless when monitoring an ALREADY-open
        # position, so evaluate_position() (monitoring) passes
        # skip_position_count=True. Entry-path callers (scan_symbol /
        # scan_symbols) never pass this, so their behavior is
        # completely unchanged.
        # --------------------------------------------------

        position_count = len(open_positions)

        if skip_position_count:
            checks["max_positions"] = True
        else:
            checks["max_positions"] = position_count < self.MAX_OPEN_POSITIONS

        if rejection_reason is None and not checks["max_positions"]:

            rejection_reason = "Maximum portfolio positions reached."

        diagnostics["position_count"] = position_count

        # --------------------------------------------------
        # AVAILABLE CAPITAL
        # --------------------------------------------------

        available_cash = float(
            portfolio.get(
                "available_cash",
                portfolio.get("available_capital", 0.0),
            )
        )

        checks["capital"] = available_cash > 0

        if rejection_reason is None and not checks["capital"]:

            rejection_reason = "No available capital."

        diagnostics["available_cash"] = available_cash

        # --------------------------------------------------
        # CASH RESERVE
        # --------------------------------------------------

        total_capital = float(
            portfolio.get(
                "total_capital",
                1.0,
            )
        )

        cash_ratio = available_cash / max(
            total_capital,
            1.0,
        )

        checks["cash_reserve"] = cash_ratio >= 0.10

        if not checks["cash_reserve"]:

            warnings.append("Cash reserve below 10%.")

        diagnostics["cash_ratio"] = round(
            cash_ratio,
            4,
        )

        # --------------------------------------------------
        # SECTOR EXPOSURE
        # --------------------------------------------------

        sector_exposure = float(
            portfolio.get(
                "sector_exposure",
                0.0,
            )
        )

        checks["sector_exposure"] = sector_exposure <= self.MAX_SECTOR_EXPOSURE

        if rejection_reason is None and not checks["sector_exposure"]:

            rejection_reason = "Sector exposure exceeded."

        diagnostics["sector_exposure"] = sector_exposure

        # --------------------------------------------------
        # CORRELATION
        # --------------------------------------------------

        correlation = float(
            portfolio.get(
                "correlation",
                0.0,
            )
        )

        checks["correlation"] = correlation <= self.MAX_CORRELATION

        if rejection_reason is None and not checks["correlation"]:

            rejection_reason = "Portfolio correlation too high."

        diagnostics["correlation"] = correlation

        # --------------------------------------------------
        # PORTFOLIO HEALTH
        # --------------------------------------------------

        portfolio_score = (
            sum(
                [
                    checks["duplicate_position"],
                    checks["max_positions"],
                    checks["capital"],
                    checks["cash_reserve"],
                    checks["sector_exposure"],
                    checks["correlation"],
                ]
            )
            / 6
        ) * 100

        diagnostics["portfolio_score"] = round(
            portfolio_score,
            2,
        )

        if portfolio_score < 60:

            warnings.append("Portfolio health is below recommended level.")
        # ==========================================================
        # RISK VALIDATION
        # ==========================================================

        # --------------------------------------------------
        # DAILY LOSS
        # --------------------------------------------------

        daily_loss = float(
            portfolio.get(
                "daily_loss",
                0.0,
            )
        )

        # Phase 22 (see PHASE22_NOTES.md, point 10): previously its own
        # independent `self.MAX_DAILY_LOSS = 0.03` flat cutoff — a THIRD
        # hardcoded copy of this threshold (alongside portfolio_rules.py's
        # 3% and risk_manager.py's since-fixed 5.0 unit bug). Now sourced
        # from the same shared 4-stage hierarchy as those two — rejects at
        # the same trading_halt/emergency stages portfolio_rules.py does.
        checks["daily_loss"] = portfolio_limits.daily_loss_stage(daily_loss) not in (
            "trading_halt",
            "emergency",
        )

        if rejection_reason is None and not checks["daily_loss"]:

            rejection_reason = "Daily loss limit exceeded."

        diagnostics["daily_loss"] = round(
            daily_loss,
            4,
        )

        # --------------------------------------------------
        # WEEKLY LOSS
        # --------------------------------------------------

        weekly_loss = float(
            portfolio.get(
                "weekly_loss",
                0.0,
            )
        )

        checks["weekly_loss"] = weekly_loss <= self.MAX_WEEKLY_LOSS

        if rejection_reason is None and not checks["weekly_loss"]:

            rejection_reason = "Weekly loss limit exceeded."

        diagnostics["weekly_loss"] = round(
            weekly_loss,
            4,
        )

        # --------------------------------------------------
        # MONTHLY LOSS
        # --------------------------------------------------

        monthly_loss = float(
            portfolio.get(
                "monthly_loss",
                0.0,
            )
        )

        checks["monthly_loss"] = monthly_loss <= self.MAX_MONTHLY_LOSS

        if rejection_reason is None and not checks["monthly_loss"]:

            rejection_reason = "Monthly loss limit exceeded."

        diagnostics["monthly_loss"] = round(
            monthly_loss,
            4,
        )

        # --------------------------------------------------
        # MAX DRAWDOWN
        # --------------------------------------------------

        max_drawdown = float(
            portfolio.get(
                "max_drawdown",
                0.0,
            )
        )

        # Phase 22: was a flat `<= 0.20` cutoff — a second hardcoded copy
        # of portfolio_rules.py's old threshold. Now uses the same
        # graduated-band halt threshold (>15%) as portfolio_rules.py.
        checks["drawdown"] = portfolio_limits.drawdown_band_label(max_drawdown) != "halt"

        if rejection_reason is None and not checks["drawdown"]:

            rejection_reason = "Portfolio drawdown exceeds limit."

        diagnostics["max_drawdown"] = round(
            max_drawdown,
            4,
        )

        # --------------------------------------------------
        # PORTFOLIO RISK
        # --------------------------------------------------

        portfolio_risk = float(
            portfolio.get(
                "portfolio_risk",
                0.0,
            )
        )

        checks["portfolio_risk"] = portfolio_risk <= 0.30

        if rejection_reason is None and not checks["portfolio_risk"]:

            rejection_reason = "Portfolio risk exceeds allowed limit."

        diagnostics["portfolio_risk"] = round(
            portfolio_risk,
            4,
        )

        # --------------------------------------------------
        # OPEN EXPOSURE
        # --------------------------------------------------

        open_exposure = float(
            portfolio.get(
                "open_exposure",
                0.0,
            )
        )

        checks["open_exposure"] = open_exposure <= 0.90

        if not checks["open_exposure"]:

            warnings.append("Portfolio exposure is very high.")

        diagnostics["open_exposure"] = round(
            open_exposure,
            4,
        )

        # --------------------------------------------------
        # RISK SCORE
        # --------------------------------------------------

        risk_checks = [
            checks["daily_loss"],
            checks["weekly_loss"],
            checks["monthly_loss"],
            checks["drawdown"],
            checks["portfolio_risk"],
            checks["open_exposure"],
        ]

        risk_score = (sum(risk_checks) / len(risk_checks)) * 100

        diagnostics["risk_score"] = round(
            risk_score,
            2,
        )

        if risk_score < 70:

            warnings.append("Portfolio risk profile is elevated.")
        # ==========================================================
        # BROKER VALIDATION
        # ==========================================================

        # --------------------------------------------------
        # API CONNECTION
        # --------------------------------------------------

        checks["api_connected"] = bool(
            broker_status.get(
                "connected",
                False,
            )
        )

        if rejection_reason is None and not checks["api_connected"]:

            rejection_reason = "Broker API not connected."

        diagnostics["api_connected"] = checks["api_connected"]

        # --------------------------------------------------
        # ORDER PERMISSION
        # --------------------------------------------------

        checks["order_allowed"] = bool(
            broker_status.get(
                "order_allowed",
                False,
            )
        )

        if rejection_reason is None and not checks["order_allowed"]:

            rejection_reason = "Order placement is disabled."

        diagnostics["order_allowed"] = checks["order_allowed"]

        # --------------------------------------------------
        # MARGIN AVAILABLE
        # --------------------------------------------------

        available_margin = float(
            broker_status.get(
                "available_margin",
                0.0,
            )
        )

        checks["margin"] = available_margin > 0

        if rejection_reason is None and not checks["margin"]:

            rejection_reason = "Insufficient trading margin."

        diagnostics["available_margin"] = round(
            available_margin,
            2,
        )

        # --------------------------------------------------
        # HOLDINGS SYNCHRONIZED
        # --------------------------------------------------

        checks["holdings_sync"] = bool(
            broker_status.get(
                "holdings_sync",
                True,
            )
        )

        if not checks["holdings_sync"]:

            warnings.append("Broker holdings are not synchronized.")

        diagnostics["holdings_sync"] = checks["holdings_sync"]

        # --------------------------------------------------
        # POSITIONS SYNCHRONIZED
        # --------------------------------------------------

        checks["positions_sync"] = bool(
            broker_status.get(
                "positions_sync",
                True,
            )
        )

        if not checks["positions_sync"]:

            warnings.append("Broker positions are not synchronized.")

        diagnostics["positions_sync"] = checks["positions_sync"]

        # --------------------------------------------------
        # MARKET DATA CONNECTION
        # --------------------------------------------------

        checks["market_data_feed"] = bool(
            broker_status.get(
                "market_data_feed",
                True,
            )
        )

        if rejection_reason is None and not checks["market_data_feed"]:

            rejection_reason = "Live market data feed unavailable."

        diagnostics["market_data_feed"] = checks["market_data_feed"]

        # --------------------------------------------------
        # ORDER ROUTING
        # --------------------------------------------------

        checks["order_routing"] = bool(
            broker_status.get(
                "order_routing",
                True,
            )
        )

        if rejection_reason is None and not checks["order_routing"]:

            rejection_reason = "Order routing service unavailable."

        diagnostics["order_routing"] = checks["order_routing"]

        # --------------------------------------------------
        # BROKER HEALTH SCORE
        # --------------------------------------------------

        broker_checks = [
            checks["api_connected"],
            checks["order_allowed"],
            checks["margin"],
            checks["holdings_sync"],
            checks["positions_sync"],
            checks["market_data_feed"],
            checks["order_routing"],
        ]

        broker_health = (sum(broker_checks) / len(broker_checks)) * 100

        diagnostics["broker_health"] = round(
            broker_health,
            2,
        )

        if broker_health < 80:

            warnings.append("Broker health score is below recommended level.")
        # ==========================================================
        # OVERALL VALIDATION SCORE
        # ==========================================================

        total_checks = len(checks)

        passed_checks = sum(bool(value) for value in checks.values())

        validation_score = (
            passed_checks
            / max(
                total_checks,
                1,
            )
        ) * 100

        validation_score = round(
            validation_score,
            2,
        )

        diagnostics["validation_score"] = validation_score

        diagnostics["passed_checks"] = passed_checks

        diagnostics["failed_checks"] = total_checks - passed_checks

        # ==========================================================
        # FINAL PASS / FAIL
        # ==========================================================

        passed = rejection_reason is None

        if passed:

            reasons = ["All validation layers passed."]

        else:

            reasons = [rejection_reason]

        diagnostics["passed"] = passed

        diagnostics["rejection_reason"] = rejection_reason

        # ==========================================================
        # VALIDATION SUMMARY
        # ==========================================================

        diagnostics["summary"] = {
            "decision": decision.action,
            "validation_score": validation_score,
            "passed": passed,
            "warning_count": len(warnings),
            "failed_checks": diagnostics["failed_checks"],
        }

        logger.info(
            "Validation Score %.2f | Passed=%s",
            validation_score,
            passed,
        )

        # ==========================================================
        # FINAL VALIDATION LOG
        # ==========================================================

        if warnings:

            logger.warning(
                "Validation warnings: %s",
                "; ".join(warnings),
            )

        if rejection_reason:

            logger.warning(
                "Trade rejected: %s",
                rejection_reason,
            )

        else:

            logger.info("Trade passed validation.")
        # ==========================================================
        # RETURN RESULT
        # ==========================================================

        return ValidationResult(
            passed=passed,
            action=decision.action if passed else NO_TRADE,
            confidence=(decision.confidence if passed else 0.0),
            rejection_reason=rejection_reason,
            warnings=warnings,
            checks=checks,
            diagnostics=diagnostics,
        )

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(
        result: ValidationResult,
    ) -> dict[str, Any]:

        return {
            "passed": result.passed,
            "action": result.action,
            "confidence": result.confidence,
            "rejection_reason": result.rejection_reason,
            "warnings": result.warnings,
            "checks": result.checks,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        result: ValidationResult,
    ) -> str:

        status = "PASS" if result.passed else "REJECT"

        return (
            f"{status}"
            f" | Action={result.action}"
            f" | Confidence={result.confidence:.2f}%"
            f" | Warnings={len(result.warnings)}"
        )

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        result: ValidationResult,
    ) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("VALIDATION ENGINE REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Status              : {'PASS' if result.passed else 'REJECT'}")

        report.append(f"Action              : {result.action}")

        report.append(f"Confidence          : {result.confidence:.2f}%")

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
        report.append("Validation Checks")
        report.append("-" * 70)

        for key, value in sorted(result.checks.items()):

            status = "PASS" if value else "FAIL"

            report.append(f"{key:<30} {status}")

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
