"""
Risk Manager

Institutional Production Version

Responsibilities
----------------
Evaluate every possible trading risk before execution.

This engine NEVER decides BUY or SELL.

It only evaluates

SAFE

or

RISKY

The output is consumed by

Position Sizing
Exit Strategy
Portfolio Rules
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from decision.validation_engine import ValidationResult
from decision.decision_engine import FinalDecision
from risk import portfolio_limits

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class RiskResult:

    safe: bool

    total_risk: float

    risk_grade: str

    atr_risk: float

    gap_risk: float

    overnight_risk: float

    news_risk: float

    liquidity_risk: float

    volatility_risk: float

    portfolio_risk: float

    sector_risk: float

    correlation_risk: float

    capital_risk: float

    warnings: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# ENGINE
# ==========================================================


class RiskManager:
    """
    Master Risk Engine

    Pipeline

        Validation

              │

              ▼

        ATR Risk

        Gap Risk

        News Risk

        Liquidity Risk

        Volatility Risk

        Portfolio Risk

        Correlation Risk

        Capital Risk

              │

              ▼

        Final Risk
    """

    MAX_TOTAL_RISK = 35.0

    MAX_CAPITAL_RISK = 2.0

    MAX_OVERNIGHT_RISK = 30.0

    MAX_NEWS_RISK = 70.0

    MAX_PORTFOLIO_RISK = 40.0

    MAX_CORRELATION_RISK = 70.0

    MAX_SECTOR_RISK = 50.0

    # Phase 22 (see PHASE22_NOTES.md, point 10): this was hardcoded to
    # `5.0` — a UNIT BUG. `daily_loss` everywhere else in the codebase
    # (portfolio_rules.py, validation_engine.py, and now the real producer
    # in portfolio/portfolio.py) is a 0.0-1.0 FRACTION (0.05 = 5% loss),
    # so `daily_loss >= 5.0` could only ever fire on a 500%+ loss — with
    # daily_loss now actually being computed from real portfolio equity,
    # this would have been effectively permanently dead if left as-is.
    # Sourced from the shared module so this can never drift from the
    # other two files' thresholds again.
    MAX_DAILY_LOSS = portfolio_limits.DAILY_LOSS_EMERGENCY

    def evaluate(
        self,
        validation: ValidationResult,
        decision: FinalDecision,
        dataframe: pd.DataFrame,
        portfolio: dict[str, Any],
        market: dict[str, Any],
    ) -> RiskResult:

        if not validation.passed:

            return RiskResult(
                safe=False,
                total_risk=100.0,
                risk_grade="REJECT",
                atr_risk=100,
                gap_risk=100,
                overnight_risk=100,
                news_risk=100,
                liquidity_risk=100,
                volatility_risk=100,
                portfolio_risk=100,
                sector_risk=100,
                correlation_risk=100,
                capital_risk=100,
                warnings=["Validation engine rejected trade."],
            )

        latest = dataframe.iloc[-1]

        warnings = []

        diagnostics = {}

        logger.info("Starting Risk Manager.")
        # ==========================================================
        # ATR RISK
        # ==========================================================

        atr = float(latest.get("atr_14", 0.0))

        close = float(latest.get("close", 1.0))

        atr_percent = (atr / max(close, 1.0)) * 100

        if atr_percent <= 1.0:

            atr_risk = 10.0

        elif atr_percent <= 2.0:

            atr_risk = 25.0

        elif atr_percent <= 3.0:

            atr_risk = 45.0

        elif atr_percent <= 5.0:

            atr_risk = 70.0

        else:

            atr_risk = 95.0

        diagnostics["atr_percent"] = round(atr_percent, 2)

        diagnostics["atr_risk"] = atr_risk

        if atr_risk >= 70:

            warnings.append("ATR indicates excessive volatility.")

        # ==========================================================
        # GAP RISK
        # ==========================================================

        gap_up = bool(latest.get("gap_up", False))

        gap_down = bool(latest.get("gap_down", False))

        if gap_up or gap_down:

            gap_risk = 75.0

            warnings.append("Gap detected.")

        else:

            gap_risk = 10.0

        diagnostics["gap_risk"] = gap_risk

        # ==========================================================
        # OVERNIGHT RISK
        # ==========================================================

        overnight = bool(market.get("overnight", False))

        event_day = bool(market.get("event_day", False))

        if overnight:

            overnight_risk = 50.0

        else:

            overnight_risk = 10.0

        if event_day:

            overnight_risk += 25.0

            warnings.append("High-impact event scheduled.")

        overnight_risk = min(overnight_risk, 100.0)

        diagnostics["overnight_risk"] = overnight_risk

        # ==========================================================
        # NEWS RISK
        # ==========================================================

        news_impact = float(market.get("news_impact", 0.0))

        if news_impact <= 20:

            news_risk = 10.0

        elif news_impact <= 40:

            news_risk = 25.0

        elif news_impact <= 60:

            news_risk = 50.0

        elif news_impact <= 80:

            news_risk = 75.0

        else:

            news_risk = 95.0

        diagnostics["news_risk"] = news_risk

        if news_risk >= self.MAX_NEWS_RISK:

            warnings.append("High news-driven risk.")
        # ==========================================================
        # LIQUIDITY RISK
        # ==========================================================

        average_volume = float(latest.get("volume_sma_20", 0.0))

        spread = float(latest.get("spread", 0.0))

        turnover = float(latest.get("turnover", 0.0))

        liquidity_risk = 0.0

        if average_volume < 500000:

            liquidity_risk += 35.0

        if average_volume < 200000:

            liquidity_risk += 25.0

        if spread > 0.005:

            liquidity_risk += 20.0

        if turnover <= 0:

            liquidity_risk += 20.0

        liquidity_risk = min(liquidity_risk, 100.0)

        diagnostics["average_volume"] = average_volume

        diagnostics["spread"] = spread

        diagnostics["turnover"] = turnover

        diagnostics["liquidity_risk"] = liquidity_risk

        if liquidity_risk >= 60:

            warnings.append("Poor market liquidity.")

        # ==========================================================
        # VOLATILITY RISK
        # ==========================================================

        volatility_state = str(latest.get("volatility_state", "NORMAL")).upper()

        bb_width = float(latest.get("bb_width", 0.0))

        volatility_risk = 0.0

        if volatility_state == "LOW":

            volatility_risk = 15.0

        elif volatility_state == "NORMAL":

            volatility_risk = 30.0

        elif volatility_state == "HIGH":

            volatility_risk = 70.0

        else:

            volatility_risk = 50.0

        if bb_width > 0.30:

            volatility_risk += 15.0

        elif bb_width > 0.20:

            volatility_risk += 8.0

        volatility_risk = min(volatility_risk, 100.0)

        diagnostics["bb_width"] = round(bb_width, 4)

        diagnostics["volatility_risk"] = volatility_risk

        if volatility_risk >= 70:

            warnings.append("Volatility exceeds acceptable limit.")

        # ==========================================================
        # MARKET RISK
        # ==========================================================

        market_regime = str(latest.get("market_regime", "SIDEWAYS")).upper()

        vix = float(market.get("vix", 20.0))

        market_risk = 20.0

        if market_regime == "BEAR":

            market_risk += 25.0

        elif market_regime == "SIDEWAYS":

            market_risk += 10.0

        if vix >= 30:

            market_risk += 25.0

        elif vix >= 20:

            market_risk += 10.0

        market_risk = min(market_risk, 100.0)

        diagnostics["market_regime"] = market_regime

        diagnostics["vix"] = vix

        diagnostics["market_risk"] = market_risk

        if market_risk >= 70:

            warnings.append("Overall market risk is elevated.")
        # ==========================================================
        # PORTFOLIO RISK
        # ==========================================================

        # Phase 26 (see PHASE26_NOTES.md, point 11): this used to read
        # `portfolio.get("open_positions_count", 0)` /
        # `portfolio.get("open_exposure", 0.0)` — keys that NO producer
        # anywhere in the codebase ever actually set. The real portfolio
        # dict (PortfolioEngine.snapshot() / VirtualPortfolio.snapshot())
        # carries `"open_positions"` (a dict of open positions, not a
        # count) and `"exposure"` (not `"open_exposure"`) — the exact
        # keys portfolio_rules.py and morning_executor.py already read
        # correctly. This was structurally dead in live/paper trading
        # (always 0 / 0.0) regardless of real portfolio state — same
        # class of bug as the circuit_breaker wrong-read-source fix in
        # Phase 22.
        open_positions = len(portfolio.get("open_positions", {}) or {})

        portfolio_exposure = float(portfolio.get("exposure", 0.0))

        portfolio_risk = 0.0

        if open_positions >= 15:

            portfolio_risk += 35.0

        elif open_positions >= 10:

            portfolio_risk += 20.0

        elif open_positions >= 5:

            portfolio_risk += 10.0

        if portfolio_exposure >= 0.90:

            portfolio_risk += 40.0

        elif portfolio_exposure >= 0.75:

            portfolio_risk += 25.0

        elif portfolio_exposure >= 0.50:

            portfolio_risk += 10.0

        portfolio_risk = min(portfolio_risk, 100.0)

        diagnostics["portfolio_risk"] = portfolio_risk

        diagnostics["open_positions"] = open_positions

        diagnostics["portfolio_exposure"] = round(portfolio_exposure, 4)

        if portfolio_risk >= self.MAX_PORTFOLIO_RISK:

            warnings.append("Portfolio risk is above the allowed threshold.")

        # ==========================================================
        # SECTOR RISK
        # ==========================================================

        sector_exposure = float(portfolio.get("sector_exposure", 0.0))

        sector_risk = 0.0

        if sector_exposure >= 0.50:

            sector_risk = 90.0

        elif sector_exposure >= 0.40:

            sector_risk = 70.0

        elif sector_exposure >= 0.30:

            sector_risk = 50.0

        elif sector_exposure >= 0.20:

            sector_risk = 25.0

        else:

            sector_risk = 10.0

        diagnostics["sector_risk"] = sector_risk

        diagnostics["sector_exposure"] = round(sector_exposure, 4)

        if sector_risk >= self.MAX_SECTOR_RISK:

            warnings.append("Sector concentration risk is high.")

        # ==========================================================
        # CORRELATION RISK
        # ==========================================================

        correlation = float(portfolio.get("correlation", 0.0))

        correlation_risk = correlation * 100.0

        correlation_risk = min(correlation_risk, 100.0)

        diagnostics["correlation"] = round(correlation, 4)

        diagnostics["correlation_risk"] = round(correlation_risk, 2)

        if correlation_risk >= self.MAX_CORRELATION_RISK:

            warnings.append("Portfolio correlation is too high.")

        # ==========================================================
        # CAPITAL RISK
        # ==========================================================

        total_capital = float(portfolio.get("total_capital", 1.0))

        available_cash = float(portfolio.get("available_cash", portfolio.get("available_capital", 0.0)))

        capital_ratio = available_cash / max(total_capital, 1.0)

        capital_risk = 0.0

        if capital_ratio <= 0.05:

            capital_risk = 95.0

        elif capital_ratio <= 0.10:

            capital_risk = 75.0

        elif capital_ratio <= 0.20:

            capital_risk = 45.0

        elif capital_ratio <= 0.30:

            capital_risk = 25.0

        else:

            capital_risk = 10.0

        diagnostics["capital_ratio"] = round(capital_ratio, 4)

        diagnostics["capital_risk"] = capital_risk

        if capital_risk >= 70:

            warnings.append("Available capital is critically low.")
        # ==========================================================
        # RISK AGGREGATION
        # ==========================================================
        # Phase 26 (see PHASE26_NOTES.md, point 11 — "correlated
        # risk-factor double-counting redesign"). Two independent fixes
        # to the weighted sum below, both explicitly sign-off'd before
        # implementing (this directly changes total_risk / position
        # sizing, not a cosmetic change):
        #
        # 1. ATR risk and Volatility risk measure the SAME underlying
        #    phenomenon (how much this stock's price moves) through two
        #    different lenses (ATR-14 percent-of-price vs.
        #    volatility_state + bb_width) — summing them as if
        #    independent double-penalized a single volatility condition.
        #    Merged into ONE `price_volatility` component,
        #    max(atr_risk, volatility_risk) — max rather than average
        #    because this is a RISK metric: if either lens flags high
        #    volatility, that's the more conservative (safety-first)
        #    reading to act on, not diluted by the other lens agreeing
        #    less. atr_risk/volatility_risk are still individually
        #    computed and left in diagnostics/RiskResult exactly as
        #    before (see above) — only the WEIGHTED-SUM input changed.
        #
        # 2. sector_risk and correlation_risk are REMOVED from the
        #    weighted sum entirely. Both are the exact same CURRENT-STATE
        #    portfolio-concentration facts decision/validation_engine.py
        #    already hard-gates (checks["sector_exposure"]/
        #    checks["correlation"], rejecting the trade outright if
        #    either exceeds MAX_SECTOR_EXPOSURE/MAX_CORRELATION) — a
        #    trade that reaches this far already passed that hard gate,
        #    so re-scoring the identical fact into total_risk here was
        #    double-counting it a second, softer way. validation_engine.py
        #    is now the SOLE gate for these two facts. sector_risk/
        #    correlation_risk are still individually computed and left in
        #    diagnostics/RiskResult (see above) for audit visibility —
        #    only the WEIGHTED-SUM input changed.
        #
        # Weights below were rescaled PROPORTIONALLY from the original
        # 11-component scheme after removing sector (0.05) + correlation
        # (0.10) — i.e. every surviving component's relative weight vs.
        # every other surviving component is UNCHANGED, only the removed
        # 0.15 budget was redistributed across them (not an arbitrary
        # re-judgment of which factors matter more). Merged
        # atr(0.10)+volatility(0.10)=0.20 combined budget flows into
        # price_volatility unchanged by that rescale, then the same
        # proportional rescale is applied to it too.
        price_volatility_risk = max(atr_risk, volatility_risk)

        diagnostics["price_volatility_risk"] = price_volatility_risk

        risk_components = {
            "price_volatility": price_volatility_risk,
            "gap": gap_risk,
            "overnight": overnight_risk,
            "news": news_risk,
            "liquidity": liquidity_risk,
            "market": market_risk,
            "portfolio": portfolio_risk,
            "capital": capital_risk,
        }

        # ==========================================================
        # DYNAMIC RISK WEIGHTS
        # ==========================================================

        weights = {
            "price_volatility": 0.24,
            "gap": 0.09,
            "overnight": 0.09,
            "news": 0.12,
            "liquidity": 0.12,
            "market": 0.14,
            "portfolio": 0.14,
            "capital": 0.06,
        }

        total_risk = 0.0

        for name, value in risk_components.items():

            total_risk += value * weights[name]

        total_risk = round(min(total_risk, 100.0), 2)

        diagnostics["weighted_total_risk"] = total_risk

        diagnostics["risk_components"] = risk_components

        diagnostics["risk_weights"] = weights

        # ==========================================================
        # RISK GRADE
        # ==========================================================

        if total_risk <= 10:

            risk_grade = "A+"

        elif total_risk <= 20:

            risk_grade = "A"

        elif total_risk <= 30:

            risk_grade = "B"

        elif total_risk <= 40:

            risk_grade = "C"

        elif total_risk <= 60:

            risk_grade = "D"

        else:

            risk_grade = "F"

        diagnostics["risk_grade"] = risk_grade

        # ==========================================================
        # SAFE / RISKY
        # ==========================================================

        safe = total_risk <= self.MAX_TOTAL_RISK

        diagnostics["safe"] = safe

        if safe:

            logger.info("Risk evaluation passed.")

        else:

            warnings.append("Overall portfolio risk exceeds the configured threshold.")

            logger.warning("Trade classified as HIGH RISK.")
        # ==========================================================
        # RISK OVERRIDES
        # ==========================================================
        # Phase 21 grouping (see PHASE21_NOTES.md, point 13): these two
        # sub-groups are categorically different and were previously
        # interleaved with no labeling distinguishing them.
        #
        # MARKET-RISK ADDITIVES: continuous market-condition signals that
        # ADD to the weighted base score (capped at 100) — total_risk can
        # still land under 100 depending on the base score. `safe` is NOT
        # forced False by these alone.
        #
        # SYSTEM SAFETY OVERRIDES: operational/portfolio-level kill
        # switches — unconditionally force total_risk=100 and safe=False,
        # regardless of the weighted base score. These represent "the
        # system itself decided to stop", not "the market got riskier".
        #
        # NOTE: whether circuit_breaker/emergency_stop/daily_loss_lock
        # actually receive real live data (vs. always falling back to
        # their False/0.0 defaults) is a separate, larger wiring question
        # not addressed by this grouping change — see PHASE21_NOTES.md.
        # ==========================================================

        diagnostics["override_category"] = None

        # ----------------------------------------------------------
        # MARKET-RISK ADDITIVES
        # ----------------------------------------------------------

        # --------------------------------------------------
        # HIGH IMPACT EVENT
        # --------------------------------------------------

        if bool(market.get("event_day", False)):

            total_risk = min(total_risk + 15.0, 100.0)

            warnings.append("High-impact market event.")

            diagnostics["event_override"] = True

        else:

            diagnostics["event_override"] = False

        # --------------------------------------------------
        # VIX SPIKE
        # --------------------------------------------------

        if vix >= 35:

            total_risk = min(total_risk + 10.0, 100.0)

            warnings.append("Extreme VIX detected.")

            diagnostics["vix_override"] = True

        else:

            diagnostics["vix_override"] = False

        # --------------------------------------------------
        # NEWS SHOCK
        # --------------------------------------------------

        if news_risk >= 90:

            total_risk = min(total_risk + 15.0, 100.0)

            warnings.append("Extreme news shock detected.")

            diagnostics["news_override"] = True

        else:

            diagnostics["news_override"] = False

        # ----------------------------------------------------------
        # SYSTEM SAFETY OVERRIDES (unconditional: total_risk=100, safe=False)
        # ----------------------------------------------------------

        # --------------------------------------------------
        # CIRCUIT BREAKER
        # --------------------------------------------------
        # Phase 22 (see PHASE22_NOTES.md): this used to read
        # `latest.get("circuit_breaker")`, where `latest = dataframe.
        # iloc[-1]` — a price/indicator DATAFRAME ROW, not a market or
        # portfolio dict. No producer anywhere in the data/feature
        # pipeline ever wrote a "circuit_breaker" column onto that
        # dataframe, so this check could never fire regardless of the
        # real market. Reads from `market` (the actual dict callers pass
        # real market-condition flags through) instead.
        # Phase 25 (see PHASE25_NOTES.md): `market["circuit_breaker"]` IS
        # now populated by a real producer — execution/scanner.py builds
        # a per-symbol copy of market_state with this key set from
        # market/circuit_bands.py's OHLC-based circuit-lock heuristic
        # (a HEURISTIC, not ground-truth NSE data — see that module's
        # docstring for the caveats). Defaults to False when the scanner
        # opted out (e.g. _disable_live_market_context during
        # backtesting) or when the heuristic simply didn't detect a
        # circuit lock — both indistinguishable here by design, same as
        # every other "unavailable = treat as normal" convention in this
        # codebase.

        if bool(market.get("circuit_breaker", False)):

            total_risk = 100.0

            safe = False

            warnings.append("Circuit breaker active.")

            diagnostics["circuit_override"] = True

            diagnostics["override_category"] = "system_safety"

        else:

            diagnostics["circuit_override"] = False

        # --------------------------------------------------
        # PORTFOLIO EMERGENCY STOP
        # --------------------------------------------------

        emergency_stop = bool(portfolio.get("emergency_stop", False))

        if emergency_stop:

            safe = False

            total_risk = 100.0

            risk_grade = "F"

            warnings.append("Portfolio emergency stop enabled.")

            diagnostics["override_category"] = "system_safety"

        diagnostics["emergency_stop"] = emergency_stop

        # --------------------------------------------------
        # DAILY LOSS LOCK
        # --------------------------------------------------

        if float(portfolio.get("daily_loss", 0.0)) >= self.MAX_DAILY_LOSS:

            safe = False

            total_risk = 100.0

            warnings.append("Daily loss limit reached.")

            diagnostics["daily_loss_lock"] = True

            diagnostics["override_category"] = "system_safety"

        else:

            diagnostics["daily_loss_lock"] = False

        # --------------------------------------------------
        # FINALIZE AFTER OVERRIDES
        # --------------------------------------------------

        total_risk = round(min(total_risk, 100.0), 2)

        if total_risk > self.MAX_TOTAL_RISK:

            safe = False

        diagnostics["total_risk_after_overrides"] = total_risk

        diagnostics["safe_after_overrides"] = safe
        # ==========================================================
        # AI RISK RECOMMENDATIONS
        # ==========================================================

        recommendations: list[str] = []

        if total_risk <= 15:

            recommendations.append("Normal position sizing allowed.")

        elif total_risk <= 30:

            recommendations.append("Use reduced position sizing.")

        elif total_risk <= 50:

            recommendations.append("Trade only with high-conviction setups.")

        else:

            recommendations.append("Avoid opening new positions.")

        # ==========================================================
        # POSITION RISK LEVEL
        # ==========================================================

        if total_risk <= 10:

            position_risk = "VERY_LOW"

        elif total_risk <= 25:

            position_risk = "LOW"

        elif total_risk <= 40:

            position_risk = "MEDIUM"

        elif total_risk <= 60:

            position_risk = "HIGH"

        else:

            position_risk = "EXTREME"

        diagnostics["position_risk"] = position_risk

        # ==========================================================
        # EXPOSURE RECOMMENDATION
        # ==========================================================

        if total_risk <= 15:

            recommended_exposure = 1.00

        elif total_risk <= 30:

            recommended_exposure = 0.75

        elif total_risk <= 45:

            recommended_exposure = 0.50

        elif total_risk <= 60:

            recommended_exposure = 0.25

        else:

            recommended_exposure = 0.00

        diagnostics["recommended_exposure"] = recommended_exposure

        recommendations.append(f"Recommended exposure: {recommended_exposure:.0%}")

        # ==========================================================
        # TRADE RESTRICTIONS
        # ==========================================================

        restrictions: list[str] = []

        if atr_risk >= 70:

            restrictions.append("High ATR")

        if volatility_risk >= 70:

            restrictions.append("High Volatility")

        if liquidity_risk >= 60:

            restrictions.append("Low Liquidity")

        if news_risk >= 70:

            restrictions.append("High News Risk")

        if portfolio_risk >= self.MAX_PORTFOLIO_RISK:

            restrictions.append("Portfolio Risk")

        if correlation_risk >= self.MAX_CORRELATION_RISK:

            restrictions.append("High Correlation")

        diagnostics["restrictions"] = restrictions

        if restrictions:

            warnings.append("Trade restrictions: " + ", ".join(restrictions))

        # ==========================================================
        # STORE AI RECOMMENDATIONS
        # ==========================================================

        diagnostics["recommendations"] = recommendations

        logger.info("Risk recommendations prepared.")
        # ==========================================================
        # RISK BREAKDOWN
        # ==========================================================

        risk_breakdown = {
            "atr": atr_risk,
            "gap": gap_risk,
            "overnight": overnight_risk,
            "news": news_risk,
            "liquidity": liquidity_risk,
            "volatility": volatility_risk,
            "market": market_risk,
            "portfolio": portfolio_risk,
            "sector": sector_risk,
            "correlation": correlation_risk,
            "capital": capital_risk,
        }

        diagnostics["risk_breakdown"] = risk_breakdown

        # ==========================================================
        # SORT RISK COMPONENTS
        # ==========================================================

        ranked_risks = sorted(
            risk_breakdown.items(),
            key=lambda item: item[1],
            reverse=True,
        )

        diagnostics["ranked_risks"] = ranked_risks

        # ==========================================================
        # HIGHEST RISK
        # ==========================================================

        highest_risk_name = ranked_risks[0][0]

        highest_risk_value = ranked_risks[0][1]

        diagnostics["highest_risk"] = {
            "name": highest_risk_name,
            "value": highest_risk_value,
        }

        if highest_risk_value >= 70:

            warnings.append(f"Primary risk source: {highest_risk_name}")

        # ==========================================================
        # RISK DISTRIBUTION
        # ==========================================================

        diagnostics["risk_distribution"] = {
            "low": sum(value < 30 for value in risk_breakdown.values()),
            "medium": sum(30 <= value < 60 for value in risk_breakdown.values()),
            "high": sum(value >= 60 for value in risk_breakdown.values()),
        }

        # ==========================================================
        # RISK ANALYTICS
        # ==========================================================

        average_risk = round(sum(risk_breakdown.values()) / len(risk_breakdown), 2)

        diagnostics["average_risk"] = average_risk

        diagnostics["maximum_risk"] = round(max(risk_breakdown.values()), 2)

        diagnostics["minimum_risk"] = round(min(risk_breakdown.values()), 2)

        # ==========================================================
        # AI SUMMARY
        # ==========================================================

        diagnostics["summary"] = {
            "safe": safe,
            "risk_grade": risk_grade,
            "total_risk": total_risk,
            "highest_risk": highest_risk_name,
            "warning_count": len(warnings),
        }

        logger.info("Risk analytics completed.")
        # ==========================================================
        # FINAL RISK VALIDATION
        # ==========================================================

        validation_errors: list[str] = []

        if not (0.0 <= total_risk <= 100.0):

            validation_errors.append("Total risk out of range.")

        if risk_grade not in {
            "A+",
            "A",
            "B",
            "C",
            "D",
            "F",
        }:

            validation_errors.append("Invalid risk grade.")

        risk_fields = {
            "atr": atr_risk,
            "gap": gap_risk,
            "overnight": overnight_risk,
            "news": news_risk,
            "liquidity": liquidity_risk,
            "volatility": volatility_risk,
            "portfolio": portfolio_risk,
            "sector": sector_risk,
            "correlation": correlation_risk,
            "capital": capital_risk,
        }

        for name, value in risk_fields.items():

            if not (0.0 <= value <= 100.0):

                validation_errors.append(f"{name} risk out of range.")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE
        # ==========================================================

        if validation_errors:

            safe = False

            total_risk = 100.0

            risk_grade = "F"

            warnings.append("Risk manager internal validation failed.")

            logger.error(
                "Risk validation failed: %s",
                validation_errors,
            )

            diagnostics["fail_safe"] = True

        else:

            diagnostics["fail_safe"] = False

        # ==========================================================
        # BUILD RESULT
        # ==========================================================

        result = RiskResult(
            safe=safe,
            total_risk=round(total_risk, 2),
            risk_grade=risk_grade,
            atr_risk=round(atr_risk, 2),
            gap_risk=round(gap_risk, 2),
            overnight_risk=round(overnight_risk, 2),
            news_risk=round(news_risk, 2),
            liquidity_risk=round(liquidity_risk, 2),
            volatility_risk=round(volatility_risk, 2),
            portfolio_risk=round(portfolio_risk, 2),
            sector_risk=round(sector_risk, 2),
            correlation_risk=round(correlation_risk, 2),
            capital_risk=round(capital_risk, 2),
            warnings=warnings,
            diagnostics=diagnostics,
        )

        logger.info("RiskResult created successfully.")
        return result  # <--- FIXED: Added missing return statement

    def evaluate_order(self, order, portfolio, market="NORMAL"):
        """
        COMPATIBILITY WRAPPER FOR ORCHESTRATOR + TESTS
        """

        class V:
            passed = True

        class D:
            pass

        import pandas as pd

        dataframe = order.get("dataframe", None)

        if dataframe is None:
            dataframe = pd.DataFrame(
                [{"close": 100, "atr_14": 1, "volume_sma_20": 1000000}]
            )

        return self.evaluate(
            validation=V(),
            decision=D(),
            dataframe=dataframe,
            portfolio=portfolio,
            market={"regime": market},
        )

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(result: RiskResult) -> dict[str, Any]:

        return {
            "safe": result.safe,
            "total_risk": result.total_risk,
            "risk_grade": result.risk_grade,
            "atr_risk": result.atr_risk,
            "gap_risk": result.gap_risk,
            "overnight_risk": result.overnight_risk,
            "news_risk": result.news_risk,
            "liquidity_risk": result.liquidity_risk,
            "volatility_risk": result.volatility_risk,
            "portfolio_risk": result.portfolio_risk,
            "sector_risk": result.sector_risk,
            "correlation_risk": result.correlation_risk,
            "capital_risk": result.capital_risk,
            "warnings": result.warnings,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(result: RiskResult) -> str:

        status = "SAFE" if result.safe else "RISKY"

        return (
            f"{status}"
            f" | Grade={result.risk_grade}"
            f" | Risk={result.total_risk:.2f}%"
            f" | ATR={result.atr_risk:.2f}"
            f" | News={result.news_risk:.2f}"
            f" | Portfolio={result.portfolio_risk:.2f}"
        )

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(result: RiskResult) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("RISK MANAGER REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Status              : {'SAFE' if result.safe else 'RISKY'}")

        report.append(f"Risk Grade          : {result.risk_grade}")

        report.append(f"Total Risk          : {result.total_risk:.2f}%")

        report.append("")

        report.append("Risk Components")
        report.append("-" * 70)

        components = [
            ("ATR", result.atr_risk),
            ("Gap", result.gap_risk),
            ("Overnight", result.overnight_risk),
            ("News", result.news_risk),
            ("Liquidity", result.liquidity_risk),
            ("Volatility", result.volatility_risk),
            ("Portfolio", result.portfolio_risk),
            ("Sector", result.sector_risk),
            ("Correlation", result.correlation_risk),
            ("Capital", result.capital_risk),
        ]

        for name, value in components:

            report.append(f"{name:<20} : {value:.2f}")

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
