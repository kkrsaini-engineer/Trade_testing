"""
BUY Strategy Engine

Institutional Version

Responsibilities
----------------

Trend Analysis
Momentum Analysis
Volume Analysis
Breakout Analysis
Pullback Analysis
Ichimoku Analysis
Market Filter
Sector Filter
Fundamental Filter
News Filter

Produces

BUY

or

NO_TRADE

No scoring.

No probability.

No position sizing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.constants import BUY
from core.constants import NO_TRADE

from core.logger import get_logger
from core.exceptions import StrategyError
from strategy.fundamental_scoring import buy_fundamental_score
from news.news_bias import news_component

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class BuyDecision:

    action: str

    passed: bool

    confidence: float

    reasons: list[str] = field(default_factory=list)

    technical_checks: dict[str, bool] = field(default_factory=dict)

    # Explainability — full tier breakdown, so every decision (including
    # NO_TRADE) can be audited without re-simulating the pipeline.
    tier1_checks: dict[str, bool] = field(default_factory=dict)
    tier1_passed: bool = False
    tier2_score: float = 0.0
    tier3_score: float = 0.0
    fundamental_health: float = 0.0
    news_health: float | None = None
    overall_score: float = 0.0
    qualify_threshold: float = 0.0


# ==========================================================
# ENGINE
# ==========================================================


class BuyStrategyEngine:
    """
    Complete BUY Strategy
    """

    REQUIRED_COLUMNS = {
        "close",
        "ema_20",
        "ema_50",
        "ema_200",
        "sma_20",
        "sma_50",
        "sma_200",
        "rsi_14",
        "macd",
        "macd_signal",
        "adx_14",
        "supertrend",
        "cloud_trend",
        "vwap",
        "volume",
        "volume_sma_20",
        "obv",
        "cmf_20",
        "market_regime",
        "volatility_state",
        "is_breakout",
        "is_pullback",
    }

    def evaluate(
        self,
        dataframe: pd.DataFrame,
        fundamentals: dict[str, Any],
        news_score: float | None,
        market_score: float,
        sector_score: float,
    ) -> BuyDecision:

        if dataframe.empty:
            raise StrategyError("Empty dataframe.")

        missing = self.REQUIRED_COLUMNS.difference(dataframe.columns)

        if missing:

            raise StrategyError(f"Missing columns: {sorted(missing)}")

        row = dataframe.iloc[-1]

        checks = {}

        reasons = []
        # ==========================================================
        # TREND ENGINE
        # ==========================================================

        # --------------------------------------------------
        # EMA TREND
        # --------------------------------------------------

        checks["ema_alignment"] = row["ema_20"] > row["ema_50"] > row["ema_200"]

        if checks["ema_alignment"]:

            reasons.append("Bullish EMA alignment.")

        # --------------------------------------------------
        # SMA TREND
        # --------------------------------------------------

        checks["sma_alignment"] = row["sma_20"] > row["sma_50"] > row["sma_200"]

        if checks["sma_alignment"]:

            reasons.append("Bullish SMA alignment.")

        # --------------------------------------------------
        # PRICE ABOVE EMA20
        # --------------------------------------------------

        checks["price_above_ema20"] = row["close"] > row["ema_20"]

        if checks["price_above_ema20"]:

            reasons.append("Price above EMA20.")

        # --------------------------------------------------
        # PRICE ABOVE VWAP
        # --------------------------------------------------

        checks["price_above_vwap"] = row["close"] > row["vwap"]

        if checks["price_above_vwap"]:

            reasons.append("Price above VWAP.")

        # --------------------------------------------------
        # SUPERTREND
        # --------------------------------------------------

        checks["supertrend"] = bool(row["supertrend"])

        if checks["supertrend"]:

            reasons.append("Supertrend is bullish.")

        # --------------------------------------------------
        # ICHIMOKU CLOUD
        # --------------------------------------------------

        checks["ichimoku"] = row["cloud_trend"] == "BULL"

        if checks["ichimoku"]:

            reasons.append("Price above Ichimoku Cloud.")

        # --------------------------------------------------
        # LONG TERM MARKET TREND
        # --------------------------------------------------

        checks["market_trend"] = row["market_regime"] == "BULL"

        if checks["market_trend"]:

            reasons.append("Bullish market regime.")
        # ==========================================================
        # MOMENTUM ENGINE
        # ==========================================================

        # --------------------------------------------------
        # RSI
        # --------------------------------------------------

        rsi = float(row["rsi_14"])

        checks["rsi"] = 55 <= rsi <= 70

        if checks["rsi"]:

            reasons.append("Healthy RSI momentum.")

        # --------------------------------------------------
        # MACD
        # --------------------------------------------------

        checks["macd_cross"] = row["macd"] > row["macd_signal"]

        if checks["macd_cross"]:

            reasons.append("Bullish MACD crossover.")

        # --------------------------------------------------
        # MACD HISTOGRAM
        # --------------------------------------------------

        checks["macd_histogram"] = row["macd_histogram"] > 0

        if checks["macd_histogram"]:

            reasons.append("Positive MACD histogram.")

        # --------------------------------------------------
        # ADX
        # --------------------------------------------------

        checks["adx"] = row["adx_14"] >= 25

        if checks["adx"]:

            reasons.append("Strong trend (ADX > 25).")

        # --------------------------------------------------
        # ROC
        # --------------------------------------------------

        checks["roc"] = row["roc_12"] > 0

        if checks["roc"]:

            reasons.append("Positive Rate of Change.")

        # --------------------------------------------------
        # CCI
        # --------------------------------------------------

        checks["cci"] = row["cci_20"] > 100

        if checks["cci"]:

            reasons.append("CCI indicates bullish momentum.")

        # --------------------------------------------------
        # STOCHASTIC
        # --------------------------------------------------

        checks["stochastic"] = row["stoch_k"] > row["stoch_d"]

        if checks["stochastic"]:

            reasons.append("Bullish stochastic crossover.")

        # --------------------------------------------------
        # WILLIAMS %R
        # --------------------------------------------------

        checks["williams_r"] = row["williams_r_14"] > -50

        if checks["williams_r"]:

            reasons.append("Williams %R supports momentum.")
        # ==========================================================
        # VOLUME ENGINE
        # ==========================================================

        # --------------------------------------------------
        # VOLUME ABOVE AVERAGE
        # --------------------------------------------------

        checks["volume"] = row["volume"] > row["volume_sma_20"]

        if checks["volume"]:

            reasons.append("Volume above 20-day average.")

        # --------------------------------------------------
        # VOLUME SPIKE
        # --------------------------------------------------

        volume_ratio = row["volume"] / max(
            row["volume_sma_20"],
            1,
        )

        checks["volume_spike"] = volume_ratio >= 1.50

        if checks["volume_spike"]:

            reasons.append("Strong institutional volume detected.")

        # --------------------------------------------------
        # OBV
        # --------------------------------------------------

        checks["obv"] = row["obv"] > 0

        if checks["obv"]:

            reasons.append("On Balance Volume is positive.")

        # --------------------------------------------------
        # CMF
        # --------------------------------------------------

        checks["cmf"] = row["cmf_20"] > 0

        if checks["cmf"]:

            reasons.append("Positive Chaikin Money Flow.")

        # --------------------------------------------------
        # MONEY FLOW INDEX
        # --------------------------------------------------

        checks["mfi"] = 50 <= row["mfi_14"] <= 80

        if checks["mfi"]:

            reasons.append("Money Flow Index supports accumulation.")

        # NOTE: a "VWAP confirmation" check used to live here, but it was
        # `row["close"] > row["vwap"]` — byte-identical to "price_above_vwap"
        # above. Counting the same condition twice inflated tier2_score
        # without adding independent signal, so it was removed rather than
        # kept as a duplicate vote.

        # --------------------------------------------------
        # ACCUMULATION
        # --------------------------------------------------

        checks["accumulation"] = checks["obv"] and checks["cmf"]

        if checks["accumulation"]:

            reasons.append("Volume indicates accumulation.")
        # ==========================================================
        # BREAKOUT ENGINE
        # ==========================================================

        # --------------------------------------------------
        # BREAKOUT
        # --------------------------------------------------

        checks["breakout"] = bool(row["is_breakout"])

        if checks["breakout"]:

            reasons.append("20-period breakout detected.")

        # --------------------------------------------------
        # PULLBACK
        # --------------------------------------------------

        checks["pullback"] = bool(row["is_pullback"])

        if checks["pullback"]:

            reasons.append("Healthy pullback confirmation.")

        # --------------------------------------------------
        # GAP
        # --------------------------------------------------

        checks["gap_filter"] = not bool(row["gap_down"])

        if checks["gap_filter"]:

            reasons.append("No bearish opening gap.")

        # --------------------------------------------------
        # PIVOT
        # --------------------------------------------------

        checks["pivot"] = row["close"] > row["pivot"]

        if checks["pivot"]:

            reasons.append("Price above pivot.")

        # --------------------------------------------------
        # BOLLINGER
        # --------------------------------------------------

        checks["bollinger"] = row["close"] > row["bb_middle"]

        if checks["bollinger"]:

            reasons.append("Above Bollinger middle band.")

        # --------------------------------------------------
        # KELTNER
        # --------------------------------------------------

        checks["keltner"] = row["close"] > row["kc_middle"]

        if checks["keltner"]:

            reasons.append("Above Keltner middle channel.")

        # --------------------------------------------------
        # DONCHIAN
        # --------------------------------------------------

        checks["donchian"] = row["close"] > row["dc_middle"]

        if checks["donchian"]:

            reasons.append("Above Donchian midpoint.")

        # --------------------------------------------------
        # ATR FILTER
        # --------------------------------------------------

        checks["atr_filter"] = row["atr_14"] > 0 and row["atr_14"] < (
            row["close"] * 0.05
        )

        if checks["atr_filter"]:

            reasons.append("ATR within acceptable range.")

        # --------------------------------------------------
        # BREAKOUT CONFIRMATION
        # --------------------------------------------------

        checks["confirmed_breakout"] = (
            checks["breakout"] and checks["volume"] and checks["volume_spike"]
        )

        if checks["confirmed_breakout"]:

            reasons.append("High conviction breakout confirmed by volume.")
        # ==========================================================
        # MARKET FILTER
        # ==========================================================

        # NOTE: a "bull_market" tier2 check used to live here, but it was
        # `row["market_regime"] == "BULL"` — byte-identical to the
        # "market_trend" Tier 1 check below. Counting the same regime
        # condition as both a Tier 1 gate AND a Tier 2 vote double-counted
        # one signal as if it were two independent confirmations, so the
        # Tier 2 copy was removed.

        # --------------------------------------------------
        # VOLATILITY
        # --------------------------------------------------

        checks["volatility"] = row["volatility_state"] != "HIGH"

        if checks["volatility"]:

            reasons.append("Market volatility acceptable.")

        # --------------------------------------------------
        # GAP DAY FILTER
        # --------------------------------------------------

        checks["gap_day"] = not bool(row.get("gap_day", False))

        if checks["gap_day"]:

            reasons.append("No abnormal market gap.")

        # --------------------------------------------------
        # MARKET BREADTH
        # --------------------------------------------------

        checks["breadth"] = (
            row.get(
                "breadth",
                "NEUTRAL",
            )
            == "STRONG"
        )

        if checks["breadth"]:

            reasons.append("Market breadth is strong.")

        # ==========================================================
        # SECTOR FILTER
        # ==========================================================

        checks["sector"] = sector_score >= 70

        if checks["sector"]:

            reasons.append("Strong sector momentum.")

        # ==========================================================
        # FUNDAMENTAL FILTER (weighted, never all-or-nothing)
        # ==========================================================

        fundamental_health = buy_fundamental_score(fundamentals)

        checks["fundamental"] = fundamental_health >= 55

        if checks["fundamental"]:

            reasons.append(f"Fundamentals healthy ({fundamental_health:.0f}/100).")

        # ==========================================================
        # NEWS FILTER (bidirectional — NO news is neutral, never a fail)
        # ==========================================================

        has_news = news_score is not None
        news_health = news_component(news_score) if has_news else None

        checks["news"] = bool(has_news and news_health >= 55)

        if checks["news"]:

            reasons.append(f"News sentiment favorable ({news_health:.0f}/100).")

        elif has_news and news_health <= 45:

            reasons.append(f"News sentiment unfavorable ({news_health:.0f}/100) — weighed against BUY.")

        else:

            reasons.append("No news available — decision based on technicals + fundamentals only.")

        # ==========================================================
        # MARKET SCORE FILTER
        # ==========================================================

        checks["market_score"] = market_score >= 60

        if checks["market_score"]:

            reasons.append("Overall market score is supportive.")
        # ==========================================================
        # DECISION ENGINE — TIERED, WEIGHTED (not one giant AND condition)
        # ==========================================================

        # --------------------------------------------------
        # TIER 1 — CORE TREND (majority must agree, not unanimous)
        # A stock rarely has perfect EMA stacking + regime + SMA stacking
        # all at once; requiring 2-of-3 still enforces "there IS a trend"
        # without being an all-or-nothing gate.
        # --------------------------------------------------

        tier1_checks = {
            "ema_alignment": checks["ema_alignment"],
            "market_trend": checks["market_trend"],
            "sma_alignment": checks["sma_alignment"],
        }
        tier1_passed = sum(tier1_checks.values()) >= 2

        # --------------------------------------------------
        # TIER 2 — TECHNICAL CONFIRMATION (weighted contribution, not a gate)
        # --------------------------------------------------

        tier1_and_context_keys = {
            "ema_alignment", "market_trend", "sma_alignment",
            "fundamental", "news", "market_score",
        }
        tier2_checks = {
            key: value for key, value in checks.items()
            if key not in tier1_and_context_keys
        }
        tier2_score = (
            sum(tier2_checks.values()) / max(len(tier2_checks), 1)
        ) * 100

        # --------------------------------------------------
        # TIER 3 — CONTEXT (fundamentals + news + market, weighted)
        # When there is genuinely no news/macro signal for this stock,
        # news gets ZERO weight here (not a neutral-50 dilution) — its
        # weight is redistributed to fundamentals + market so a real
        # fundamental signal isn't watered down by an absent one.
        # --------------------------------------------------

        if has_news:
            tier3_score = (
                fundamental_health * 0.55
                + news_health * 0.30
                + min(max(market_score, 0.0), 100.0) * 0.15
            )
        else:
            tier3_score = (
                fundamental_health * (0.55 / 0.70)
                + min(max(market_score, 0.0), 100.0) * (0.15 / 0.70)
            )

        # --------------------------------------------------
        # OVERALL WEIGHTED SCORE
        # --------------------------------------------------

        overall_score = (tier2_score * 0.45) + (tier3_score * 0.55)

        # Kept for backward-compatible diagnostics/reporting.
        optional_checks = tier2_checks
        optional_passed = sum(optional_checks.values())
        optional_total = len(optional_checks)

        # --------------------------------------------------
        # FINAL QUALIFICATION
        # --------------------------------------------------

        QUALIFY_THRESHOLD = 58.0

        qualified = tier1_passed and overall_score >= QUALIFY_THRESHOLD

        # --------------------------------------------------
        # CONFIDENCE
        # --------------------------------------------------

        confidence = round(overall_score, 2)

        # --------------------------------------------------
        # REASONS
        # --------------------------------------------------

        if tier1_passed:

            reasons.append("Core trend confirmed (Tier 1).")

        else:

            failed = [key for key, value in tier1_checks.items() if not value]

            reasons.append("Trend not confirmed: " + ", ".join(failed))

        reasons.append(f"Technical confirmation: {optional_passed}/{optional_total}")

        reasons.append(f"Weighted score: {overall_score:.2f}/100 (need >= {QUALIFY_THRESHOLD:.0f})")

        reasons.append(f"Confidence: {confidence:.2f}%")

        # --------------------------------------------------
        # ACTION
        # --------------------------------------------------

        action = BUY if qualified else NO_TRADE
        # ==========================================================
        # DIAGNOSTICS
        # ==========================================================

        passed_checks = sum(bool(value) for value in checks.values())

        failed_checks = len(checks) - passed_checks

        reasons.append(f"Checks Passed : {passed_checks}")

        reasons.append(f"Checks Failed : {failed_checks}")

        logger.info(
            "BUY Strategy | Action=%s | Confidence=%.2f | Passed=%d/%d",
            action,
            confidence,
            passed_checks,
            len(checks),
        )

        # ==========================================================
        # RETURN
        # ==========================================================

        return BuyDecision(
            action=action,
            passed=qualified,
            confidence=confidence,
            reasons=reasons,
            technical_checks=checks,
            tier1_checks=tier1_checks,
            tier1_passed=tier1_passed,
            tier2_score=round(tier2_score, 2),
            tier3_score=round(tier3_score, 2),
            fundamental_health=round(fundamental_health, 2),
            news_health=round(news_health, 2) if has_news else None,
            overall_score=round(overall_score, 2),
            qualify_threshold=QUALIFY_THRESHOLD,
        )

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def to_dict(
        decision: BuyDecision,
    ) -> dict[str, Any]:

        return {
            "action": decision.action,
            "passed": decision.passed,
            "confidence": decision.confidence,
            "reasons": decision.reasons,
            "technical_checks": decision.technical_checks,
        }

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        decision: BuyDecision,
    ) -> str:

        report = []

        report.append("========== BUY STRATEGY ==========")

        report.append(f"ACTION : {decision.action}")

        report.append(f"PASSED : {decision.passed}")

        report.append(f"CONFIDENCE : {decision.confidence:.2f}%")

        report.append("")

        report.append("Reasons:")

        for reason in decision.reasons:

            report.append(f" • {reason}")

        report.append("")

        report.append("Checks:")

        for key, value in sorted(decision.technical_checks.items()):

            status = "PASS" if value else "FAIL"

            report.append(f"{key:<30} {status}")

        return "\n".join(report)


# ==========================================================
# END OF FILE
# ==========================================================
