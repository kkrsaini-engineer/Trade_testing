"""
SELL Strategy Engine

Institutional Version

Responsibilities
----------------

Trend Analysis
Momentum Analysis
Volume Analysis
Breakdown Analysis
Failed Breakout Analysis
Ichimoku Analysis
Market Filter
Sector Filter
Fundamental Filter
News Filter

Produces

SELL

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

from core.constants import SELL
from core.constants import NO_TRADE

from core.logger import get_logger
from core.exceptions import StrategyError
from strategy.fundamental_scoring import sell_fundamental_score
from news.news_bias import news_component

logger = get_logger(__name__)


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class SellDecision:

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
    fundamental_weakness: float = 0.0
    news_negativity: float | None = None
    overall_score: float = 0.0
    qualify_threshold: float = 0.0


# ==========================================================
# ENGINE
# ==========================================================


class SellStrategyEngine:
    """
    Complete SELL Strategy
    """

    REQUIRED_COLUMNS = {
        "close",
        "open",
        "high",
        "ema_20",
        "ema_50",
        "ema_200",
        "sma_20",
        "sma_50",
        "sma_200",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_histogram",
        "adx_14",
        "supertrend",
        "cloud_trend",
        "vwap",
        "volume",
        "volume_sma_20",
        "obv",
        "cmf_20",
        "mfi_14",
        "roc_12",
        "cci_20",
        "stochastic_k",
        "stochastic_d",
        "williams_r_14",
        "market_regime",
        "volatility_state",
        "breadth",
        "atr_14",
        "bb_middle",
        "kc_middle",
        "dc_middle",
        "pivot",
        "gap_up",
        "gap_down",
        "gap_day",
        "is_breakdown",
        "is_pullback",
        "bb_lower",
    }

    def evaluate(
        self,
        dataframe: pd.DataFrame,
        fundamentals: dict[str, Any],
        news_score: float | None,
        market_score: float,
        sector_score: float,
    ) -> SellDecision:

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

        checks["ema_alignment"] = row["ema_20"] < row["ema_50"] < row["ema_200"]

        if checks["ema_alignment"]:

            reasons.append("Bearish EMA alignment.")

        # --------------------------------------------------
        # SMA TREND
        # --------------------------------------------------

        checks["sma_alignment"] = row["sma_20"] < row["sma_50"] < row["sma_200"]

        if checks["sma_alignment"]:

            reasons.append("Bearish SMA alignment.")

        # --------------------------------------------------
        # PRICE BELOW EMA20
        # --------------------------------------------------

        checks["price_below_ema20"] = row["close"] < row["ema_20"]

        if checks["price_below_ema20"]:

            reasons.append("Price below EMA20.")

        # --------------------------------------------------
        # PRICE BELOW VWAP
        # --------------------------------------------------

        checks["price_below_vwap"] = row["close"] < row["vwap"]

        if checks["price_below_vwap"]:

            reasons.append("Price below VWAP.")

        # --------------------------------------------------
        # SUPERTREND
        # --------------------------------------------------

        checks["supertrend"] = not bool(row["supertrend"])

        if checks["supertrend"]:

            reasons.append("Supertrend is bearish.")

        # --------------------------------------------------
        # ICHIMOKU CLOUD
        # --------------------------------------------------

        checks["ichimoku"] = row["cloud_trend"] == "BEAR"

        if checks["ichimoku"]:

            reasons.append("Price below Ichimoku Cloud.")

        # --------------------------------------------------
        # LONG TERM MARKET TREND
        # --------------------------------------------------

        checks["market_trend"] = row["market_regime"] == "BEAR"

        if checks["market_trend"]:

            reasons.append("Bearish market regime.")
        # ==========================================================
        # MOMENTUM ENGINE
        # ==========================================================

        # --------------------------------------------------
        # RSI
        # --------------------------------------------------

        rsi = float(row["rsi_14"])

        checks["rsi"] = 30 <= rsi <= 45

        if checks["rsi"]:

            reasons.append("Weak RSI momentum.")

        # --------------------------------------------------
        # MACD
        # --------------------------------------------------

        checks["macd_cross"] = row["macd"] < row["macd_signal"]

        if checks["macd_cross"]:

            reasons.append("Bearish MACD crossover.")

        # NOTE: a "macd_histogram < 0" check used to live here — removed
        # for the same reason as the BUY-side mirror (see
        # strategy/buy_strategy.py): macd_histogram = macd - macd_signal,
        # so "< 0" is mathematically identical to "macd_cross" above.

        # --------------------------------------------------
        # ADX
        # --------------------------------------------------

        checks["adx"] = row["adx_14"] >= 25

        if checks["adx"]:

            reasons.append("Strong bearish trend.")

        # --------------------------------------------------
        # ROC
        # --------------------------------------------------

        checks["roc"] = row["roc_12"] < 0

        if checks["roc"]:

            reasons.append("Negative Rate of Change.")

        # --------------------------------------------------
        # CCI
        # --------------------------------------------------

        checks["cci"] = row["cci_20"] < -100

        if checks["cci"]:

            reasons.append("CCI indicates bearish momentum.")

        # --------------------------------------------------
        # STOCHASTIC
        # --------------------------------------------------

        checks["stochastic"] = row["stochastic_k"] < row["stochastic_d"]

        if checks["stochastic"]:

            reasons.append("Bearish stochastic crossover.")

        # --------------------------------------------------
        # WILLIAMS %R
        # --------------------------------------------------

        checks["williams_r"] = row["williams_r_14"] < -50

        if checks["williams_r"]:

            reasons.append("Williams %R confirms weakness.")
        # ==========================================================
        # VOLUME ENGINE
        # ==========================================================

        # --------------------------------------------------
        # VOLUME ABOVE AVERAGE
        # --------------------------------------------------

        checks["volume"] = row["volume"] > row["volume_sma_20"]

        if checks["volume"]:

            reasons.append("Selling volume above 20-day average.")

        # --------------------------------------------------
        # VOLUME SPIKE
        # --------------------------------------------------

        volume_ratio = row["volume"] / max(
            row["volume_sma_20"],
            1,
        )

        checks["volume_spike"] = volume_ratio >= 1.50

        if checks["volume_spike"]:

            reasons.append("Heavy institutional selling volume detected.")

        # --------------------------------------------------
        # OBV
        # --------------------------------------------------

        checks["obv"] = row["obv"] < 0

        if checks["obv"]:

            reasons.append("On Balance Volume is negative.")

        # --------------------------------------------------
        # CMF
        # --------------------------------------------------

        checks["cmf"] = row["cmf_20"] < 0

        if checks["cmf"]:

            reasons.append("Negative Chaikin Money Flow.")

        # --------------------------------------------------
        # MONEY FLOW INDEX
        # --------------------------------------------------

        checks["mfi"] = 20 <= row["mfi_14"] <= 50

        if checks["mfi"]:

            reasons.append("Money Flow Index indicates distribution.")

        # NOTE: a "VWAP confirmation" check used to live here, but it was
        # `row["close"] < row["vwap"]` — byte-identical to
        # "price_below_vwap" above. Removed for the same reason as the
        # BUY-side mirror (see strategy/buy_strategy.py).

        # --------------------------------------------------
        # DISTRIBUTION
        # --------------------------------------------------

        checks["distribution"] = checks["obv"] and checks["cmf"]

        if checks["distribution"]:

            reasons.append("Volume profile indicates institutional distribution.")
        # ==========================================================
        # BREAKDOWN ENGINE
        # ==========================================================

        # --------------------------------------------------
        # BREAKDOWN
        # --------------------------------------------------

        checks["breakdown"] = bool(row["is_breakdown"])

        if checks["breakdown"]:

            reasons.append("20-period breakdown detected.")

        # --------------------------------------------------
        # FAILED BREAKOUT
        # --------------------------------------------------

        checks["failed_breakout"] = bool(
            row.get(
                "failed_breakout",
                False,
            )
        )

        if checks["failed_breakout"]:

            reasons.append("Failed breakout confirmed.")

        # --------------------------------------------------
        # GAP FILTER
        # --------------------------------------------------

        checks["gap_filter"] = not bool(row["gap_up"])

        if checks["gap_filter"]:

            reasons.append("No bullish opening gap.")

        # --------------------------------------------------
        # PIVOT
        # --------------------------------------------------

        checks["pivot"] = row["close"] < row["pivot"]

        if checks["pivot"]:

            reasons.append("Price below pivot.")

        # --------------------------------------------------
        # BOLLINGER
        # --------------------------------------------------

        checks["bollinger"] = row["close"] < row["bb_middle"]

        if checks["bollinger"]:

            reasons.append("Below Bollinger middle band.")

        # NOTE: a "keltner" check used to live here (close < kc_middle) —
        # removed for the same reason as the BUY-side mirror (see
        # strategy/buy_strategy.py): kc_middle = ema_20, byte-identical
        # to "price_below_ema20" above.

        # --------------------------------------------------
        # DONCHIAN
        # --------------------------------------------------

        checks["donchian"] = row["close"] < row["dc_middle"]

        if checks["donchian"]:

            reasons.append("Below Donchian midpoint.")

        # --------------------------------------------------
        # ATR FILTER
        # --------------------------------------------------

        checks["atr_filter"] = row["atr_14"] > 0 and row["atr_14"] < (
            row["close"] * 0.05
        )

        if checks["atr_filter"]:

            reasons.append("ATR within acceptable range.")

        # --------------------------------------------------
        # BREAKDOWN CONFIRMATION
        # --------------------------------------------------

        checks["confirmed_breakdown"] = (
            checks["breakdown"] and checks["volume"] and checks["volume_spike"]
        )

        if checks["confirmed_breakdown"]:

            reasons.append("High conviction breakdown confirmed by volume.")

        # ==========================================================
        # EARLY-ENTRY ENGINE (mirror of strategy/buy_strategy.py's —
        # same reasoning, bearish direction: catch a breakdown near its
        # START instead of after price has already fallen a long way)
        # ==========================================================

        # --------------------------------------------------
        # FRESH EMA CROSS (crossed bearish within the last few bars,
        # not "has been stacked bearish for weeks")
        # --------------------------------------------------

        EMA_CROSS_LOOKBACK = 3

        lookback_n = min(len(dataframe), EMA_CROSS_LOOKBACK + 1)
        recent = dataframe.iloc[-lookback_n:]

        ema_fresh_cross = False

        for i in range(1, len(recent)):

            prev_above = recent["ema_20"].iloc[i - 1] >= recent["ema_50"].iloc[i - 1]

            now_below = recent["ema_20"].iloc[i] < recent["ema_50"].iloc[i]

            if prev_above and now_below:

                ema_fresh_cross = True

                break

        checks["ema_fresh_cross"] = ema_fresh_cross

        if checks["ema_fresh_cross"]:

            reasons.append("Fresh bearish EMA20/EMA50 cross (early breakdown, not chasing).")

        # --------------------------------------------------
        # RSI FRESH MIDLINE CROSS (just crossed below 50, not
        # "already sitting" deeply oversold in 30-45 for a while)
        # --------------------------------------------------

        RSI_CROSS_LOOKBACK = 2

        recent_rsi = dataframe["rsi_14"].iloc[-min(len(dataframe), RSI_CROSS_LOOKBACK):]

        rsi_fresh_cross = (
            len(recent_rsi) >= 2
            and recent_rsi.iloc[-2] >= 50 > recent_rsi.iloc[-1]
            and recent_rsi.iloc[-1] >= 35
        )

        checks["rsi_fresh_cross"] = bool(rsi_fresh_cross)

        if checks["rsi_fresh_cross"]:

            reasons.append("RSI freshly crossed below the 50 momentum midline.")

        # --------------------------------------------------
        # SQUEEZE BREAKDOWN (breakdown FROM a prior low-volatility
        # consolidation, not a decline that's already run far)
        # --------------------------------------------------

        SQUEEZE_LOOKBACK = 5

        SQUEEZE_BB_WIDTH_THRESHOLD = 0.04

        squeeze_n = min(len(dataframe), SQUEEZE_LOOKBACK + 1)

        prior_bb_width = dataframe["bb_width"].iloc[-squeeze_n:-1] if squeeze_n > 1 else dataframe["bb_width"].iloc[0:0]

        was_squeezed = bool((prior_bb_width < SQUEEZE_BB_WIDTH_THRESHOLD).all()) if len(prior_bb_width) > 0 else False

        # Breakdown trigger is the actual Bollinger lower band (close <
        # bb_lower), not the existing is_breakdown flag (a 20-day-low
        # Donchian breakdown — a different, unrelated definition). Mirror
        # of the BUY side's same fix (see strategy/buy_strategy.py).
        checks["squeeze_breakout"] = (
            was_squeezed and row["close"] < row["bb_lower"] and checks["volume_spike"]
        )

        if checks["squeeze_breakout"]:

            reasons.append("Breakdown below the Bollinger lower band from a prior low-volatility squeeze.")

        # --------------------------------------------------
        # RELIEF-RALLY REJECTION (shorted a failed bounce within an
        # established downtrend, instead of a fresh low) — price was
        # WITHIN 1.5% of EMA20 yesterday (relief-rallied up to
        # resistance) and today closes back below EMA20 on a bearish
        # rejection candle (close < open), mirroring the BUY side's
        # pullback_entry exactly (see strategy/buy_strategy.py).
        # --------------------------------------------------

        PULLBACK_PROXIMITY_PERCENT = 1.5

        prev_row = dataframe.iloc[-2] if len(dataframe) >= 2 else row
        prev_ema20 = float(prev_row["ema_20"])
        prev_proximity_percent = (
            abs(float(prev_row["close"]) - prev_ema20) / prev_ema20 * 100 if prev_ema20 else 100.0
        )

        checks["pullback_entry"] = bool(
            row["close"] < row["ema_200"]
            and prev_proximity_percent <= PULLBACK_PROXIMITY_PERCENT
            and row["close"] < row["ema_20"]
            and row["close"] < row["open"]
        )

        if checks["pullback_entry"]:

            reasons.append("Relief-rally to EMA20 resistance (within 1.5%) followed by a bearish rejection candle.")

        # --------------------------------------------------
        # OVEREXTENSION CAP (HARD REJECT below, not just a vote) —
        # price already too far below EMA20 means the easy part of
        # the decline is over, and snap-back/short-squeeze risk is
        # elevated; treat it as a chase-risk filter.
        # --------------------------------------------------

        OVEREXTENSION_CAP_PERCENT = 8.0

        ema20_value = float(row["ema_20"])

        extension_percent = (
            ((ema20_value - float(row["close"])) / ema20_value) * 100
            if ema20_value
            else 0.0
        )

        checks["not_overextended"] = extension_percent <= OVEREXTENSION_CAP_PERCENT

        if not checks["not_overextended"]:

            reasons.append(
                f"Price {extension_percent:.1f}% below EMA20 (cap {OVEREXTENSION_CAP_PERCENT:.0f}%) — too extended, short-squeeze risk."
            )

        # ==========================================================
        # MARKET FILTER
        # ==========================================================

        # NOTE: a "bear_market" tier2 check used to live here, but it was
        # `row["market_regime"] == "BEAR"` — byte-identical to the
        # "market_trend" Tier 1 check below. Removed for the same reason
        # as the BUY-side mirror (see strategy/buy_strategy.py).

        # --------------------------------------------------
        # VOLATILITY
        # --------------------------------------------------

        checks["volatility"] = row["volatility_state"] != "LOW"

        if checks["volatility"]:

            reasons.append("Volatility supports downside movement.")

        # --------------------------------------------------
        # GAP DAY FILTER
        # --------------------------------------------------

        checks["gap_day"] = not bool(
            row.get(
                "gap_day",
                False,
            )
        )

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
            == "WEAK"
        )

        if checks["breadth"]:

            reasons.append("Weak market breadth.")

        # ==========================================================
        # SECTOR FILTER
        # ==========================================================

        checks["sector"] = sector_score >= 70

        if checks["sector"]:

            reasons.append("Sector showing relative weakness.")

        # ==========================================================
        # FUNDAMENTAL FILTER (weighted, never all-or-nothing)
        # ==========================================================

        fundamental_weakness = sell_fundamental_score(fundamentals)

        checks["fundamental"] = fundamental_weakness >= 55

        if checks["fundamental"]:

            reasons.append(f"Fundamentals weak ({fundamental_weakness:.0f}/100 weakness).")

        # ==========================================================
        # NEWS FILTER (bidirectional — NO news is neutral, never a fail)
        # ==========================================================

        # news_component() is BUY-oriented (higher = more positive); for
        # SELL we want the mirror — higher = more negative sentiment.
        has_news = news_score is not None
        news_negativity = (100.0 - news_component(news_score)) if has_news else None

        checks["news"] = bool(has_news and news_negativity >= 55)

        if checks["news"]:

            reasons.append(f"News sentiment unfavorable ({news_negativity:.0f}/100) — supports bearish trade.")

        elif has_news and news_negativity <= 45:

            reasons.append(f"News sentiment favorable ({100 - news_negativity:.0f}/100) — weighed against SELL.")

        else:

            reasons.append("No news available — decision based on technicals + fundamentals only.")

        # ==========================================================
        # MARKET SCORE FILTER
        # ==========================================================

        checks["market_score"] = market_score >= 60

        if checks["market_score"]:

            reasons.append("Overall market score favors bearish trades.")
        # ==========================================================
        # DECISION ENGINE — TIERED, WEIGHTED (not one giant AND condition)
        # ==========================================================

        # --------------------------------------------------
        # TIER 1 — CORE TREND (majority must agree, not unanimous)
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
        # TIER 3 — CONTEXT (weak fundamentals + negative news + market, weighted)
        # When there's no news, its 30% weight redistributes to
        # fundamentals + market instead of diluting toward neutral.
        # --------------------------------------------------

        inverted_market = 100.0 - min(max(market_score, 0.0), 100.0)

        if has_news:
            tier3_score = (
                fundamental_weakness * 0.55
                + news_negativity * 0.30
                # market_score is BUY-oriented (BULL=75, BEAR=25) — invert
                # it here, since a BEAR market should score HIGH for a
                # SELL setup, not low.
                + inverted_market * 0.15
            )
        else:
            tier3_score = (
                fundamental_weakness * (0.55 / 0.70)
                + inverted_market * (0.15 / 0.70)
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

        # not_overextended is a HARD reject, not just a weighted vote —
        # same reasoning as buy_strategy.py's mirror.
        qualified = (
            tier1_passed
            and overall_score >= QUALIFY_THRESHOLD
            and checks["not_overextended"]
        )

        # --------------------------------------------------
        # CONFIDENCE
        # --------------------------------------------------

        confidence = round(overall_score, 2)

        # --------------------------------------------------
        # REASONS
        # --------------------------------------------------

        if tier1_passed:

            reasons.append("Core bearish trend confirmed (Tier 1).")

        else:

            failed = [key for key, value in tier1_checks.items() if not value]

            reasons.append("Trend not confirmed: " + ", ".join(failed))

        reasons.append(f"Technical confirmation: {optional_passed}/{optional_total}")

        reasons.append(f"Weighted score: {overall_score:.2f}/100 (need >= {QUALIFY_THRESHOLD:.0f})")

        if not checks["not_overextended"]:

            reasons.append("Rejected: overextension cap breached (short-squeeze risk), regardless of score.")

        reasons.append(f"Confidence: {confidence:.2f}%")

        # --------------------------------------------------
        # ACTION
        # --------------------------------------------------

        action = SELL if qualified else NO_TRADE
        # ==========================================================
        # DIAGNOSTICS
        # ==========================================================

        passed_checks = sum(bool(value) for value in checks.values())

        failed_checks = len(checks) - passed_checks

        reasons.append(f"Checks Passed : {passed_checks}")

        reasons.append(f"Checks Failed : {failed_checks}")

        logger.info(
            "SELL Strategy | Action=%s | Confidence=%.2f | Passed=%d/%d",
            action,
            confidence,
            passed_checks,
            len(checks),
        )

        # ==========================================================
        # RETURN
        # ==========================================================

        return SellDecision(
            action=action,
            passed=qualified,
            confidence=confidence,
            reasons=reasons,
            technical_checks=checks,
            tier1_checks=tier1_checks,
            tier1_passed=tier1_passed,
            tier2_score=round(tier2_score, 2),
            tier3_score=round(tier3_score, 2),
            fundamental_weakness=round(fundamental_weakness, 2),
            news_negativity=round(news_negativity, 2) if has_news else None,
            overall_score=round(overall_score, 2),
            qualify_threshold=QUALIFY_THRESHOLD,
        )

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def to_dict(
        decision: SellDecision,
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
        decision: SellDecision,
    ) -> str:

        report = []

        report.append("========== SELL STRATEGY ==========")

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
