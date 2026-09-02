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
from decision.state_rules import evaluate_entry_state
from strategy.fundamental_scoring import buy_fundamental_evaluation
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
    fundamental_coverage: float = 0.0
    news_health: float | None = None
    overall_score: float = 0.0
    qualify_threshold: float = 0.0
    # FIX #15 (architecture review — state-based structure over raw
    # check counts): a human-readable narrative — "MarketState=BULL /
    # TrendState=UPTREND / SetupState=PULLBACK / EntryState=WAITING" —
    # instead of only "32/40" / "86.55/100". Originally (Phases 2-25)
    # this was presentation-only, computed AFTER `qualified` purely to
    # describe it. Point 15 (PHASE29_NOTES.md) promoted it to the actual
    # source of truth: TrendState/SetupState are now computed BEFORE
    # qualification and fed into decision/state_rules.py's ordered rule
    # table, which decides BOTH `qualified`/`action` AND `entry_state`
    # together — see that module's docstring for the exact contradiction
    # this closes (a 2-of-3 tier1 majority vote could previously pass
    # while the single-check TrendState disagreed).
    state_narrative: str = ""
    # FIX #10/#16 (architecture review — volume-pressure model): whether
    # volume_factor_score above includes real NSE delivery-percentage
    # data, or fell back to the direction-vote-only composite because
    # delivery data was unavailable for this symbol/day. See the
    # volume_factor_score computation site's NOTE for the full
    # rationale, including the explicit "this is not a real buy-vs-sell
    # volume split" caveat.
    volume_pressure_uses_delivery: bool = False


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
        # ICHIMOKU CLOUD — REMOVED (user review). cloud_trend's
        # underlying senkou_span_a/b are built via .shift(26) — today's
        # cloud level is derived from price data up to ~52-78 bars old.
        # For a fast-moving swing stock that lag meant this check (and
        # the identical cloud_trend penalty previously buried in
        # buy_scoring.py's _risk_score(), found and removed at the same
        # time) could keep voting on a stale, pre-move price structure
        # well after a real trend change. features/indicators/
        # ichimoku.py itself is untouched — cloud_trend still computes
        # and still shows up in reports/diagnostics for reference — it
        # simply no longer feeds tier2_score, result.overall, or
        # result.risk anywhere.
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
        # GAP — REMOVED (user review). checks["gap_filter"] was dead
        # weight here (never in any *_CORE list, never reached
        # tier2_score — only counted in the informational "Technical
        # confirmation: X/Y" tally), while the SAME row["gap_down"]
        # value was separately, live, the single biggest penalty in
        # strategy/buy_scoring.py's _risk_score() (-30, now rescaled,
        # see that file). Rather than wire a second live vote here
        # (which would recreate the exact volatility_state/
        # market_regime duplicate-path problem just fixed), the dead
        # copy is removed — _risk_score() remains gap's one live vote.
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
        #
        # FIX #12 (architecture review — crude % distance): was a single
        # fixed OVEREXTENSION_CAP_PERCENT = 8.0 applied identically to
        # every stock regardless of its own volatility — a low-ATR stock
        # 8% above EMA20 and a high-ATR stock 8% above EMA20 are not
        # equally extended. Cap is now ATR-normalized: expressed as a
        # multiple of the stock's OWN ATR-as-percent-of-price, floored/
        # ceilinged to a sane range so a near-zero-ATR stock doesn't get
        # an unreasonably tight cap and a very high-ATR/penny/junk stock
        # doesn't get an unreasonably loose one.
        #
        # SCOPE NOTE: the critique's fuller "Entry Distance Model" also
        # wants breakout-age (days-since-signal) and volume-decay-after-
        # breakout tracking — both need NEW persisted per-symbol state
        # across scan days, which doesn't exist anywhere in this
        # codebase today (confirmed via search) and is a materially
        # bigger, separate scope item (same class as fix #5). This fix
        # is deliberately scoped to the ATR-normalization part only,
        # which needs no new state — atr_14 is already computed per row.
        #
        # CALIBRATION: multiplier chosen so a "typical" NSE swing-trade
        # candidate (roughly 2% ATR-as-percent-of-price) lands close to
        # the OLD flat 8% cap (2% * 4.0 = 8%) — i.e. this is a genuine
        # ATR-normalization, not a blanket tightening: a lower-than-
        # typical-ATR stock now gets a tighter cap than 8% (correctly —
        # it was never that volatile to begin with), a higher-than-
        # typical-ATR stock gets a wider one (correctly — 8% is nothing
        # unusual for a genuinely volatile stock), and a stock at
        # roughly-typical ATR sees close to no change.
        # --------------------------------------------------

        OVEREXTENSION_ATR_MULTIPLE = 4.0
        OVEREXTENSION_CAP_FLOOR_PERCENT = 5.0
        OVEREXTENSION_CAP_CEILING_PERCENT = 20.0

        ema20_value = float(row["ema_20"])
        close_value = float(row["close"])
        atr_value = float(row.get("atr_14", 0) or 0)

        extension_percent = (
            ((close_value - ema20_value) / ema20_value) * 100
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
                f"Price {extension_percent:.1f}% above EMA20 (ATR-normalized cap "
                f"{overextension_cap_percent:.1f}%, {atr_percent:.1f}% ATR x "
                f"{OVEREXTENSION_ATR_MULTIPLE:.1f}) — too extended, chase risk."
            )

        # --------------------------------------------------
        # STALE-ENTRY CAP (HARD REJECT, not just a vote) — FIX #5
        # (architecture review — late-entry architecture / Trend-Setup-
        # Entry separation).
        #
        # The gap this closes: not_overextended above already hard-
        # rejects when price has moved too FAR from EMA20 — but a setup
        # can be well within that distance cap and STILL be stale: the
        # underlying breakout condition (`is_breakout` — close above the
        # prior 20-day high, see features/indicators/breakout.py) can
        # stay True for many consecutive days once a stock is trending,
        # long after the actual breakout day. Before this fix, an old,
        # no-longer-fresh breakout like that could still mathematically
        # qualify if enough OTHER, unrelated checks (fundamentals,
        # oscillators, sector, ...) compensated in the weighted blend —
        # exactly the critique's "old breakout... hard NO_TRADE even if
        # score=92" scenario, for the staleness dimension specifically
        # (extension is already covered by not_overextended above).
        #
        # Definition: "stale" = is_breakout is running (the move is
        # underway) AND none of the checks that specifically detect a
        # FRESH trigger — ema_fresh_cross, rsi_fresh_cross,
        # squeeze_breakout, pullback_entry, confirmed_breakout (today's
        # volume confirmation counts as "fresh enough" even on an
        # older breakout) — fired. No new persisted state: every one of
        # these checks already looks back a few rows within the SAME
        # scan's dataframe (see EMA_CROSS_LOOKBACK/RSI_CROSS_LOOKBACK/
        # SQUEEZE_LOOKBACK above), not across separate scan days.
        #
        # SCOPE NOTE: this is deliberately NOT the critique's fuller
        # architecture (explicit TrendState/SetupState/EntryState
        # dataclasses, qualified re-derived from state combinations
        # instead of a threshold, persisted breakout-age tracking across
        # scan days). That is a materially bigger rewrite with real risk
        # across the whole qualify/reject surface — this fix targets the
        # ONE concrete, currently-real gap (staleness isn't gated at
        # all today) with the narrowest change that closes it, reusing
        # signals that already exist. See PHASE12_NOTES.md for the full
        # scope discussion and the flagged real-behavior-change caveat.
        # --------------------------------------------------

        FRESH_TRIGGER_CHECKS = (
            "ema_fresh_cross", "rsi_fresh_cross",
            "squeeze_breakout", "pullback_entry", "confirmed_breakout",
        )

        has_fresh_trigger = any(checks.get(key, False) for key in FRESH_TRIGGER_CHECKS)
        is_running_move = bool(row.get("is_breakout", False))
        setup_is_stale = is_running_move and not has_fresh_trigger

        checks["not_stale_entry"] = not setup_is_stale

        if not checks["not_stale_entry"]:

            reasons.append(
                "Rejected: breakout is running but no fresh trigger today "
                "(no EMA/RSI cross, squeeze, pullback, or fresh-volume "
                "confirmation) — chasing an already-established move, "
                "regardless of score."
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

        # FIX #8: sector_score is now None (not a fabricated 50.0) when
        # unavailable (see execution/scanner.py's NOTE) — guard against
        # `None >= 70` raising TypeError. None correctly stays a
        # not-passed check here (same as before: 50.0 >= 70 was False
        # too), just without needing a fabricated number to get there.
        checks["sector"] = sector_score is not None and sector_score >= 70

        if checks["sector"]:

            reasons.append("Strong sector momentum.")

        # ==========================================================
        # FUNDAMENTAL FILTER (weighted, never all-or-nothing)
        # ==========================================================

        fundamental_evidence = buy_fundamental_evaluation(fundamentals)
        fundamental_health = fundamental_evidence.score
        fundamental_coverage = fundamental_evidence.coverage

        checks["fundamental"] = fundamental_health >= 55

        if checks["fundamental"]:

            reasons.append(f"Fundamentals healthy ({fundamental_health:.0f}/100).")

        # Coverage is exposed for visibility/audit (a score of 72 from
        # 8/8 metrics vs. 1/8 metrics isn't equally trustworthy) — see
        # strategy/fundamental_scoring.py's FundamentalEvidence. NOT used
        # to gate/reject here — what to do with low coverage is a policy
        # decision left to whoever consumes fundamental_coverage
        # downstream (e.g. execution/scanner.py's diagnostics), not
        # decided unilaterally in this file.
        reasons.append(
            f"Fundamental data coverage: {fundamental_evidence.available_metrics}"
            f"/{fundamental_evidence.total_metrics} metrics ({fundamental_coverage:.0%})."
        )

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
            # FIX #5: not_stale_entry is a HARD reject (see its
            # computation site), not a graded Tier2 vote — excluded the
            # same way not_overextended already is, so it isn't
            # double-counted.
            "not_stale_entry",
        }
        tier2_checks = {
            key: value for key, value in checks.items()
            if key not in tier1_and_context_keys
        }

        # "ichimoku" removed (user review) — see the ICHIMOKU CLOUD
        # NOTE above the check computation site for the full rationale.
        TREND_CORE = [
            "price_above_ema20", "supertrend",
            "adx", "ema_fresh_cross", "pullback_entry",
        ]
        MOMENTUM_CORE = ["rsi", "macd_cross", "rsi_fresh_cross"]
        # FIX #7 (architecture review — evidence family / duplicate
        # vote): was ["volume_spike", "obv", "accumulation"]. But
        # `checks["accumulation"] = checks["obv"] and checks["cmf"]`
        # (see the ACCUMULATION check above) — "obv" was being counted
        # BOTH on its own AND again inside "accumulation" within this
        # same factor-score average, i.e. the exact "same underlying
        # signal double-counted without adding independent evidence"
        # pattern the macd_cross/macd_histogram fix above this method
        # already removed once — reintroduced here by Phase 4's own
        # factor grouping. "obv" removed; "accumulation" already
        # carries the OBV-positive signal (combined with CMF).
        VOLUME_CORE = ["volume_spike", "accumulation"]
        VOLATILITY_CORE = ["squeeze_breakout", "confirmed_breakout", "atr_filter", "volatility"]

        # FIX #7: cheap structural guard, not a full "evidence family"
        # abstraction (that would need dependency-tracking through
        # composite checks like "accumulation" itself — out of scope
        # for this pass). This only catches the LITERAL-duplicate-key
        # case above (the same check key listed in two *_CORE lists) —
        # it will NOT catch a future composite check silently re-using
        # another check's underlying signal (as "accumulation" did with
        # "obv" here). Treat this as a guard against copy-paste
        # duplication, not a guarantee against all double-counting.
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

        # FIX #10/#16 (architecture review — volume-pressure model): was
        # purely _factor_score(VOLUME_CORE) = average of [volume_spike,
        # accumulation] — both same-timeframe-OHLCV price*volume
        # proxies, no genuinely independent evidence source. Blends in
        # two additional, non-duplicate signals:
        #  - mfi (Money Flow Index — checks["mfi"] was already computed
        #    but never counted in this factor before).
        #  - delivery_percentage — REAL NSE settlement data (the % of
        #    the day's traded volume actually delivered/held rather
        #    than squared off intraday; see data/delivery_data.py's
        #    DeliveryDataProvider, already wired into
        #    execution/scanner.py's dataframe but, until this fix, never
        #    consumed by any scoring in this file — only by
        #    decision/validation_engine.py's binary >=20% liquidity
        #    gate). This is a genuinely different signal from OBV/CMF/
        #    MFI (all price*volume proxies derived from the same OHLCV
        #    bars) — an independent, NSE-published measure of how much
        #    of today's volume was durable/delivered vs speculative
        #    same-day churn.
        #
        # HONESTY NOTE — this does NOT answer "exactly how much of
        # today's volume was buying vs selling." No indicator built from
        # daily OHLCV + bhavcopy data — not this one, not OBV, not CMF,
        # not MFI — can do that; it needs tick-level order-flow/Level 2
        # data, which doesn't exist anywhere in this pipeline and can't
        # be fetched from this sandbox. What this fix genuinely adds is
        # a real, independent way to separate DURABLE volume (delivered)
        # from SAME-DAY SPECULATIVE NOISE — a narrower, honestly-scoped
        # improvement, not the buy-vs-sell split itself. See
        # PHASE13_NOTES.md for the full discussion.
        DELIVERY_WEIGHT_IN_VOLUME = 0.35
        MFI_WEIGHT_IN_VOLUME = 0.15

        # FIX (MFI meaningful-use — raised by user review): checks["mfi"]
        # above collapses MFI's full 0-100 range into a binary
        # 50<=mfi<=80 cliff, discarding the extreme readings (>80
        # overbought, <20 oversold) that are actually MFI's most
        # information-rich signal — a stock at MFI=90 (deep overbought,
        # real reversal-risk warning) was scored identically (0) to one
        # at MFI=10 (deep oversold). Replaced with a continuous,
        # direction-aware trapezoid instead of a hard cutoff:
        #   mfi <= 20   -> 20   (money flowing OUT — contradicts a BUY
        #                        thesis outright, floored not zeroed)
        #   20-50       -> ramps 20 -> 100 (strengthening bullish flow)
        #   50-80       -> 100  (healthy bullish money flow — the same
        #                        sweet spot the old binary check used)
        #   80-100      -> ramps 100 -> 40 (overbought/reversal risk —
        #                        tapered, not zeroed, since strong
        #                        momentum can persist through overbought)
        # checks["mfi"] itself is left unchanged (still used for the
        # reasons text and the Tier2 "Technical confirmation: X/Y"
        # informational count) — only mfi_component (what actually
        # reaches volume_factor_score) changes.
        MFI_LOW_FLOOR = 20.0
        MFI_HIGH_FLOOR = 40.0

        def _mfi_component_score(mfi_value: float) -> float:
            if pd.isna(mfi_value):
                return 50.0  # no data -> neutral, not a fabricated claim
            if mfi_value <= 20:
                return MFI_LOW_FLOOR
            if mfi_value < 50:
                return MFI_LOW_FLOOR + (mfi_value - 20) * (80.0 / 30.0)
            if mfi_value <= 80:
                return 100.0
            if mfi_value < 100:
                return 100.0 - (mfi_value - 80) * (60.0 / 20.0)
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
            # Delivery data unavailable for this symbol/day — redistribute
            # its weight back to the direction-vote components instead of
            # fabricating a neutral number (same has_X convention as
            # fixes #8/#11).
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
        # FIX #8 (architecture review — sector/breadth placeholders):
        # execution/scanner.py used to hardcode sector_score=50.0 and
        # dataframe["breadth"]=50.0 (a FLOAT, silently mismatched
        # against the "STRONG"/"WEAK"/"NEUTRAL" strings this blend
        # expects — checks["breadth"] above was unconditionally False
        # because of it, a bonus bug found while fixing this). Both are
        # now None when unavailable (see execution/scanner.py's NOTE),
        # and rather than quietly blending in a fabricated 50.0 for 50%
        # of this score's weight, that weight is redistributed to
        # market_score — the one component that's actually real today —
        # mirroring the same has_news-style weight-redistribution
        # pattern this method already uses for news below. Once real
        # sector-rotation/market-breadth data is eventually wired in
        # (market/sector_rotation.py's SectorRotationEngine and
        # market/market_breadth.py's MarketBreadthEngine already exist
        # but are fed no market-wide data by this per-symbol scan — a
        # data-infrastructure item, out of scope here), this blend picks
        # the real values up automatically with no further code change.
        #
        # REAL BEHAVIOR CHANGE (flagging explicitly, not cosmetic):
        # since sector/breadth are unavailable in every real scan today,
        # market_context_score now equals market_score directly (100%
        # weight) instead of being diluted toward a fabricated 50 for
        # half its weight — Tier 3 / overall_score numbers will shift
        # for every symbol as a result of this fix, not just in the
        # hypothetical case where sector/breadth data exists.
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
            {"STRONG": 75.0, "WEAK": 25.0}.get(breadth_state, 50.0) * breadth_weight
            if has_breadth else 0.0
        )
        sector_component = (
            min(max(sector_score, 0.0), 100.0) * sector_weight
            if has_sector else 0.0
        )
        market_context_score = (
            min(max(market_score, 0.0), 100.0) * market_weight
            + breadth_component
            + sector_component
        ) / active_weight

        # WEIGHT REBALANCE (2026-09-02 — raised by user review): fundamentals
        # move on a QUARTERLY cadence (revenue growth, ROE, PE, etc. don't
        # shift within a swing trade's 1-15 day holding window), yet used
        # to carry 0.55 of tier3 (~30% of overall_score) — the single
        # largest input into a signal that's fundamentally about near-term
        # price/technical timing. market_context_score (built from
        # market_score, which real regime data actually drives day to day —
        # see FIX #8 above) is the fast-reacting counterpart and used to be
        # squeezed to just 0.15. Rebalanced so market_context now carries
        # equal weight to fundamentals (0.35 each); news keeps its 0.30 —
        # mirrored identically in sell_strategy.py's tier3, so BUY and SELL
        # stay weighted the same way, just direction-inverted inputs.
        if has_news:
            tier3_score = (
                fundamental_health * 0.35
                + news_health * 0.30
                + market_context_score * 0.35
            )
        else:
            tier3_score = (
                fundamental_health * (0.35 / 0.70)
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
        # STATES (Point 15, PHASE29_NOTES.md) — TrendState/SetupState
        # computed BEFORE qualification, from the checks/scores already
        # computed above, so they can feed the state-transition rule
        # table below as genuine INPUTS rather than being derived
        # AFTER-the-fact from `qualified` purely for display (that
        # older, presentation-only design is exactly what let TrendState
        # and the actual qualification gate silently disagree — see
        # decision/state_rules.py's module docstring for the concrete
        # bug this closes).
        # --------------------------------------------------

        market_state = str(row.get("market_regime", "UNKNOWN")).upper()

        if adx_regime == "RANGE_BOUND":
            trend_state = "RANGE"
        elif checks.get("price_above_ema20"):
            trend_state = "UPTREND"
        else:
            trend_state = "DOWNTREND"

        # FIX #5: setup_state distinguishes a genuinely fresh setup from
        # a STALE_BREAKOUT (is_breakout running, nothing fresh triggered
        # — see the not_stale_entry NOTE above) instead of collapsing
        # both into "NONE".
        if checks.get("squeeze_breakout"):
            setup_state = "SQUEEZE_BREAKOUT"
        elif checks.get("confirmed_breakout"):
            setup_state = "BREAKOUT"
        elif checks.get("pullback_entry"):
            setup_state = "PULLBACK"
        elif setup_is_stale:
            setup_state = "STALE_BREAKOUT"
        else:
            setup_state = "NONE"

        # --------------------------------------------------
        # FINAL QUALIFICATION (Point 15) — a single ordered rule table
        # decides BOTH `qualified` and `entry_state` together, so they
        # can never diverge again. See decision/state_rules.py for the
        # exact rule order and rationale.
        # --------------------------------------------------

        QUALIFY_THRESHOLD = 58.0

        gate_result = evaluate_entry_state(
            trend_state=trend_state,
            unfavorable_trend_state="DOWNTREND",
            tier1_passed=tier1_passed,
            setup_state=setup_state,
            stale_setup_state="STALE_BREAKOUT",
            stale_reason=(
                "Rejected: stale entry (no fresh trigger on a running "
                "breakout), regardless of score."
            ),
            not_overextended=checks["not_overextended"],
            overextended_reason=(
                "Rejected: overextension cap breached (chase risk), "
                "regardless of score."
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

        if gate_result.reject_reason:

            reasons.append(gate_result.reject_reason)

        reasons.append(f"Confidence: {confidence:.2f}%")

        # --------------------------------------------------
        # ACTION
        # --------------------------------------------------

        action = BUY if qualified else NO_TRADE

        # --------------------------------------------------
        # STATE NARRATIVE (Point 15) — entry_state now comes from the
        # SAME rule table that decided `qualified` above, so this
        # narrative can no longer contradict the actual decision.
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
            "BUY Strategy | Action=%s | Confidence=%.2f | Passed=%d/%d | "
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
            fundamental_coverage=fundamental_coverage,
            news_health=round(news_health, 2) if has_news else None,
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
