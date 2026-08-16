"""
SELL Scoring Engine

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
- Overall SELL Score
- Confidence
- Reasons
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.logger import get_logger
from core.exceptions import StrategyError
from strategy.fundamental_scoring import sell_fundamental_score
from strategy.sell_strategy import SellDecision

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
# LIQUIDITY_WEIGHT above, not new top-level weights). Mirrors
# buy_scoring.py exactly — liquidity/illiquidity is not a directional
# concept, a thin/block-deal-driven market is equally bad information
# for an exit decision as for an entry decision, so there is nothing to
# invert here (unlike e.g. gap_down/gap_up).
LIQUIDITY_VOLUME_SUBWEIGHT = 0.50
LIQUIDITY_TRADE_QUALITY_SUBWEIGHT = 0.30
LIQUIDITY_AMIHUD_SUBWEIGHT = 0.20

LIQUIDITY_RATIO_GOOD = 1.2
LIQUIDITY_RATIO_BAD = 2.0
LIQUIDITY_COMPONENT_FLOOR = 40.0


# ==========================================================
# RESULT MODEL
# ==========================================================


@dataclass(slots=True)
class SellScore:

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


class SellScoringEngine:
    """
    Calculates the complete SELL score.
    """

    def score(
        self,
        dataframe: pd.DataFrame,
        fundamentals: dict[str, Any],
        news_score: float | None,
        market_score: float,
        sector_score: float,
        sell_decision: SellDecision,
    ) -> SellScore:

        if dataframe.empty:
            raise StrategyError("Empty dataframe.")

        latest = dataframe.iloc[-1]

        result = SellScore()

        # UNIFICATION (was: an independent duplicate point-based
        # implementation, mirroring buy_scoring.py's same fix — see the
        # removed _technical_score() method's git history). This
        # engine's own technical score used to be a separate ~14-check
        # point system (with its own now-corrected max_points=22
        # calibration bug, see the removed method's old comment) that
        # never reflected any of strategy/sell_strategy.py's technical
        # fixes (duplicate-vote removal, exact-formula early-entry,
        # regime-conditional factor-score restructuring) — even though
        # THIS engine's score is what actually drives sell_strength (50%
        # weight) in decision/decision_engine.py, i.e. what decides
        # BUY-vs-SELL conflict resolution and the final trade ranking.
        # Delegating to SellStrategyEngine's tier2_score (already
        # computed once per symbol scan) fixes both problems.
        result.technical = sell_decision.tier2_score

        result.fundamental = self._fundamental_score(fundamentals)

        has_news = news_score is not None
        result.news = self._normalize(news_score) if has_news else 0.0

        # market_score is BUY-oriented (BULL regime=75, BEAR regime=25 —
        # see execution/scanner.py's market_score assignment). For a
        # SELL setup a BEAR market should score HIGH here, not low —
        # invert it, mirroring the same fix already applied to
        # strategy/sell_strategy.py's Tier 3 blend (`inverted_market`).
        # This was found NOT inverted here during an architecture review
        # — confirmed real impact: strategy/sell_probability.py's own
        # reason text ("Overall market supports bearish trades." when
        # score.market >= 70) already assumed this was inverted; it
        # wasn't, so that text was firing backwards (on bullish markets)
        # before this fix.
        result.market = 100.0 - self._normalize(market_score)

        # FIX #8 (architecture review — sector/breadth placeholders):
        # mirrors buy_scoring.py's identical fix. sector_score is now
        # None (not a fabricated 50.0) when unavailable — see
        # execution/scanner.py's NOTE. self._normalize() falls back to
        # 50.0 for display; has_sector below excludes it from
        # result.overall's weighted sum instead.
        has_sector = sector_score is not None
        result.sector = self._normalize(sector_score)

        result.liquidity = self._liquidity_score(latest)

        result.volatility = self._volatility_score(latest)

        result.risk = self._risk_score(latest)

        # Generalized from has_news-only to also cover has_sector (FIX #8)
        # — same redistribution pattern as buy_scoring.py.
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

        # FIX #11 (architecture review — no-news confidence): mirrors
        # buy_scoring.py's identical fix — pass has_news through so
        # _confidence() can exclude the fabricated result.news=0.0 the
        # same way result.overall already does via scale/news_w.
        result.confidence = self._confidence(result, has_news=has_news, has_sector=has_sector)

        result.reasons = self._reason_generator(
            latest,
            result,
        )

        logger.info(
            "SELL SCORE = %.2f",
            result.overall,
        )

        return result

    # ==========================================================
    # TECHNICAL SCORE
    # ==========================================================
    # (removed: the independent point-based _technical_score() —
    # result.technical now delegates to SellStrategyEngine's tier2_score,
    # see the NOTE at the score() call site above.)

    # ==========================================================
    # FUNDAMENTAL SCORE
    # ==========================================================

    def _fundamental_score(
        self,
        fundamentals: dict[str, Any],
    ) -> float:
        return sell_fundamental_score(fundamentals)

    # ==========================================================
    # LIQUIDITY SCORE
    # ==========================================================

    def _liquidity_score(
        self,
        row: pd.Series,
    ) -> float:
        """PHASE 18: see buy_scoring.py's _liquidity_score() docstring —
        identical logic, mirrored (liquidity is non-directional, see
        the SELL-side note at LIQUIDITY_VOLUME_SUBWEIGHT above)."""
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
        this symbol's own 20-day rolling average — see buy_scoring.py's
        version for the full explanation. Returns None if the rolling
        window hasn't built up yet for this symbol."""
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
        """Today's Amihud (2002) illiquidity ratio vs this symbol's own
        20-day rolling average — see buy_scoring.py's version for the
        full explanation. Returns None if the rolling window hasn't
        built up yet for this symbol."""
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

        # Higher volatility favors SELL opportunities
        if atr >= 3:
            score += 15

        elif atr >= 2:
            score += 10

        elif atr >= 1:
            score += 5

        if bb_width >= 0.30:
            score += 15

        elif bb_width >= 0.20:
            score += 10

        elif bb_width >= 0.10:
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

        # RECALIBRATED (user review, mirrors strategy/buy_scoring.py) —
        # only 3 real, non-duplicate risk inputs remain after removing
        # market_regime (Phase 16) and gap's dead strategy-file copy
        # (this phase). Rescaled proportionally so the full 0-100 range
        # is reachable again (old 25/10/5 summed to only 40, floor was
        # 60): 25*(100/40)=62.5, 10*(100/40)=25.0, 5*(100/40)=12.5
        # (sums to exactly 100.0).
        GAP_UP_PENALTY = 62.5
        RSI_EXTREME_PENALTY = 25.0
        LOW_VOLUME_PENALTY = 12.5

        if row.get("gap_up", False):
            score -= GAP_UP_PENALTY

        # market_regime (NIFTY BULL/BEAR) penalty REMOVED (user review,
        # same audit that found the cloud_trend duplicate below). This
        # was the EXACT same field already voted on live in
        # strategy/sell_strategy.py's Tier1 hard gate
        # (checks["market_trend"], part of the 2-of-3 majority). Note:
        # unlike BUY, SELL's _risk_score() never had a volatility_state
        # line — nothing to remove there.

        # cloud_trend (Ichimoku) penalty REMOVED (user review) — mirrors
        # strategy/buy_scoring.py. See that file's _risk_score() for the
        # full rationale (this was a second, previously-undiscovered
        # place Ichimoku fed a real decision, on top of
        # strategy/sell_strategy.py's TREND_CORE vote).

        if row.get("rsi_14", 50) < 20:
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
        result: SellScore,
        has_news: bool = True,
        has_sector: bool = True,
    ) -> float:

        # FIX #11: see buy_scoring.py's identical fix for the full
        # rationale — excludes the fabricated result.news=0.0 from the
        # confidence calc when there's genuinely no news, mirroring the
        # has_news-based exclusion result.overall already uses.
        #
        # FIX #8: same exclusion for result.sector when sector_score is
        # unavailable (has_sector=False) — see buy_scoring.py's
        # identical fix for the full rationale.
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
        result: SellScore,
    ) -> list[str]:

        reasons: list[str] = []

        # ---------- Trend ----------
        if row.get("ema_20", 0) < row.get("ema_50", 0):
            reasons.append("EMA20 is below EMA50 (short-term downtrend).")

        if row.get("ema_50", 0) < row.get("ema_200", 0):
            reasons.append("EMA50 is below EMA200 (long-term bearish trend).")

        # ---------- Momentum ----------
        if row.get("macd", 0) < row.get("macd_signal", 0):
            reasons.append("MACD bearish crossover.")

        rsi = row.get("rsi_14", 50)

        if 30 <= rsi <= 45:
            reasons.append("RSI indicates bearish momentum.")

        elif rsi < 30:
            reasons.append("RSI is oversold. Watch for possible bounce.")

        # ---------- Breakdown ----------
        if row.get("is_breakdown", False):
            reasons.append("Price breakdown confirmed.")

        if row.get("failed_breakout", False):
            reasons.append("Failed breakout detected.")

        # ---------- Ichimoku ----------
        if row.get("cloud_trend") == "BEAR":
            reasons.append("Price is below Ichimoku Cloud.")

        # ---------- Volume ----------
        if row.get("volume", 0) > row.get("volume_sma_20", 0):
            reasons.append("High selling volume detected.")

        # ---------- VWAP ----------
        if row.get("close", 0) < row.get("vwap", 0):
            reasons.append("Price trading below VWAP.")

        # ---------- Market ----------
        if row.get("market_regime") == "BEAR":
            reasons.append("Overall market regime is bearish.")

        # ---------- Risk ----------
        if result.risk < 60:
            reasons.append("High downside risk environment.")

        # ---------- Overall ----------
        if result.overall >= 85:
            reasons.append("Excellent SELL setup.")

        elif result.overall >= 70:
            reasons.append("Strong SELL setup.")

        elif result.overall >= 55:
            reasons.append("Moderate SELL setup.")

        else:
            reasons.append("Weak SELL setup.")

        return reasons

    # ==========================================================
    # UTILITIES
    # ==========================================================

    @staticmethod
    def _normalize(value: Any) -> float:

        try:
            value = float(value)
        except (TypeError, ValueError):
            return 50.0

        return max(0.0, min(100.0, value))

    # ==========================================================
    # PUBLIC SUMMARY
    # ==========================================================

    @staticmethod
    def to_dict(result: SellScore) -> dict[str, Any]:

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
# ==================================================
