"""
BUY Scoring Engine

Professional Production Version

Responsibilities
----------------
- Technical Score
- Fundamental Score
- News Score
- Market Score
- Sector Score
- Liquidity Score
- Volatility Score
- Risk Score
- Overall BUY Score
- Confidence
- Reasons

Author:
Quant Trading Platform
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.logger import get_logger
from core.exceptions import StrategyError
from strategy.fundamental_scoring import buy_fundamental_score
from strategy.buy_strategy import BuyDecision

logger = get_logger(__name__)


# ==========================================================
# CONFIGURATION
# ==========================================================


TECHNICAL_WEIGHT = 0.35
FUNDAMENTAL_WEIGHT = 0.15
NEWS_WEIGHT = 0.10
MARKET_WEIGHT = 0.10
SECTOR_WEIGHT = 0.10
LIQUIDITY_WEIGHT = 0.05
VOLATILITY_WEIGHT = 0.05
RISK_WEIGHT = 0.10

MAX_SCORE = 100.0

# PHASE 18 — _liquidity_score() internal blend (sub-weights of the 5%
# LIQUIDITY_WEIGHT above, not new top-level weights). volume_component
# stays dominant since it's the longest-validated signal; the other two
# are rolling-history-based refinements that only exist once
# data/liquidity_history.py has built up enough days for this symbol —
# see _liquidity_score()'s exclusion+reweight fallback for what happens
# before that (identical to how has_news/has_sector are excluded from
# result.overall rather than defaulted to a fabricated neutral value).
LIQUIDITY_VOLUME_SUBWEIGHT = 0.50
LIQUIDITY_TRADE_QUALITY_SUBWEIGHT = 0.30
LIQUIDITY_AMIHUD_SUBWEIGHT = 0.20

# Ratio thresholds shared by both new sub-components: today's value vs
# its own 20-day rolling average. <= GOOD -> perfect (100); >= BAD ->
# floor (40, not 0 — a single ratio heuristic without cross-sectional
# confirmation shouldn't zero a stock out entirely); linear in between.
LIQUIDITY_RATIO_GOOD = 1.2
LIQUIDITY_RATIO_BAD = 2.0
LIQUIDITY_COMPONENT_FLOOR = 40.0


# ==========================================================
# RESULT MODEL
# ==========================================================


@dataclass(slots=True)
class BuyScore:

    technical: float = 0.0

    fundamental: float = 0.0

    news: float = 0.0

    market: float = 0.0

    sector: float = 0.0

    liquidity: float = 0.0

    volatility: float = 0.0

    risk: float = 0.0

    overall: float = 0.0

    confidence: float = 0.0

    reasons: list[str] = field(default_factory=list)


# ==========================================================
# ENGINE
# ==========================================================


class BuyScoringEngine:
    """
    Calculates the complete BUY score.

    Pipeline

        Technical

        Fundamental

        News

        Market

        Sector

        Liquidity

        Volatility

        Risk

                ↓

        Overall Score

                ↓

        Confidence

                ↓

        Reasons
    """

    def score(
        self,
        dataframe: pd.DataFrame,
        fundamentals: dict[str, Any],
        news_score: float | None,
        market_score: float,
        sector_score: float,
        buy_decision: BuyDecision,
    ) -> BuyScore:

        if dataframe.empty:
            raise StrategyError("Empty dataframe.")

        latest = dataframe.iloc[-1]

        result = BuyScore()

        # UNIFICATION (was: an independent duplicate point-based
        # implementation — see the removed _technical_score() method's
        # git history). This engine used to hand-roll its own ~14-check
        # technical score from scratch, separate from
        # strategy/buy_strategy.py's checks — which meant every technical
        # fix made there (duplicate-vote removal, the exact-formula
        # early-entry checks, the regime-conditional Trend/Momentum/
        # Volume/Volatility factor-score restructuring) never reached
        # this engine, even though THIS engine's overall/technical score
        # is what actually drives buy_strength (50% weight) in
        # decision/decision_engine.py — i.e. what decides BUY-vs-SELL
        # conflict resolution and the final trade ranking in
        # scripts/generate_full_report.py. The old point-system also
        # still had the macd_cross + macd_histogram duplicate-vote bug
        # that was already fixed in buy_strategy.py. Delegating to
        # BuyStrategyEngine's tier2_score (already computed once per
        # symbol scan — see execution/scanner.py's call order) fixes
        # both problems: one technical-scoring implementation instead of
        # two drifting independently, and every future technical fix
        # automatically reaches ranking too.
        result.technical = buy_decision.tier2_score

        result.fundamental = self._fundamental_score(fundamentals)

        has_news = news_score is not None
        result.news = self._normalize(news_score) if has_news else 0.0

        result.market = self._normalize(market_score)

        # FIX #8 (architecture review — sector/breadth placeholders):
        # sector_score is now None (not a fabricated 50.0) when
        # unavailable — see execution/scanner.py's NOTE. self._normalize()
        # already falls back to 50.0 for a non-numeric input (its
        # TypeError/ValueError catch), so result.sector still shows a
        # readable 50.0 for diagnostics/reports; has_sector below is
        # what actually excludes it from result.overall's weighted sum
        # (the diagnostic display value and the scoring weight are
        # deliberately handled separately, same as result.news above).
        has_sector = sector_score is not None
        result.sector = self._normalize(sector_score)

        result.liquidity = self._liquidity_score(latest)

        result.volatility = self._volatility_score(latest)

        result.risk = self._risk_score(latest)

        # When there's no news and/or no sector data, that component's
        # weight is redistributed proportionally across the other
        # components (rather than contributing 0 at its normal weight,
        # which would just silently shrink the total) — generalized from
        # the original has_news-only version to also cover has_sector
        # (FIX #8).
        excluded_weight = (0.0 if has_news else NEWS_WEIGHT) + (0.0 if has_sector else SECTOR_WEIGHT)
        scale = 1.0 / (1.0 - excluded_weight) if excluded_weight < 1.0 else 1.0
        news_w = NEWS_WEIGHT if has_news else 0.0
        sector_w = SECTOR_WEIGHT if has_sector else 0.0

        result.overall = (
            result.technical * TECHNICAL_WEIGHT * scale
            + result.fundamental * FUNDAMENTAL_WEIGHT * scale
            + result.news * news_w
            + result.market * MARKET_WEIGHT * scale
            + result.sector * sector_w
            + result.liquidity * LIQUIDITY_WEIGHT * scale
            + result.volatility * VOLATILITY_WEIGHT * scale
            + result.risk * RISK_WEIGHT * scale
        )

        result.overall = round(result.overall, 2)

        # FIX #11 (architecture review — no-news confidence): pass
        # has_news through so _confidence() can exclude result.news the
        # same way result.overall already does above (scale/news_w),
        # instead of letting a fabricated 0.0 drag down the mean/std
        # confidence calc when there's genuinely no news to score.
        result.confidence = self._confidence(result, has_news=has_news, has_sector=has_sector)

        result.reasons = self._reason_generator(
            latest,
            result,
        )

        logger.info(
            "BUY SCORE = %.2f",
            result.overall,
        )

        return result

    # ==========================================================
    # TECHNICAL SCORE
    # ==========================================================
    # (removed: the independent point-based _technical_score() —
    # result.technical now delegates to BuyStrategyEngine's tier2_score,
    # see the NOTE at the score() call site above.)

    # ==========================================================
    # FUNDAMENTAL SCORE
    # ==========================================================

    def _fundamental_score(
        self,
        fundamentals: dict[str, Any],
    ) -> float:
        return buy_fundamental_score(fundamentals)

    # ==========================================================
    # LIQUIDITY SCORE
    # ==========================================================

    def _liquidity_score(
        self,
        row: pd.Series,
    ) -> float:
        """PHASE 18: blends the original volume-adequacy step function
        with two rolling-history-based components (trade-size quality,
        Amihud illiquidity) that data/liquidity_history.py builds from
        NSE bhavcopy fields already fetched but previously discarded
        (see data/delivery_data.py's module docstring).

        Exclusion+reweight (NOT a fabricated neutral default): if a
        symbol has no rolling history yet (cold start — see
        data/liquidity_history.py's MIN_HISTORY_DAYS) the two new
        components are simply absent and volume_component alone carries
        100% of the weight — i.e. behavior is IDENTICAL to before this
        phase until real rolling data exists for that symbol. This
        mirrors result.overall's has_news/has_sector exclusion pattern
        rather than the naive alternative (defaulting missing
        components to 50.0), which would have silently pulled every
        cold-start score toward the middle instead of leaving the
        validated volume signal untouched.
        """
        volume_component = self._volume_adequacy_component(row)

        weighted = [(volume_component, LIQUIDITY_VOLUME_SUBWEIGHT)]

        trade_quality = self._trade_quality_component(row)
        if trade_quality is not None:
            weighted.append((trade_quality, LIQUIDITY_TRADE_QUALITY_SUBWEIGHT))

        amihud_component = self._amihud_component(row)
        if amihud_component is not None:
            weighted.append((amihud_component, LIQUIDITY_AMIHUD_SUBWEIGHT))

        total_weight = sum(w for _, w in weighted)
        score = sum(c * w for c, w in weighted) / total_weight

        return round(score, 2)

    @staticmethod
    def _volume_adequacy_component(row: pd.Series) -> float:
        """Original step function, unchanged: today's volume vs this
        stock's own 20-day average volume."""
        volume = float(row.get("volume", 0))
        avg_volume = float(row.get("volume_sma_20", 0))

        if avg_volume <= 0:
            return 50.0

        ratio = volume / avg_volume

        if ratio >= 2.00:
            score = 100.0
        elif ratio >= 1.50:
            score = 90.0
        elif ratio >= 1.20:
            score = 80.0
        elif ratio >= 1.00:
            score = 70.0
        elif ratio >= 0.80:
            score = 55.0
        else:
            score = 35.0

        return score

    @staticmethod
    def _ratio_component_score(ratio: float) -> float:
        """Shared interpolation for the two rolling-comparison
        components below: ratio <= LIQUIDITY_RATIO_GOOD -> 100,
        ratio >= LIQUIDITY_RATIO_BAD -> LIQUIDITY_COMPONENT_FLOOR,
        linear in between."""
        if ratio <= LIQUIDITY_RATIO_GOOD:
            return 100.0
        if ratio >= LIQUIDITY_RATIO_BAD:
            return LIQUIDITY_COMPONENT_FLOOR
        frac = (ratio - LIQUIDITY_RATIO_GOOD) / (LIQUIDITY_RATIO_BAD - LIQUIDITY_RATIO_GOOD)
        return 100.0 - frac * (100.0 - LIQUIDITY_COMPONENT_FLOOR)

    @classmethod
    def _trade_quality_component(cls, row: pd.Series) -> float | None:
        """Today's average trade size (TTL_TRD_QNTY / NO_OF_TRADES) vs
        this symbol's own 20-day rolling average. A ratio well above 1
        means today's volume was driven by fewer, larger trades than
        usual — a possible block-deal day, i.e. thinner real liquidity
        than the headline volume number alone would suggest. Returns
        None (not a guess) if the rolling window hasn't built up yet
        for this symbol — see data/liquidity_history.py."""
        today = row.get("avg_trade_size_today")
        rolling = row.get("avg_trade_size_20d")
        if today is None or rolling is None or pd.isna(today) or pd.isna(rolling):
            return None
        rolling = float(rolling)
        if rolling <= 0:
            return None
        ratio = float(today) / rolling
        return cls._ratio_component_score(ratio)

    @classmethod
    def _amihud_component(cls, row: pd.Series) -> float | None:
        """Today's Amihud (2002) illiquidity ratio
        (|price change %| / turnover) vs this symbol's own 20-day
        rolling average. A ratio well above 1 means today's price moved
        unusually far for the turnover it took to move it — a classic
        thin-liquidity signature. Returns None if the rolling window
        hasn't built up yet — see data/liquidity_history.py."""
        today = row.get("amihud_today")
        rolling = row.get("avg_amihud_20d")
        if today is None or rolling is None or pd.isna(today) or pd.isna(rolling):
            return None
        rolling = float(rolling)
        if rolling <= 0:
            return None
        ratio = float(today) / rolling
        return cls._ratio_component_score(ratio)

    # ==========================================================
    # VOLATILITY SCORE
    # ==========================================================

    def _volatility_score(
        self,
        row: pd.Series,
    ) -> float:

        atr = float(row.get("atr_14", 0))

        bb_width = float(row.get("bb_width", 0))

        score = 50.0

        if atr > 0:

            if atr < 1:
                score += 15

            elif atr < 2:
                score += 10

            elif atr < 3:
                score += 5

        if bb_width > 0:

            if bb_width < 0.10:
                score += 15

            elif bb_width < 0.20:
                score += 10

            elif bb_width < 0.30:
                score += 5

        return min(
            round(score, 2),
            100.0,
        )

    # ==========================================================
    # RISK SCORE
    # ==========================================================

    def _risk_score(
        self,
        row: pd.Series,
    ) -> float:

        score = 100.0

        # RECALIBRATED (user review): only 3 real, non-duplicate risk
        # inputs remain after removing volatility_state/market_regime
        # (Phase 16) and gap's dead strategy-file copy (this phase) —
        # the OLD constants (30/20/20/15/10/5, 6 inputs) only summed to
        # 100 because 3 of those 6 were spurious duplicates or already
        # removed. Left as-is, the 3 survivors (30/10/5) only summed to
        # 45, making the worst case floor 55 instead of 0 and making
        # risk_level()'s "EXTREME" tier mathematically unreachable.
        # Rescaled proportionally (same relative weighting the original
        # author gave gap > RSI-extreme > below-average-volume,
        # 6:2:1) so the full 0-100 range is reachable again:
        #   30 * (100/45) = 66.67, 10 * (100/45) = 22.22,
        #   5  * (100/45) = 11.11  (sums to exactly 100.00)
        # This is a proportional rescale of the ORIGINAL author's
        # chosen relative weights, not newly-invented magnitudes — no
        # historical/backtest data exists in this sandbox to derive
        # different ones from scratch (same limitation noted for every
        # other calibration choice in this review).
        GAP_DOWN_PENALTY = 66.67
        RSI_EXTREME_PENALTY = 22.22
        LOW_VOLUME_PENALTY = 11.11

        if row.get("gap_down", False):
            score -= GAP_DOWN_PENALTY

        # volatility_state (ATR HIGH/NORMAL/LOW) penalty REMOVED (user
        # review, same "second hidden path" audit that found the
        # cloud_trend duplicate below). This was the EXACT same
        # condition (row["volatility_state"] == "HIGH") already voted
        # on live in strategy/buy_strategy.py's VOLATILITY_CORE
        # (checks["volatility"], part of tier2_score) — the identical
        # signal was reaching the final decision through two
        # independent scoring paths (tier2_score here, result.risk ->
        # result.overall + BuyProbabilityEngine's confidence/
        # expected_drawdown/risk_level there). Removed here since
        # VOLATILITY_CORE is the one, single live vote for this signal
        # now.

        # market_regime (NIFTY BULL/BEAR) penalty REMOVED (same audit).
        # This was the EXACT same field already voted on live in
        # strategy/buy_strategy.py's Tier1 hard gate
        # (checks["market_trend"], part of the 2-of-3 majority) — same
        # duplicate-path pattern as volatility_state above. Tier1's
        # hard gate is the one, single live vote for this signal now.

        # cloud_trend (Ichimoku) penalty REMOVED (user review) — this
        # was a second, previously-undiscovered place Ichimoku's
        # lagging cloud_trend fed a real decision (result.risk ->
        # result.overall, and separately weighted again in
        # BuyProbabilityEngine), on top of strategy/buy_strategy.py's
        # TREND_CORE vote — found while removing Ichimoku end-to-end,
        # not just from the one place it was originally reported.

        if row.get("rsi_14", 50) > 80:
            score -= RSI_EXTREME_PENALTY

        if row.get("volume", 0) < row.get(
            "volume_sma_20",
            0,
        ):
            score -= LOW_VOLUME_PENALTY

        return max(
            round(score, 2),
            0.0,
        )

    # ==========================================================
    # CONFIDENCE
    # ==========================================================

    def _confidence(
        self,
        result: BuyScore,
        has_news: bool = True,
        has_sector: bool = True,
    ) -> float:

        # FIX #11: when there's genuinely no news (has_news=False),
        # result.news is a fabricated 0.0 (see score()'s
        # `result.news = ... if has_news else 0.0`) — including it here
        # would drag down BOTH the mean and the consistency (std) terms
        # for a component that was never actually measured. result.overall
        # already excludes it via `scale`/`news_w`; this mirrors that
        # same has_news-based exclusion for the confidence calc instead
        # of silently letting the 0.0 sit in the array.
        #
        # FIX #8: same exclusion, generalized to result.sector when
        # sector_score is unavailable (has_sector=False) — result.sector
        # is a self._normalize()-fallback 50.0 in that case (see score()'s
        # NOTE), which would otherwise sit in this array as a fabricated
        # "measured neutral" value.
        components = [
            result.technical,
            result.fundamental,
            result.market,
            result.liquidity,
            result.volatility,
            result.risk,
        ]
        if has_news:
            components.append(result.news)
        if has_sector:
            components.append(result.sector)

        values = np.array(components, dtype=float)

        mean = values.mean()

        consistency = 100 - values.std()

        confidence = mean * 0.60 + consistency * 0.40

        return round(
            max(
                0.0,
                min(
                    confidence,
                    100.0,
                ),
            ),
            2,
        )

    # ==========================================================
    # REASON GENERATOR
    # ==========================================================

    def _reason_generator(
        self,
        row: pd.Series,
        result: BuyScore,
    ) -> list[str]:

        reasons: list[str] = []

        # ---------- Trend ----------
        if row.get("ema_20", 0) > row.get("ema_50", 0):
            reasons.append("EMA20 is above EMA50 (short-term uptrend).")

        if row.get("ema_50", 0) > row.get("ema_200", 0):
            reasons.append("EMA50 is above EMA200 (long-term bullish trend).")

        # ---------- Momentum ----------
        if row.get("macd", 0) > row.get("macd_signal", 0):
            reasons.append("MACD bullish crossover.")

        rsi = row.get("rsi_14", 50)

        if 55 <= rsi <= 70:
            reasons.append("RSI indicates healthy bullish momentum.")

        elif rsi > 70:
            reasons.append("RSI is overbought.")

        # ---------- Breakout ----------
        if row.get("is_breakout", False):
            reasons.append("Price breakout confirmed.")

        if row.get("is_pullback", False):
            reasons.append("Healthy pullback detected.")

        # ---------- Ichimoku ----------
        if row.get("cloud_trend") == "BULL":
            reasons.append("Price is above Ichimoku Cloud.")

        # ---------- Volume ----------
        if row.get("volume", 0) > row.get("volume_sma_20", 0):
            reasons.append("Volume is above 20-period average.")

        # ---------- VWAP ----------
        if row.get("close", 0) > row.get("vwap", 0):
            reasons.append("Price trading above VWAP.")

        # ---------- Market ----------
        if row.get("market_regime") == "BULL":
            reasons.append("Overall market regime is bullish.")

        # ---------- Risk ----------
        if result.risk < 60:
            reasons.append("Elevated market risk.")

        # ---------- Overall ----------
        if result.overall >= 85:
            reasons.append("Excellent BUY setup.")

        elif result.overall >= 70:
            reasons.append("Strong BUY setup.")

        elif result.overall >= 55:
            reasons.append("Moderate BUY setup.")

        else:
            reasons.append("Weak BUY setup.")

        return reasons

    # ==========================================================
    # UTILITIES
    # ==========================================================

    @staticmethod
    def _normalize(value: Any) -> float:
        """
        Normalize a value to the range [0, 100].
        """

        try:
            value = float(value)
        except (TypeError, ValueError):
            return 50.0

        return max(0.0, min(100.0, value))

    # ==========================================================
    # PUBLIC SUMMARY
    # ==========================================================

    @staticmethod
    def to_dict(result: BuyScore) -> dict[str, Any]:
        """
        Convert BuyScore dataclass to dictionary.
        """

        return {
            "technical": result.technical,
            "fundamental": result.fundamental,
            "news": result.news,
            "market": result.market,
            "sector": result.sector,
            "liquidity": result.liquidity,
            "volatility": result.volatility,
            "risk": result.risk,
            "overall": result.overall,
            "confidence": result.confidence,
            "reasons": result.reasons,
        }


# ==========================================================
# END OF FILE
# ==========================================================
