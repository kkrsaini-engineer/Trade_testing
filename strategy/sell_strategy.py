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
from decision.state_rules import evaluate_entry_state
from strategy.fundamental_scoring import sell_fundamental_evaluation
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
    # Tier 2 factor-score breakdown (see the restructuring NOTE at the
    # Tier 2 computation site) — kept on the decision object so live
    # production logs/diagnostics can show *why* tier2_score landed
    # where it did, not just the final blended number.
    trend_factor_score: float = 0.0
    momentum_factor_score: float = 0.0
    volume_factor_score: float = 0.0
    volatility_factor_score: float = 0.0
    adx_regime: str = ""
    fundamental_weakness: float = 0.0
    fundamental_coverage: float = 0.0
    news_negativity: float | None = None
    overall_score: float = 0.0
    qualify_threshold: float = 0.0
    # FIX #15 (architecture review — state-based structure), Point 15
    # (PHASE29_NOTES.md): mirrors buy_strategy.py's identical fix — see
    # BuyDecision.state_narrative's NOTE and decision/state_rules.py's
    # module docstring for the full rationale (entry_state now comes
    # from the same rule table that decides `qualified`/`action`, not a
    # separate after-the-fact narration of it).
    state_narrative: str = ""
    # FIX #10/#16 (architecture review — volume-pressure model): mirrors
    # BuyDecision's identical field — see buy_strategy.py's
    # volume_factor_score computation site for the full rationale.
    volume_pressure_uses_delivery: bool = False


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
        # ICHIMOKU CLOUD — REMOVED (user review, mirrors
        # strategy/buy_strategy.py). cloud_trend's underlying
        # senkou_span_a/b are built via .shift(26) — today's cloud
        # level reflects price data up to ~52-78 bars old, a poor fit
        # for a fast-moving swing stock. features/indicators/
        # ichimoku.py itself is untouched — cloud_trend still computes
        # and still shows up in reports/diagnostics for reference — it
        # simply no longer feeds tier2_score, result.overall, or
        # result.risk anywhere.
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
        # GAP FILTER — REMOVED (user review, mirrors
        # strategy/buy_strategy.py). checks["gap_filter"] was dead
        # weight (never reached tier2_score), while row["gap_up"] was
        # separately, live, the single biggest penalty in
        # strategy/sell_scoring.py's _risk_score() (-25, now rescaled).
        # _risk_score() remains gap's one live vote.
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
        #
        # FIX #12 (architecture review — crude % distance): mirrors
        # buy_strategy.py's identical fix — see that file's NOTE for the
        # full rationale, the explicit scope boundary (ATR-normalization
        # only; breakout-age/volume-decay tracking need new persisted
        # per-symbol state and are out of scope here, same class of work
        # as fix #5), and the calibration reasoning (multiplier chosen
        # so a typical ~2% ATR stock lands close to the old flat 8% cap
        # — genuine normalization, not a blanket tightening).
        # --------------------------------------------------

        OVEREXTENSION_ATR_MULTIPLE = 4.0
        OVEREXTENSION_CAP_FLOOR_PERCENT = 5.0
        OVEREXTENSION_CAP_CEILING_PERCENT = 20.0

        ema20_value = float(row["ema_20"])
        close_value = float(row["close"])
        atr_value = float(row.get("atr_14", 0) or 0)

        extension_percent = (
            ((ema20_value - close_value) / ema20_value) * 100
            if ema20_value
            else 0.0
        )

        atr_percent = (atr_value / close_value) * 100 if close_value else 0.0
        overextension_cap_percent = min(
            max(atr_percent * OVEREXTENSION_ATR_MULTIPLE, OVEREXTENSION_CAP_FLOOR_PERCENT),
            OVEREXTENSION_CAP_CEILING_PERCENT,
        )

        checks["not_overextended"] = extension_percent <= overextension_cap_percent

        if not checks["not_overextended"]:

            reasons.append(
                f"Price {extension_percent:.1f}% below EMA20 (ATR-normalized cap "
                f"{overextension_cap_percent:.1f}%, {atr_percent:.1f}% ATR x "
                f"{OVEREXTENSION_ATR_MULTIPLE:.1f}) — too extended, short-squeeze risk."
            )

        # --------------------------------------------------
        # STALE-ENTRY CAP (HARD REJECT, not just a vote) — FIX #5
        # (architecture review — late-entry architecture). Mirrors
        # buy_strategy.py's identical fix — see that file's NOTE for the
        # full rationale and scope boundary. is_breakdown (close below
        # the prior 20-day low) can stay True for many consecutive days
        # once a decline is underway, long after the actual breakdown
        # day; this rejects when that's running but nothing today
        # specifically re-confirms a fresh bearish trigger.
        # --------------------------------------------------

        FRESH_TRIGGER_CHECKS = (
            "ema_fresh_cross", "rsi_fresh_cross",
            "squeeze_breakout", "pullback_entry", "confirmed_breakdown",
        )

        has_fresh_trigger = any(checks.get(key, False) for key in FRESH_TRIGGER_CHECKS)
        is_running_move = bool(row.get("is_breakdown", False))
        setup_is_stale = is_running_move and not has_fresh_trigger

        checks["not_stale_entry"] = not setup_is_stale

        if not checks["not_stale_entry"]:

            reasons.append(
                "Rejected: breakdown is running but no fresh trigger today "
                "(no EMA/RSI cross, squeeze, pullback, or fresh-volume "
                "confirmation) — chasing an already-established decline, "
                "regardless of score."
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

        # FIX #8: sector_score is now None (not a fabricated 50.0) when
        # unavailable — guard against `None >= 70` raising TypeError.
        #
        # SEPARATE, NOT FIXED HERE: same `>= 70` threshold as
        # buy_strategy.py's checks["sector"], reasons text below implies
        # sector_score should be SELL-direction (high = weak sector) the
        # way market_score needed inverting for fix #2 — but
        # execution/scanner.py passes the identical sector_score to both
        # buy_strat and sell_strat with no inversion. Not fixed now:
        # sector_score is unavailable (None) in every real scan today
        # (see execution/scanner.py's NOTE), so this has zero real
        # impact until sector data is actually wired in — flagging so
        # it isn't silently reintroduced as a live bug once that
        # happens, the same way fix #2's market_score issue was found.
        checks["sector"] = sector_score is not None and sector_score >= 70

        if checks["sector"]:

            reasons.append("Sector showing relative weakness.")

        # ==========================================================
        # FUNDAMENTAL FILTER (weighted, never all-or-nothing)
        # ==========================================================

        fundamental_evidence = sell_fundamental_evaluation(fundamentals)
        fundamental_weakness = fundamental_evidence.score
        fundamental_coverage = fundamental_evidence.coverage

        checks["fundamental"] = fundamental_weakness >= 55

        if checks["fundamental"]:

            reasons.append(f"Fundamentals weak ({fundamental_weakness:.0f}/100 weakness).")

        # Coverage exposed for visibility/audit — see the mirrored NOTE
        # in strategy/buy_strategy.py. Not used to gate here.
        reasons.append(
            f"Fundamental data coverage: {fundamental_evidence.available_metrics}"
            f"/{fundamental_evidence.total_metrics} metrics ({fundamental_coverage:.0%})."
        )

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

        # market_score is BUY-oriented (BULL regime=75, BEAR regime=25).
        # For a SELL setup a BEAR (LOW) market_score should pass this
        # check, not a high one — this was found NOT inverted (checking
        # market_score >= 60, same threshold/direction as the BUY side)
        # during an architecture review: the condition and its reason
        # text below were backwards (a bullish market was being reported
        # as "favors bearish trades"). Mirrors the inversion already
        # used in this file's Tier 3 blend (`inverted_market`, see
        # market_context_score above) and in sell_scoring.py's
        # result.market (same fix, same reasoning).
        checks["market_score"] = market_score <= 40

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
        # TIER 2 — TECHNICAL CONFIRMATION: 4 regime-conditionally-weighted
        # category factor scores (Trend / Momentum / Volume / Volatility),
        # mirroring the BUY-side restructuring exactly (see
        # strategy/buy_strategy.py for the full rationale). breadth/sector
        # move to Tier 3's market blend below; not_overextended stays a
        # HARD reject only (see the overextension cap section above), no
        # longer double-counted as a graded Tier 2 vote too.
        #
        # NOTE on asymmetry: SELL's breakdown-engine section tracks
        # "breakdown" + "failed_breakout" where BUY tracks "breakout" +
        # "pullback" — these are NOT literal 1:1 mirrors (pre-existing,
        # not introduced by this restructuring). "failed_breakout" is
        # grouped into VOLATILITY-diagnostic (same bucket as
        # "breakdown"/BUY's "breakout") since it's a breakout-pattern
        # concept, not a trend-support one.
        # --------------------------------------------------

        tier1_and_context_keys = {
            "ema_alignment", "market_trend", "sma_alignment",
            "fundamental", "news", "market_score",
            "breadth", "sector", "not_overextended",
            # FIX #5: hard reject, not a graded Tier2 vote — mirrors
            # not_overextended's exclusion.
            "not_stale_entry",
        }
        tier2_checks = {
            key: value for key, value in checks.items()
            if key not in tier1_and_context_keys
        }

        # "ichimoku" removed (user review) — see the ICHIMOKU CLOUD
        # NOTE above the check computation site for the full rationale.
        TREND_CORE = [
            "price_below_ema20", "supertrend",
            "adx", "ema_fresh_cross", "pullback_entry",
        ]
        MOMENTUM_CORE = ["rsi", "macd_cross", "rsi_fresh_cross"]
        # FIX #7 (architecture review — evidence family / duplicate
        # vote), mirrors buy_strategy.py's identical fix: was
        # ["volume_spike", "obv", "distribution"], but
        # `checks["distribution"] = checks["obv"] and checks["cmf"]` —
        # "obv" was counted both standalone and again inside
        # "distribution" within this same factor-score average. "obv"
        # removed; "distribution" already carries the OBV-negative
        # signal (combined with CMF).
        VOLUME_CORE = ["volume_spike", "distribution"]
        VOLATILITY_CORE = ["squeeze_breakout", "confirmed_breakdown", "atr_filter", "volatility"]

        # FIX #7: same cheap structural guard as buy_strategy.py — only
        # catches a literal duplicate key across *_CORE lists, not
        # signal reuse hidden inside a composite check (see that file's
        # NOTE for the full caveat).
        _all_core_keys = TREND_CORE + MOMENTUM_CORE + VOLUME_CORE + VOLATILITY_CORE
        assert len(_all_core_keys) == len(set(_all_core_keys)), (
            "Duplicate check key across factor-score CORE lists — the "
            "same evidence would be double-counted within tier2_score."
        )

        def _factor_score(core_keys: list[str]) -> float:
            values = [checks[key] for key in core_keys]
            return (sum(values) / len(values)) * 100 if values else 50.0

        trend_factor_score = _factor_score(TREND_CORE)
        momentum_factor_score = _factor_score(MOMENTUM_CORE)
        volatility_factor_score = _factor_score(VOLATILITY_CORE)

        # FIX #10/#16 (architecture review — volume-pressure model):
        # mirrors buy_strategy.py's identical fix — see that file's NOTE
        # for the full rationale and the explicit "this is not a real
        # buy-vs-sell volume split" honesty caveat. delivery_percentage
        # is NOT inverted for SELL (unlike market_score, fix #2) — it
        # measures how much of a day's volume was durable/delivered vs
        # speculative churn, which has no inherent bullish/bearish
        # direction; a high reading means "this move (whichever
        # direction) had real conviction behind it," equally relevant
        # to a SELL setup as a BUY one.
        DELIVERY_WEIGHT_IN_VOLUME = 0.35
        MFI_WEIGHT_IN_VOLUME = 0.15

        # FIX (MFI meaningful-use — raised by user review, mirrors
        # strategy/buy_strategy.py). checks["mfi"] above collapses MFI's
        # full 0-100 range into a binary 20<=mfi<=50 cliff, discarding
        # the extreme readings that are MFI's most information-rich
        # signal. Replaced with a continuous, direction-aware trapezoid
        # — the exact mirror of the BUY version around 50, since SELL
        # wants LOW MFI (bearish money outflow) instead of high:
        #   mfi >= 80   -> 20   (heavy buying inflow — contradicts a
        #                        SELL thesis outright, floored not
        #                        zeroed)
        #   50-80       -> ramps 20 -> 100 as mfi falls (strengthening
        #                        bearish flow)
        #   20-50       -> 100  (healthy bearish money flow — the same
        #                        sweet spot the old binary check used)
        #   0-20        -> ramps 100 -> 40 as mfi falls further
        #                        (deep-oversold bounce-risk — tapered,
        #                        not zeroed, since a genuine breakdown
        #                        can stay oversold for a while)
        # checks["mfi"] itself is left unchanged (reasons text / Tier2
        # "Technical confirmation: X/Y" informational count only) — only
        # mfi_component (what actually reaches volume_factor_score)
        # changes.
        MFI_LOW_FLOOR = 20.0
        MFI_HIGH_FLOOR = 40.0

        def _mfi_component_score(mfi_value: float) -> float:
            if pd.isna(mfi_value):
                return 50.0  # no data -> neutral, not a fabricated claim
            if mfi_value >= 80:
                return MFI_LOW_FLOOR
            if mfi_value > 50:
                return MFI_LOW_FLOOR + (80 - mfi_value) * (80.0 / 30.0)
            if mfi_value >= 20:
                return 100.0
            if mfi_value > 0:
                return 100.0 - (20 - mfi_value) * (60.0 / 20.0)
            return MFI_HIGH_FLOOR

        volume_core_score = _factor_score(VOLUME_CORE)
        mfi_component = _mfi_component_score(float(row["mfi_14"]))
        delivery_pct = row.get("delivery_percentage")
        has_delivery = delivery_pct is not None

        if has_delivery:
            delivery_component = min(max(float(delivery_pct), 0.0), 100.0)
            volume_factor_score = (
                volume_core_score * (1 - DELIVERY_WEIGHT_IN_VOLUME - MFI_WEIGHT_IN_VOLUME)
                + mfi_component * MFI_WEIGHT_IN_VOLUME
                + delivery_component * DELIVERY_WEIGHT_IN_VOLUME
            )
        else:
            volume_factor_score = (
                volume_core_score * (1 - MFI_WEIGHT_IN_VOLUME)
                + mfi_component * MFI_WEIGHT_IN_VOLUME
            )

        reasons.append(
            f"Volume pressure: direction={volume_core_score:.0f} MFI={mfi_component:.0f} "
            + (
                f"delivery={delivery_pct:.1f}% -> {volume_factor_score:.1f}"
                if has_delivery
                else f"delivery=unavailable -> {volume_factor_score:.1f}"
            )
        )

        # Regime-conditional weights, same ADX thresholds as the BUY side
        # (trend strength is direction-agnostic).
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
        # TIER 3 — CONTEXT (weak fundamentals + negative news + market, weighted)
        # When there's no news, its 30% weight redistributes to
        # fundamentals + market instead of diluting toward neutral.
        # --------------------------------------------------

        inverted_market = 100.0 - min(max(market_score, 0.0), 100.0)

        # Market-context blend for the Tier 3 "market" slot — mirrors the
        # BUY side, direction-inverted: WEAK breadth favors SELL (not
        # STRONG). sector_score is NOT inverted here — same convention as
        # the existing checks["sector"] gate above (both BUY and SELL
        # test sector_score >= 70 directly) — see that gate's NOTE for
        # why this is flagged but not fixed now (no real impact while
        # sector_score is unavailable).
        #
        # FIX #8 (architecture review — sector/breadth placeholders):
        # mirrors buy_strategy.py's identical fix. sector_score/breadth
        # are now None when unavailable (was a fabricated 50.0/50.0 —
        # see execution/scanner.py's NOTE, including the bonus
        # float-vs-string type-mismatch bug found there). Their weight
        # redistributes to inverted_market instead of diluting toward a
        # fabricated neutral value — same real-behavior-change caveat as
        # the BUY side: market_context_score now equals inverted_market
        # directly in every real scan today, not a 50/50/50 blend.
        breadth_state = row.get("breadth", "NEUTRAL")
        has_breadth = breadth_state is not None
        has_sector = sector_score is not None

        market_weight, breadth_weight, sector_weight = 0.50, 0.25, 0.25
        active_weight = market_weight
        if has_breadth:
            active_weight += breadth_weight
        if has_sector:
            active_weight += sector_weight

        breadth_component = (
            {"WEAK": 75.0, "STRONG": 25.0}.get(breadth_state, 50.0) * breadth_weight
            if has_breadth else 0.0
        )
        sector_component = (
            min(max(sector_score, 0.0), 100.0) * sector_weight
            if has_sector else 0.0
        )
        market_context_score = (
            inverted_market * market_weight
            + breadth_component
            + sector_component
        ) / active_weight

        # WEIGHT REBALANCE (2026-09-02 — mirrors buy_strategy.py's identical
        # fix; see that file's NOTE for the full rationale). Root cause
        # found via a real-data audit: sell_fundamental_score is a strict
        # mirror of buy_fundamental_score (100 - buy score, see
        # strategy/fundamental_scoring.py), and the average BUY-direction
        # fundamental score across the full NSE500 watchlist on a real scan
        # day (2026-08-31) was 67.86/100 — most constituents currently show
        # decent-to-good fundamentals, which is normal (fundamentals change
        # quarterly) but mechanically forced sell_fundamental_weakness down
        # to ~32/100 on average, independent of any actual price decline.
        # At the old 0.55 weight (~30% of overall_score) this alone was
        # enough to keep almost every SELL candidate below threshold even
        # during a real 15-day, -5% market pullback (measured: 83-187
        # BUY signals/day vs only 2-4 SELL signals/day across 4 real scan
        # days). Rebalanced to 0.35/0.35/0.30 (fundamental/market/news),
        # so the fast-reacting market_context_score (built from
        # inverted_market, which real regime data drives day to day) gets
        # equal say instead of being squeezed to 0.15.
        if has_news:
            tier3_score = (
                fundamental_weakness * 0.35
                + news_negativity * 0.30
                + market_context_score * 0.35
            )
        else:
            tier3_score = (
                fundamental_weakness * (0.35 / 0.70)
                + market_context_score * (0.35 / 0.70)
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
        # STATES (Point 15, PHASE29_NOTES.md) — mirrors
        # buy_strategy.py's identical fix. TrendState/SetupState
        # computed BEFORE qualification, so they can feed the shared
        # decision/state_rules.py rule table as genuine INPUTS rather
        # than being derived AFTER-the-fact purely for display.
        # --------------------------------------------------

        market_state = str(row.get("market_regime", "UNKNOWN")).upper()

        if adx_regime == "RANGE_BOUND":
            trend_state = "RANGE"
        elif checks.get("price_below_ema20"):
            trend_state = "DOWNTREND"
        else:
            trend_state = "UPTREND"

        # FIX #5: setup_state distinguishes STALE_BREAKDOWN — mirrors
        # buy_strategy.py's identical fix.
        if checks.get("squeeze_breakout"):
            setup_state = "SQUEEZE_BREAKDOWN"
        elif checks.get("confirmed_breakdown"):
            setup_state = "BREAKDOWN"
        elif checks.get("pullback_entry"):
            setup_state = "PULLBACK"
        elif setup_is_stale:
            setup_state = "STALE_BREAKDOWN"
        else:
            setup_state = "NONE"

        # --------------------------------------------------
        # FINAL QUALIFICATION (Point 15) — mirrors buy_strategy.py's
        # identical fix; see decision/state_rules.py for the exact rule
        # order and rationale.
        # --------------------------------------------------

        QUALIFY_THRESHOLD = 58.0

        gate_result = evaluate_entry_state(
            trend_state=trend_state,
            unfavorable_trend_state="UPTREND",
            tier1_passed=tier1_passed,
            setup_state=setup_state,
            stale_setup_state="STALE_BREAKDOWN",
            stale_reason=(
                "Rejected: stale entry (no fresh trigger on a running "
                "breakdown), regardless of score."
            ),
            not_overextended=checks["not_overextended"],
            overextended_reason=(
                "Rejected: overextension cap breached (short-squeeze "
                "risk), regardless of score."
            ),
            overall_score=overall_score,
            qualify_threshold=QUALIFY_THRESHOLD,
        )

        qualified = gate_result.qualified

        entry_state = gate_result.entry_state

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

        reasons.append(
            f"Tier2 factors [{adx_regime}]: Trend={trend_factor_score:.0f} "
            f"Momentum={momentum_factor_score:.0f} Volume={volume_factor_score:.0f} "
            f"Volatility={volatility_factor_score:.0f} -> Tier2={tier2_score:.2f}"
        )

        reasons.append(f"Weighted score: {overall_score:.2f}/100 (need >= {QUALIFY_THRESHOLD:.0f})")

        if gate_result.reject_reason:

            reasons.append(gate_result.reject_reason)

        reasons.append(f"Confidence: {confidence:.2f}%")

        # --------------------------------------------------
        # ACTION
        # --------------------------------------------------

        action = SELL if qualified else NO_TRADE

        # --------------------------------------------------
        # STATE NARRATIVE (Point 15) — entry_state now comes from the
        # SAME rule table that decided `qualified` above — mirrors
        # buy_strategy.py's identical fix.
        # --------------------------------------------------

        state_narrative = (
            f"MarketState={market_state} / TrendState={trend_state} / "
            f"SetupState={setup_state} / EntryState={entry_state}"
        )

        reasons.append(state_narrative)

        # ==========================================================
        # DIAGNOSTICS
        # ==========================================================

        passed_checks = sum(bool(value) for value in checks.values())

        failed_checks = len(checks) - passed_checks

        reasons.append(f"Checks Passed : {passed_checks}")

        reasons.append(f"Checks Failed : {failed_checks}")

        logger.info(
            "SELL Strategy | Action=%s | Confidence=%.2f | Passed=%d/%d | "
            "ADXRegime=%s | Trend=%.1f Momentum=%.1f Volume=%.1f Volatility=%.1f | "
            "Tier2=%.2f Tier3=%.2f | State=[%s]",
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
            state_narrative,
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
            trend_factor_score=round(trend_factor_score, 2),
            momentum_factor_score=round(momentum_factor_score, 2),
            volume_factor_score=round(volume_factor_score, 2),
            volatility_factor_score=round(volatility_factor_score, 2),
            adx_regime=adx_regime,
            fundamental_weakness=round(fundamental_weakness, 2),
            fundamental_coverage=fundamental_coverage,
            news_negativity=round(news_negativity, 2) if has_news else None,
            overall_score=round(overall_score, 2),
            qualify_threshold=QUALIFY_THRESHOLD,
            state_narrative=state_narrative,
            volume_pressure_uses_delivery=has_delivery,
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
