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
    # Tier 2 factor-score breakdown (see the restructuring NOTE at the
    # Tier 2 computation site) — kept on the decision object so live
    # production logs/diagnostics can show *why* tier2_score landed
    # where it did, not just the final blended number.
    trend_factor_score: float = 0.0
    momentum_factor_score: float = 0.0
    volume_factor_score: float = 0.0
    volatility_factor_score: float = 0.0
    adx_regime: str = ""
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
        "open",
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
        "bb_upper",
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

        # NOTE: a "macd_histogram > 0" check used to live here, but
        # macd_histogram is computed as `macd - macd_signal`
        # (features/indicators/momentum.py), so "> 0" is mathematically
        # identical to "macd_cross" above (macd > macd_signal) — same
        # condition, counted as a second independent vote. Removed for
        # the same reason as the vwap_confirmation/bull_market
        # duplicates removed earlier (see NOTEs elsewhere in this file).

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

        # NOTE: a "keltner" check used to live here (close > kc_middle),
        # but kc_middle is computed as the 20-period EMA of close
        # (features/indicators/volatility.py: `kc_middle = ema20`) —
        # byte-identical to "ema_20" used in "price_above_ema20" above.
        # Same duplicate-vote reasoning as the other NOTEs in this file.

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
        # EARLY-ENTRY ENGINE
        # Catches a trend near its START instead of after it's already
        # extended. Before this, qualification leaned on EMA/SMA being
        # ALREADY fully stacked and RSI ALREADY sitting in 55-70 — both
        # true only once a move is well underway, which is why entries
        # were landing late/at highs. These checks look for the MOMENT
        # a trend starts (a fresh cross, a dip-buy, a breakout out of a
        # tight range) instead of its already-established state.
        # ==========================================================

        # --------------------------------------------------
        # FRESH EMA CROSS (crossed bullish within the last few bars,
        # not "has been stacked for weeks")
        # --------------------------------------------------

        EMA_CROSS_LOOKBACK = 3

        lookback_n = min(len(dataframe), EMA_CROSS_LOOKBACK + 1)
        recent = dataframe.iloc[-lookback_n:]

        ema_fresh_cross = False

        for i in range(1, len(recent)):

            prev_below = recent["ema_20"].iloc[i - 1] <= recent["ema_50"].iloc[i - 1]

            now_above = recent["ema_20"].iloc[i] > recent["ema_50"].iloc[i]

            if prev_below and now_above:

                ema_fresh_cross = True

                break

        checks["ema_fresh_cross"] = ema_fresh_cross

        if checks["ema_fresh_cross"]:

            reasons.append("Fresh bullish EMA20/EMA50 cross (early trend, not chasing).")

        # --------------------------------------------------
        # RSI FRESH MIDLINE CROSS (just crossed above 50, not
        # "already sitting" in the 55-70 zone for a while)
        # --------------------------------------------------

        RSI_CROSS_LOOKBACK = 2

        recent_rsi = dataframe["rsi_14"].iloc[-min(len(dataframe), RSI_CROSS_LOOKBACK):]

        rsi_fresh_cross = (
            len(recent_rsi) >= 2
            and recent_rsi.iloc[-2] < 50 <= recent_rsi.iloc[-1]
            and recent_rsi.iloc[-1] <= 65
        )

        checks["rsi_fresh_cross"] = bool(rsi_fresh_cross)

        if checks["rsi_fresh_cross"]:

            reasons.append("RSI freshly crossed above the 50 momentum midline.")

        # --------------------------------------------------
        # SQUEEZE BREAKOUT (breakout FROM a prior low-volatility
        # consolidation, not a move that's already run far)
        # --------------------------------------------------

        SQUEEZE_LOOKBACK = 5

        SQUEEZE_BB_WIDTH_THRESHOLD = 0.04

        squeeze_n = min(len(dataframe), SQUEEZE_LOOKBACK + 1)

        prior_bb_width = dataframe["bb_width"].iloc[-squeeze_n:-1] if squeeze_n > 1 else dataframe["bb_width"].iloc[0:0]

        was_squeezed = bool((prior_bb_width < SQUEEZE_BB_WIDTH_THRESHOLD).all()) if len(prior_bb_width) > 0 else False

        # Breakout trigger is the actual Bollinger upper band (close >
        # bb_upper), not the existing is_breakout flag (which is a
        # 20-day-high Donchian breakout — a different, unrelated
        # definition). A squeeze is specifically a Bollinger-Band
        # concept, so the breakout confirming it should be too.
        checks["squeeze_breakout"] = (
            was_squeezed and row["close"] > row["bb_upper"] and checks["volume_spike"]
        )

        if checks["squeeze_breakout"]:

            reasons.append("Breakout above the Bollinger upper band from a prior low-volatility squeeze.")

        # --------------------------------------------------
        # PULLBACK-IN-TREND ENTRY (bought the dip within an
        # established uptrend, instead of a fresh high) — price was
        # WITHIN 1.5% of EMA20 yesterday (touched support) and today
        # closes back above EMA20 on a bullish reversal candle
        # (close > open), not just "low dipped under EMA20 at some
        # point today" (the old is_pullback-based approximation).
        # --------------------------------------------------

        PULLBACK_PROXIMITY_PERCENT = 1.5

        prev_row = dataframe.iloc[-2] if len(dataframe) >= 2 else row
        prev_ema20 = float(prev_row["ema_20"])
        prev_proximity_percent = (
            abs(float(prev_row["close"]) - prev_ema20) / prev_ema20 * 100 if prev_ema20 else 100.0
        )

        checks["pullback_entry"] = bool(
            row["close"] > row["ema_200"]
            and prev_proximity_percent <= PULLBACK_PROXIMITY_PERCENT
            and row["close"] > row["ema_20"]
            and row["close"] > row["open"]
        )

        if checks["pullback_entry"]:

            reasons.append("Pullback to EMA20 support (within 1.5%) followed by a bullish reversal candle.")

        # --------------------------------------------------
        # OVEREXTENSION CAP (HARD REJECT below, not just a vote) —
        # price already too far above EMA20 means the easy part of
        # the move is over; treat it as a chase-risk filter, not a
        # confirmation to weigh in with everything else.
        # --------------------------------------------------

        OVEREXTENSION_CAP_PERCENT = 8.0

        ema20_value = float(row["ema_20"])

        extension_percent = (
            ((float(row["close"]) - ema20_value) / ema20_value) * 100
            if ema20_value
            else 0.0
        )

        checks["not_overextended"] = extension_percent <= OVEREXTENSION_CAP_PERCENT

        if not checks["not_overextended"]:

            reasons.append(
                f"Price {extension_percent:.1f}% above EMA20 (cap {OVEREXTENSION_CAP_PERCENT:.0f}%) — too extended, chase risk."
            )

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
        # TIER 2 — TECHNICAL CONFIRMATION: 4 regime-conditionally-weighted
        # category factor scores (Trend / Momentum / Volume / Volatility),
        # replacing the old flat average-of-~30-checks. A flat average let
        # a category with many near-duplicate checks (e.g. bollinger +
        # donchian + pivot all essentially voting "price above a support
        # line") silently drown out a category with few checks — this
        # weights by CATEGORY instead, and each category's score is built
        # from a curated non-redundant "core" subset, not every check in
        # it. breadth/sector move to Tier 3's market blend below (they're
        # market-wide context, not stock-technical signal); not_overextended
        # stays a HARD reject only (see the overextension cap section
        # above) — it was previously double-counted as ALSO a graded Tier 2
        # vote, which this removes.
        # --------------------------------------------------

        tier1_and_context_keys = {
            "ema_alignment", "market_trend", "sma_alignment",
            "fundamental", "news", "market_score",
            "breadth", "sector", "not_overextended",
        }
        tier2_checks = {
            key: value for key, value in checks.items()
            if key not in tier1_and_context_keys
        }

        TREND_CORE = [
            "price_above_ema20", "supertrend", "ichimoku",
            "adx", "ema_fresh_cross", "pullback_entry",
        ]
        MOMENTUM_CORE = ["rsi", "macd_cross", "rsi_fresh_cross"]
        VOLUME_CORE = ["volume_spike", "obv", "accumulation"]
        VOLATILITY_CORE = ["squeeze_breakout", "confirmed_breakout", "atr_filter", "volatility"]

        def _factor_score(core_keys: list[str]) -> float:
            values = [checks[key] for key in core_keys]
            return (sum(values) / len(values)) * 100 if values else 50.0

        trend_factor_score = _factor_score(TREND_CORE)
        momentum_factor_score = _factor_score(MOMENTUM_CORE)
        volume_factor_score = _factor_score(VOLUME_CORE)
        volatility_factor_score = _factor_score(VOLATILITY_CORE)

        # Regime-conditional weights, driven by the RAW ADX reading (not
        # the boolean "adx" check) — a trending market (ADX >= 25) leans
        # on Trend+Momentum; a range-bound one (ADX < 20) leans on
        # Volatility, since squeeze/breakout timing matters more than
        # "is there a trend" when there mostly isn't one.
        adx_value = float(row["adx_14"])

        if adx_value >= 25:
            adx_regime = "TRENDING"
            factor_weights = {"trend": 0.35, "momentum": 0.30, "volume": 0.20, "volatility": 0.15}
        elif adx_value < 20:
            adx_regime = "RANGE_BOUND"
            factor_weights = {"trend": 0.20, "momentum": 0.20, "volume": 0.20, "volatility": 0.40}
        else:
            adx_regime = "BASELINE"
            factor_weights = {"trend": 0.30, "momentum": 0.25, "volume": 0.25, "volatility": 0.20}

        tier2_score = (
            trend_factor_score * factor_weights["trend"]
            + momentum_factor_score * factor_weights["momentum"]
            + volume_factor_score * factor_weights["volume"]
            + volatility_factor_score * factor_weights["volatility"]
        )

        # --------------------------------------------------
        # TIER 3 — CONTEXT (fundamentals + news + market, weighted)
        # When there is genuinely no news/macro signal for this stock,
        # news gets ZERO weight here (not a neutral-50 dilution) — its
        # weight is redistributed to fundamentals + market so a real
        # fundamental signal isn't watered down by an absent one.
        # --------------------------------------------------

        # Market-context blend for the Tier 3 "market" slot — was plain
        # market_score alone; now also folds in breadth and sector
        # (moved out of the Tier 2 technical-factor framework above).
        # market_score keeps the largest share since it's the one macro
        # signal actually driven by real regime data today.
        #
        # NOTE (real-data caveat, found while wiring this): as of this
        # restructuring, execution/scanner.py hardcodes sector_score=50.0
        # and dataframe["breadth"]=50.0 (a FLOAT, not a "STRONG"/
        # "NEUTRAL"/"WEAK" string) for every real scan — neither
        # cross-symbol sector-rotation nor market-wide breadth data is
        # wired into the per-symbol pipeline yet (see the NOTEs at
        # execution/scanner.py's sector_score/breadth assignment). So
        # today this blend numerically contributes a flat, inert 50.0 for
        # both terms below — moving them here is correct and
        # forward-compatible, it just won't change real behavior until
        # that data is actually wired in.
        breadth_state = row.get("breadth", "NEUTRAL")
        breadth_value = {"STRONG": 75.0, "WEAK": 25.0}.get(breadth_state, 50.0)
        sector_value = min(max(sector_score, 0.0), 100.0)
        market_context_score = (
            min(max(market_score, 0.0), 100.0) * 0.50
            + breadth_value * 0.25
            + sector_value * 0.25
        )

        if has_news:
            tier3_score = (
                fundamental_health * 0.55
                + news_health * 0.30
                + market_context_score * 0.15
            )
        else:
            tier3_score = (
                fundamental_health * (0.55 / 0.70)
                + market_context_score * (0.15 / 0.70)
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
        # even a strong score elsewhere shouldn't override "we are
        # chasing an already-extended move".
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

            reasons.append("Core trend confirmed (Tier 1).")

        else:

            failed = [key for key, value in tier1_checks.items() if not value]

            reasons.append("Trend not confirmed: " + ", ".join(failed))

        reasons.append(f"Technical confirmation: {optional_passed}/{optional_total}")

        reasons.append(
            f"Tier2 factors [{adx_regime}]: Trend={trend_factor_score:.0f} "
            f"Momentum={momentum_factor_score:.0f} Volume={volume_factor_score:.0f} "
            f"Volatility={volatility_factor_score:.0f} -> Tier2={tier2_score:.2f}"
        )

        reasons.append(f"Weighted score: {overall_score:.2f}/100 (need >= {QUALIFY_THRESHOLD:.0f})")

        if not checks["not_overextended"]:

            reasons.append("Rejected: overextension cap breached (chase risk), regardless of score.")

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
            "BUY Strategy | Action=%s | Confidence=%.2f | Passed=%d/%d | "
            "ADXRegime=%s | Trend=%.1f Momentum=%.1f Volume=%.1f Volatility=%.1f | "
            "Tier2=%.2f Tier3=%.2f",
            action,
            confidence,
            passed_checks,
            len(checks),
            adx_regime,
            trend_factor_score,
            momentum_factor_score,
            volume_factor_score,
            volatility_factor_score,
            tier2_score,
            tier3_score,
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
            trend_factor_score=round(trend_factor_score, 2),
            momentum_factor_score=round(momentum_factor_score, 2),
            volume_factor_score=round(volume_factor_score, 2),
            volatility_factor_score=round(volatility_factor_score, 2),
            adx_regime=adx_regime,
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
