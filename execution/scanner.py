"""
Institutional Market Scanner - Production Version
Synchronized with WiredOrchestrator Contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import pandas as pd

from core.logger import get_logger

# Fixed Correct Class Imports
from data.market_data import MarketData
from features.feature_engineering import FeatureEngineeringEngine
from strategy.buy_strategy import BuyStrategyEngine
from strategy.sell_strategy import SellStrategyEngine
from strategy.buy_scoring import BuyScoringEngine
from strategy.sell_scoring import SellScoringEngine
from strategy.buy_probability import BuyProbabilityEngine
from strategy.sell_probability import SellProbabilityEngine
from decision.decision_engine import DecisionEngine
from decision.validation_engine import ValidationEngine
from risk.risk_manager import RiskManager
from risk.position_sizing import PositionSizingEngine
from risk.portfolio_rules import PortfolioRulesEngine
from fundamental.fundamental import FundamentalEngine
from news.sentiment_engine import SentimentEngine
from market.market_regime import MarketRegimeEngine
from market.volatility import fetch_india_vix
from data.news_data import NewsDataProvider
from data.delivery_data import DeliveryDataProvider, symbol_without_suffix
from data import liquidity_history
from data.fii_dii_data import FiiDiiDataProvider
from datetime import date as _date
from market import macro_intelligence

logger = get_logger(__name__)


def _signed_news_bias(scored_item: dict[str, Any]) -> float:
    """
    SentimentEngine.evaluate() returns an UNSIGNED 0-100 magnitude in
    "impact_score" (50=weak/neutral, 100=strong) plus a separate polarity
    string in "sentiment" (POSITIVE/NEGATIVE/NEUTRAL) — it does not encode
    direction as a signed number by itself. This converts the pair into a
    signed bias in [-1, +1], so downstream scoring can distinguish good
    news from bad news instead of both maxing out identically.

    This mirrors market_intelligence/market_intelligence_engine.py's
    _signed_bias() (same formula, kept as a separate local copy here to
    avoid a cross-module dependency between two independent subsystems).
    Ported here to fix the previously-documented "news_score always
    clips to 100" limitation (see prior "Known Limitations" note).
    """
    impact = float(scored_item.get("impact_score", 50.0))
    magnitude = max(0.0, (impact - 50.0) / 50.0)  # 0 (neutral) .. 1 (max)
    polarity = scored_item.get("sentiment", "NEUTRAL")
    if polarity == "POSITIVE":
        return magnitude
    if polarity == "NEGATIVE":
        return -magnitude
    return 0.0


@dataclass(slots=True)
class ScanResult:
    symbol: str
    action: str
    score: float
    probability: float
    confidence: float
    ranking: float
    position_size: int
    portfolio_allowed: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


class MarketScanner:
    """
    Master Scanner - Handles final order generation, ranking,
    and session-wide asset analytics for the Orchestrator.
    """

    def __init__(self, disable_live_market_context: bool = False):
        # FIX #4 (architecture review — backtest contamination):
        # analytics/backtest_engine.py builds ONE MarketScanner and
        # reuses it across an entire multi-year day-by-day replay loop.
        # _get_market_headlines()/_get_delivery_data()/_get_fii_dii_data()
        # below are lazy-fetch-once-per-scanner-instance caches — correct
        # for a single real scan run (one live snapshot IS the point),
        # but during a backtest that means whatever real value was live
        # on the day the backtest happened to run gets silently reused
        # for every single simulated historical day, instead of that
        # day's actual historical reading.
        #
        # CORRECTION to an earlier claim in this review thread: VIX was
        # also named as leaking the same way. Re-checked against the
        # actual code before fixing anything — VIX is NOT fetched
        # anywhere in this class's scan path (_evaluate_market_context()
        # never calls fetch_india_vix(); only prepare_orders(), which
        # the backtester does not use, does). risk_manager.py reads
        # `market.get("vix", 20.0)` from the market_state dict passed
        # in by the caller, and backtest_engine.py's market_state never
        # sets "vix" — so backtest VIX is a fixed 20.0 default for the
        # whole run, not a live-today value. Not a leak; that earlier
        # claim was wrong and is corrected here rather than "fixed"
        # (there is nothing to fix for VIX specifically).
        #
        # What IS a genuine, confirmed leak: FII/DII net-flow bias,
        # macro/sector news-headline bias, and NSE delivery-percentage
        # data — all three via the exact lazy-cache pattern above.
        #
        # Fix: an opt-in constructor flag. When True, the three fetch
        # helpers below short-circuit to the SAME "no live data"
        # fallback each already supports for a real fetch failure
        # (empty headlines -> macro_bias 0.0, empty delivery dict ->
        # column left unset, None FII/DII -> nudge skipped) instead of
        # ever calling the live-only fetcher. Default is False, so
        # every existing caller (daily_scan.py, orchestrator.py,
        # paper_trading_engine.py, prepare_orders(), etc. — all
        # construct MarketScanner() with no args) is completely
        # unaffected. Only analytics/backtest_engine.py opts in.
        self._disable_live_market_context = disable_live_market_context

        self._last_full_scan_results: list = []
        # Professional Standard: Use the actual Data Engine for pipeline management, not a single raw dataclass model
        try:
            from data.data_engine import DataEngine
            self.data_engine = DataEngine()
        except ImportError:
            self.data_engine = None
            logger.warning("DataEngine could not be imported. Scanner will rely on direct data injection parameters.")

        self.features = FeatureEngineeringEngine()
        self.buy_strat = BuyStrategyEngine()
        self.sell_strat = SellStrategyEngine()
        self.buy_score = BuyScoringEngine()
        self.sell_score = SellScoringEngine()
        self.buy_prob = BuyProbabilityEngine()
        self.sell_prob = SellProbabilityEngine()
        self.decision_engine = DecisionEngine()
        self.validation = ValidationEngine()
        self.risk = RiskManager()
        self.sizer = PositionSizingEngine()
        self.rules = PortfolioRulesEngine()
        self.fundamental = FundamentalEngine()
        self.sentiment = SentimentEngine()
        self.regime = MarketRegimeEngine()
        self._news_provider = NewsDataProvider()
        self._market_headlines: list[str] | None = None  # lazy-fetched, shared across all symbols in a scan run
        self._delivery_provider = DeliveryDataProvider()
        # lazy-fetched, shared across all symbols in a scan run.
        # {symbol: {field: value}} — see data/delivery_data.py's
        # fetch_latest() docstring for the field list.
        self._delivery_data: dict[str, dict[str, float]] | None = None
        # The ACTUAL trading day self._delivery_data is for — not
        # necessarily today (fetch_latest() walks back if today's
        # bhavcopy isn't published yet). Any code persisting delivery/
        # liquidity data under a date label (e.g. liquidity_history's
        # rolling window, below) MUST use this, never date.today()
        # unconditionally — see fetch_latest()'s docstring for why.
        self._delivery_data_as_of: _date | None = None
        # Rolling multi-day liquidity history (trade-count/turnover/
        # price), loaded once per run and appended to per-symbol as
        # _evaluate_market_context() processes each symbol — see
        # data/liquidity_history.py. Persisted back to disk after each
        # append so a mid-run crash doesn't lose already-processed
        # symbols' updates.
        self._liquidity_history: dict[str, list[dict[str, Any]]] | None = None
        self._fii_dii_provider = FiiDiiDataProvider()
        self._fii_dii_data: dict[str, Any] | None = None
        # Separate from `is None` on the dict above, since a completed
        # fetch that legitimately found nothing is ALSO `None` — this
        # flag is what actually prevents re-hitting NSE once per symbol.
        self._fii_dii_fetched: bool = False

        logger.info("Market Scanner Engine initialized under professional pipeline contracts.")

    def prepare_orders(
        self, decision: Any, signals: dict[str, Any], portfolio: dict[str, Any]
    ) -> list[ScanResult]:
        """
        Hard entrypoint matching the exact call inside orchestrator.py (Step 7).
        Processes decision signals, calculates sizing, applies portfolio rules,
        and ranks candidates.
        """
        logger.info(
            "Orchestrator contract callback: Preparing and ranking market orders."
        )

        # Fallback to general scanner loop if orchestration context needs raw symbols extraction
        symbols = (
            list(signals.get("buy", {}).keys())
            if isinstance(signals.get("buy"), dict)
            else []
        )
        if not symbols:
            return []

        # Dummy/Mock state matching for global evaluation criteria
        broker_status = {"status": "ONLINE", "connected": True, "order_allowed": True, "available_margin": 100000.0}
        market_state = {
            "max_trade_candidates": 20,
            "max_watchlist": 50,
            "market_open": True,
            "holiday": False,
            "vix": fetch_india_vix(),
        }

        return self.scan_symbols(
            symbols=symbols,
            portfolio=portfolio,
            broker_status=broker_status,
            market_state=market_state,
        )

    def _get_market_headlines(self) -> list[str]:
        """Fetch broad market/macro headlines once per scan run and cache
        them (not per-symbol — one shared macro news snapshot is enough,
        and avoids hammering the API 500 times)."""
        # FIX #4: see the disable_live_market_context NOTE in __init__ —
        # short-circuit to the already-supported "no headlines" fallback
        # instead of fetching live data that would leak into a backtest.
        if self._disable_live_market_context:
            return []
        if self._market_headlines is None:
            try:
                self._market_headlines = self._news_provider.fetch_market_news()
            except Exception as exc:
                logger.warning("Market news fetch failed: %s", exc)
                self._market_headlines = []
        return self._market_headlines

    def _get_delivery_data(self) -> dict[str, dict[str, float]]:
        """Fetch the NSE-wide delivery/liquidity bhavcopy once per scan
        run and cache it (same reasoning as _get_market_headlines() —
        one file covers every symbol, so there's no reason to hit NSE
        once per symbol). Returns {} on total failure; callers must
        treat that as "no live data" and fall back gracefully, not
        substitute a fabricated number."""
        # FIX #4: see the disable_live_market_context NOTE in __init__.
        if self._disable_live_market_context:
            return {}
        if self._delivery_data is None:
            try:
                self._delivery_data, self._delivery_data_as_of = self._delivery_provider.fetch_latest()
            except Exception as exc:
                logger.warning("Delivery data fetch failed: %s", exc)
                self._delivery_data = {}
                self._delivery_data_as_of = None
        return self._delivery_data

    def _get_liquidity_history(self) -> dict[str, list[dict[str, Any]]]:
        """Load the persisted rolling liquidity-history file once per
        scan run (same lazy-cache reasoning as _get_delivery_data() —
        one file, read once, mutated in-memory per symbol as the run
        progresses)."""
        if self._disable_live_market_context:
            return {}
        if self._liquidity_history is None:
            self._liquidity_history = liquidity_history.load_history()
        return self._liquidity_history

    def _get_fii_dii_data(self) -> dict[str, Any] | None:
        """Fetch NSE-wide FII/DII net activity once per scan run and
        cache it (same reasoning as _get_delivery_data() — one market-
        wide value covers every symbol). Returns None when unavailable;
        callers must treat that as "no signal" and skip the nudge, not
        substitute a fabricated bias."""
        # FIX #4: see the disable_live_market_context NOTE in __init__.
        if self._disable_live_market_context:
            return None
        if not self._fii_dii_fetched:
            try:
                self._fii_dii_data = self._fii_dii_provider.fetch_latest()
            except Exception as exc:
                logger.warning("FII/DII data fetch failed: %s", exc)
                self._fii_dii_data = None
            self._fii_dii_fetched = True
        return self._fii_dii_data

    def _evaluate_market_context(self, symbol: str, bundle: Any = None) -> dict[str, Any]:
        """
        SINGLE SOURCE OF TRUTH for market analysis: data fetch, feature
        engineering, fundamentals/news/regime scoring, BUY/SELL strategy
        evaluation, scoring, probability, and the final decision.

        Used by BOTH:
          - scan_symbol() (new-entry path — unchanged behavior)
          - evaluate_position() (monitoring path — existing positions only)

        This is a pure extraction of what was previously inlined at the
        top of scan_symbol() — no scoring/probability/confidence/decision
        logic was changed, only moved, so both callers get IDENTICAL
        market analysis with zero duplicated logic.
        """
        diagnostics: dict[str, Any] = {}

        # 1. DOWNLOAD DATA (market OHLCV + fundamentals + news, in one bundle)
        # A caller (e.g. the backtester, replaying history day-by-day)
        # can pass a pre-fetched, correctly time-sliced `bundle` here to
        # skip the live fetch entirely — this is what makes real
        # historical backtesting possible instead of every simulated
        # day silently re-fetching today's live data.
        if bundle is None:
            if self.data_engine is None:
                raise ValueError("DataEngine unavailable; cannot fetch market data.")
            bundle = self.data_engine.fetch(symbol=symbol)

        dataframe = bundle.market
        if dataframe is None or dataframe.empty:
            raise ValueError("No market data received.")

        diagnostics["candles"] = len(dataframe)
        diagnostics["symbol"] = symbol
        diagnostics["sector"] = bundle.fundamentals.get("sector") if bundle.fundamentals else None
        diagnostics["industry"] = bundle.fundamentals.get("industry") if bundle.fundamentals else None
        diagnostics["highest"] = round(float(dataframe["high"].max()), 2)
        diagnostics["lowest"] = round(float(dataframe["low"].min()), 2)

        # 2. FEATURE ENGINEERING
        dataframe = self.features.generate(dataframe)
        latest = dataframe.iloc[-1]
        diagnostics["latest_close"] = round(float(latest["close"]), 2)
        diagnostics["latest_high"] = round(float(latest["high"]), 2)
        diagnostics["latest_low"] = round(float(latest["low"]), 2)

        # 2a. DELIVERY PERCENTAGE — was previously ALWAYS absent from
        # this dataframe, so validation_engine.py's
        # `latest.get("delivery_percentage", 100.0)` liquidity check
        # could never fail (100.0 >= 20.0 always). Only set the column
        # when a real NSE reading is available for this symbol; if the
        # fetch failed or this symbol isn't covered, leave the column
        # unset so validation_engine.py's own default/fallback behavior
        # is unchanged (no regression when live data is unreachable).
        #
        # PHASE 18: deliv_lookup values are now {field: value} dicts
        # (previously a bare float) — see data/delivery_data.py's
        # fetch_latest() docstring. delivery_percentage wiring below is
        # unchanged in behavior, just reads it out of the dict.
        deliv_lookup = self._get_delivery_data()
        symbol_key = symbol_without_suffix(symbol)
        deliv_fields = deliv_lookup.get(symbol_key)
        # PHASE 18d: the day this data is ACTUALLY for — fetch_latest()
        # walks back to an earlier trading day if today's bhavcopy isn't
        # published yet (NSE can run late). Surfaced here so the report
        # shows when delivery/liquidity numbers are stale, instead of
        # silently presenting an older day's data as if it were today's.
        bhavcopy_as_of = self._delivery_data_as_of

        deliv_value = deliv_fields.get("delivery_percent") if deliv_fields else None
        if deliv_value is not None:
            dataframe.loc[dataframe.index[-1], "delivery_percentage"] = deliv_value
            diagnostics["delivery_percentage"] = deliv_value
        if bhavcopy_as_of is not None:
            diagnostics["bhavcopy_as_of"] = bhavcopy_as_of.isoformat()
            diagnostics["bhavcopy_stale"] = bhavcopy_as_of != _date.today()

        # 2b. ROLLING LIQUIDITY HISTORY — the bhavcopy row fetched above
        # (deliv_fields) also carries ttl_trd_qnty/no_of_trades/
        # turnover_lacs/close_price/prev_close. A single day's numbers
        # can't tell a "broad-participation" trading day apart from a
        # "block-deal-driven" one on their own — that needs comparing
        # today against this symbol's OWN recent history, the same way
        # volume_sma_20 does for volume. Each run appends today's
        # numbers to a small persisted history file (data/
        # liquidity_history.py) and computes the rolling comparison from
        # it; the file is saved after every symbol so a mid-run crash
        # doesn't lose already-processed symbols. Cold-start note: for
        # the first MIN_HISTORY_DAYS days after this ships (and for any
        # newly-added symbol), there isn't enough history yet — the
        # columns below are simply left unset and _liquidity_score()
        # falls back to its existing volume-only behavior, exactly as if
        # this feature weren't present, rather than trusting a
        # comparison built from almost no data.
        # PHASE 18d FIX: this used to unconditionally label the history
        # entry with _date.today() — but deliv_fields can legitimately
        # be an EARLIER day's data (fetch_latest()'s walk-back fallback
        # when today's bhavcopy isn't published yet). Labeling that as
        # "today" would silently create the exact same kind of
        # duplicate/mislabeled entry Phase 18c's DATE1 validation was
        # built to catch — just introduced here instead of by NSE. Use
        # bhavcopy_as_of (the real date fetch_latest() returned) so a
        # late-bhavcopy day correctly REPLACES that earlier date's
        # existing entry (append_and_prune() is same-date-safe) instead
        # of manufacturing a phantom "today" entry with yesterday's
        # numbers.
        if deliv_fields and bhavcopy_as_of is not None and not self._disable_live_market_context:
            history = self._get_liquidity_history()
            liquidity_history.append_and_prune(history, {symbol_key: deliv_fields}, bhavcopy_as_of)
            liquidity_history.save_history(history)

            today_quality = liquidity_history.today_trade_quality(deliv_fields)
            rolling = liquidity_history.rolling_liquidity_stats(history, symbol_key)

            if "avg_trade_size" in today_quality:
                dataframe.loc[dataframe.index[-1], "avg_trade_size_today"] = today_quality["avg_trade_size"]
                diagnostics["avg_trade_size_today"] = round(today_quality["avg_trade_size"], 2)
            if "amihud" in today_quality:
                dataframe.loc[dataframe.index[-1], "amihud_today"] = today_quality["amihud"]

            if rolling is not None:
                if "avg_trade_size" in rolling:
                    dataframe.loc[dataframe.index[-1], "avg_trade_size_20d"] = rolling["avg_trade_size"]
                    diagnostics["avg_trade_size_20d"] = round(rolling["avg_trade_size"], 2)
                if "avg_amihud" in rolling:
                    dataframe.loc[dataframe.index[-1], "avg_amihud_20d"] = rolling["avg_amihud"]
                dataframe.loc[dataframe.index[-1], "liquidity_window_days"] = rolling["window_size"]
                diagnostics["liquidity_window_days"] = int(rolling["window_size"])

        # 2c. FUNDAMENTALS / NEWS SENTIMENT / MARKET REGIME
        # These feed the strategy + scoring engines (news_score / market_score /
        # sector_score are 0-100 normalized inputs).
        fundamentals = bundle.fundamentals or {}

        news_items = self.sentiment.evaluate(bundle.news or [])
        if news_items:
            # Fixed: previously averaged the raw (unsigned) impact_score
            # directly, which mathematically always clipped news_score
            # to 100 regardless of whether the news was positive or
            # negative (proven via static + runtime audit). Now uses
            # the signed bias (magnitude + polarity combined), so
            # negative news correctly pulls the score down instead of
            # being indistinguishable from positive news.
            avg_bias = sum(_signed_news_bias(i) for i in news_items) / len(news_items)
            news_score = max(0.0, min(100.0, 50.0 + avg_bias * 50.0))
        else:
            news_score = None  # genuinely no news -> must NOT interfere with score/confidence/probability

        # Macro/geopolitical sector bias (e.g. "Strait of Hormuz closed"
        # -> Energy/Defence positive, Airlines/Paints negative). This is
        # a nudge on top of company-specific news, not a replacement.
        macro_bias = macro_intelligence.sector_bias(
            self._get_market_headlines(), diagnostics.get("sector")
        )
        if macro_bias != 0.0:
            # A macro event counts as "news" even if this specific
            # company had none — it's a real, directional signal.
            base = news_score if news_score is not None else 50.0
            news_score = max(0.0, min(100.0, base + macro_bias * 20.0))
        diagnostics["macro_bias"] = macro_bias
        diagnostics["news_score"] = news_score if news_score is not None else ""
        diagnostics["has_news"] = news_score is not None

        dataframe = self.regime.evaluate(dataframe)
        latest = dataframe.iloc[-1]
        latest_regime = latest["market_regime"]
        market_score = {"BULL": 75.0, "SIDEWAYS": 50.0, "BEAR": 25.0}.get(
            latest_regime, 50.0
        )
        diagnostics["market_regime"] = latest_regime

        # FII/DII INSTITUTIONAL FLOW — market-wide context nudge on top
        # of the per-stock regime score above, not a replacement for it.
        # This automatically reaches BOTH buy_strat and sell_strat below
        # (sell_strategy.py already inverts market_score for its own
        # tier3 calc), so heavy institutional buying nudges BUY
        # confidence up / SELL confidence down and vice versa, with no
        # separate BUY/SELL-side code needed. Weight (10.0) is kept
        # deliberately smaller than macro_bias's nudge on news_score
        # (20.0, see below) — market_score already carries a strong
        # per-stock trend signal; FII/DII is secondary context on top
        # of it, not the primary driver.
        fii_dii = self._get_fii_dii_data()
        if fii_dii is not None:
            fii_dii_bias = fii_dii.get("bias", 0.0)
            market_score = max(0.0, min(100.0, market_score + fii_dii_bias * 10.0))
            diagnostics["fii_dii_bias"] = fii_dii_bias
            diagnostics["fii_dii_net_cr"] = fii_dii.get("combined_net_cr")

        # Internal passthrough (not used by the report) so callers
        # like the Paper Trading Engine can re-evaluate an existing
        # position (via ExitEngine) using the SAME already-computed
        # dataframe/fundamentals/news_score, instead of re-fetching
        # or re-deriving them.
        diagnostics["_dataframe"] = dataframe
        diagnostics["_fundamentals"] = fundamentals
        diagnostics["_news_score"] = news_score
        diagnostics["market_score"] = market_score

        # Raw indicator snapshot (latest row) for reporting.
        diagnostics["ema_20"] = round(float(latest.get("ema_20", 0) or 0), 2)
        diagnostics["ema_50"] = round(float(latest.get("ema_50", 0) or 0), 2)
        diagnostics["ema_200"] = round(float(latest.get("ema_200", 0) or 0), 2)
        diagnostics["rsi_14"] = round(float(latest.get("rsi_14", 0) or 0), 2)
        diagnostics["macd"] = round(float(latest.get("macd", 0) or 0), 2)
        diagnostics["adx_14"] = round(float(latest.get("adx_14", 0) or 0), 2)
        diagnostics["atr_14"] = round(float(latest.get("atr_14", 0) or 0), 2)
        vol = float(latest.get("volume", 0) or 0)
        vol_sma = float(latest.get("volume_sma_20", 0) or 0)
        diagnostics["volume_ratio"] = round(vol / vol_sma, 2) if vol_sma else 0.0
        # Renamed from "relative_strength" (misleading — this is close vs
        # the stock's OWN 20-day mean, not vs a benchmark/index; see the
        # NOTE at features/indicators/breakout.py's price_vs_20d_mean).
        # diagnostics key renamed too; the CSV report's "RelativeStrength"
        # COLUMN HEADER is left unchanged on purpose (see
        # scripts/generate_full_report.py's FIELDNAMES comment — it's the
        # user's own fixed/locked report schema) — only the internal
        # source-of-truth key is corrected here.
        diagnostics["price_vs_20d_mean"] = round(
            float(latest.get("price_vs_20d_mean", 0) or 0), 2
        )
        diagnostics["is_breakout"] = bool(latest.get("is_breakout", False))
        diagnostics["is_pullback"] = bool(latest.get("is_pullback", False))
        diagnostics["stoch_k"] = round(float(latest.get("stoch_k", 0) or 0), 2)
        diagnostics["cmf_20"] = round(float(latest.get("cmf_20", 0) or 0), 2)
        diagnostics["mfi_14"] = round(float(latest.get("mfi_14", 0) or 0), 2)
        diagnostics["supertrend"] = bool(latest.get("supertrend", False))
        diagnostics["cloud_trend"] = latest.get("cloud_trend", "")
        diagnostics["bullish_engulfing"] = bool(latest.get("bullish_engulfing", False))
        diagnostics["bearish_engulfing"] = bool(latest.get("bearish_engulfing", False))

        # FIX #8 (architecture review — sector/breadth placeholders):
        # was `sector_score = 50.0`. Sector rotation needs a cross-symbol
        # sector-index dataframe (see market/sector_rotation.py's
        # SectorRotationEngine — the engine already exists, it's just
        # never fed real sector-index data by this per-symbol scan) that
        # this per-symbol scan does not have available. A fabricated
        # 50.0 was indistinguishable downstream from "measured and
        # genuinely neutral" — None instead, mirroring the has_news /
        # news_score=None convention already used above in this same
        # method, so every consumer can tell "unavailable" apart from
        # "actually neutral" and redistribute weight instead of quietly
        # diluting toward 50.
        sector_score = None

        # FIX #8: was `dataframe["breadth"] = 50.0`. Same underlying gap
        # as sector_score above — market breadth needs market-wide
        # advance/decline data (see market/market_breadth.py's
        # MarketBreadthEngine, also unwired) that isn't fetched anywhere
        # in this per-symbol pipeline. None instead of a fabricated
        # 50.0.
        #
        # BONUS BUG found while fixing this: the OLD value was a FLOAT
        # (50.0), but strategy/buy_strategy.py's/sell_strategy.py's
        # Tier2 check does `row.get("breadth", "NEUTRAL") == "STRONG"` —
        # a float can never equal that string, so checks["breadth"] was
        # unconditionally False in every real scan (not just "neutral",
        # literally always-false), independent of this None-based fix.
        # Confirmed via direct read of both strategy files' breadth
        # check before touching this line.
        dataframe["breadth"] = None

        # 3. STRATEGIES EVALUATION
        buy_decision = self.buy_strat.evaluate(
            dataframe=dataframe,
            fundamentals=fundamentals,
            news_score=news_score,
            market_score=market_score,
            sector_score=sector_score,
        )
        sell_decision = self.sell_strat.evaluate(
            dataframe=dataframe,
            fundamentals=fundamentals,
            news_score=news_score,
            market_score=market_score,
            sector_score=sector_score,
        )
        diagnostics["buy_signal"] = buy_decision.action
        diagnostics["sell_signal"] = sell_decision.action

        # EXPLAINABILITY: full tier breakdown for both engines, for
        # every outcome (BUY/SELL/NO_TRADE) — not just the winner.
        diagnostics["buy_tier1_checks"] = buy_decision.tier1_checks
        diagnostics["buy_tier1_passed"] = buy_decision.tier1_passed
        diagnostics["buy_tier2_score"] = buy_decision.tier2_score
        diagnostics["buy_tier3_score"] = buy_decision.tier3_score
        diagnostics["buy_overall_score"] = buy_decision.overall_score
        diagnostics["buy_qualify_threshold"] = buy_decision.qualify_threshold
        diagnostics["buy_fundamental_health"] = buy_decision.fundamental_health
        # How much of the fundamental_health number above is actually
        # backed by data (1.0 = all 8 metrics present, 0.0 = none) — see
        # strategy/fundamental_scoring.py's FundamentalEvidence. Exposed
        # for visibility/audit only; no gating logic added here.
        diagnostics["buy_fundamental_coverage"] = buy_decision.fundamental_coverage
        diagnostics["buy_news_health"] = buy_decision.news_health
        # FIX #15 (architecture review — state-based structure): a
        # human-readable "MarketState=.../TrendState=.../SetupState=...
        # /EntryState=..." narrative, purely descriptive of the checks/
        # scores already above — see BuyDecision.state_narrative's NOTE.
        diagnostics["buy_state_narrative"] = buy_decision.state_narrative
        # FIX #10/#16 (architecture review — volume-pressure model):
        # whether buy_decision.volume_factor_score (part of tier2_score)
        # includes real NSE delivery-percentage data — see
        # strategy/buy_strategy.py's volume_factor_score NOTE.
        diagnostics["buy_volume_pressure_uses_delivery"] = buy_decision.volume_pressure_uses_delivery

        diagnostics["sell_tier1_checks"] = sell_decision.tier1_checks
        diagnostics["sell_tier1_passed"] = sell_decision.tier1_passed
        diagnostics["sell_tier2_score"] = sell_decision.tier2_score
        diagnostics["sell_tier3_score"] = sell_decision.tier3_score
        diagnostics["sell_overall_score"] = sell_decision.overall_score
        diagnostics["sell_qualify_threshold"] = sell_decision.qualify_threshold
        diagnostics["sell_fundamental_weakness"] = sell_decision.fundamental_weakness
        diagnostics["sell_fundamental_coverage"] = sell_decision.fundamental_coverage
        diagnostics["sell_news_negativity"] = sell_decision.news_negativity
        diagnostics["sell_state_narrative"] = sell_decision.state_narrative
        diagnostics["sell_volume_pressure_uses_delivery"] = sell_decision.volume_pressure_uses_delivery
        diagnostics["buy_checks_passed"] = sum(
            bool(v) for v in buy_decision.technical_checks.values()
        )
        diagnostics["buy_checks_total"] = len(buy_decision.technical_checks)
        diagnostics["sell_checks_passed"] = sum(
            bool(v) for v in sell_decision.technical_checks.values()
        )
        diagnostics["sell_checks_total"] = len(sell_decision.technical_checks)
        # Per-rule pass/fail (e.g. {"rsi_oversold": True, ...}) — pure
        # addition alongside the aggregate counts above, needed so the
        # Learning Engine can eventually correlate INDIVIDUAL rules
        # (not just aggregate technical score) against real outcomes.
        # bool(v) here matters: technical_checks values often come from
        # pandas/numpy comparisons (e.g. df['rsi'] < 30), which return
        # numpy.bool_ — NOT JSON-serializable by json.dumps() downstream
        # in generate_full_report.py. Cast to native Python bool at the
        # source so nothing further down the pipeline needs to know
        # about this numpy quirk.
        diagnostics["buy_technical_checks"] = {k: bool(v) for k, v in buy_decision.technical_checks.items()}
        diagnostics["sell_technical_checks"] = {k: bool(v) for k, v in sell_decision.technical_checks.items()}
        diagnostics["buy_decision_confidence"] = round(buy_decision.confidence, 2)
        diagnostics["sell_decision_confidence"] = round(sell_decision.confidence, 2)

        # 4. SCORING MATRIX
        # buy_decision/sell_decision (already computed above by
        # BuyStrategyEngine/SellStrategyEngine) are now passed through so
        # BuyScoringEngine/SellScoringEngine can delegate their technical
        # component to tier2_score instead of an independent duplicate
        # implementation — see the NOTE in strategy/buy_scoring.py.
        buy_score = self.buy_score.score(
            dataframe=dataframe,
            fundamentals=fundamentals,
            news_score=news_score,
            market_score=market_score,
            sector_score=sector_score,
            buy_decision=buy_decision,
        )
        sell_score = self.sell_score.score(
            dataframe=dataframe,
            fundamentals=fundamentals,
            news_score=news_score,
            market_score=market_score,
            sector_score=sector_score,
            sell_decision=sell_decision,
        )
        diagnostics["buy_score"] = round(buy_score.overall, 2)
        diagnostics["sell_score"] = round(sell_score.overall, 2)
        diagnostics["buy_technical_score"] = round(buy_score.technical, 2)
        diagnostics["buy_fundamental_score"] = round(buy_score.fundamental, 2)
        diagnostics["buy_news_score"] = round(buy_score.news, 2)
        diagnostics["sell_technical_score"] = round(sell_score.technical, 2)

        # 5. PROBABILITY ENGINES
        buy_probability = self.buy_prob.evaluate(score=buy_score)
        sell_probability = self.sell_prob.evaluate(score=sell_score)
        diagnostics["buy_probability"] = round(buy_probability.win_probability, 2)
        diagnostics["sell_probability"] = round(
            sell_probability.success_probability, 2
        )

        # 6. DECISION ENGINE
        final_decision = self.decision_engine.evaluate(
            buy_decision=buy_decision,
            sell_decision=sell_decision,
            buy_score=buy_score,
            sell_score=sell_score,
            buy_probability=buy_probability,
            sell_probability=sell_probability,
        )
        diagnostics["decision"] = final_decision.action
        diagnostics["ranking"] = round(final_decision.ranking, 2)
        diagnostics["confidence"] = round(final_decision.confidence, 2)
        diagnostics["decision_reasons"] = " | ".join(final_decision.reasons)
        diagnostics["expected_return"] = round(final_decision.expected_return, 2)
        diagnostics["expected_drawdown"] = round(final_decision.expected_drawdown, 2)
        diagnostics["expected_hold_days"] = final_decision.expected_hold_days
        diagnostics["trade_grade"] = final_decision.diagnostics.get("trade_grade", "")

        return {
            "dataframe": dataframe,
            "fundamentals": fundamentals,
            "news_score": news_score,
            "final_decision": final_decision,
            "diagnostics": diagnostics,
        }

    def _compute_stop_loss_targets(
        self, direction: str, close_price: float, atr: float
    ) -> tuple[float, float, float, float]:
        """
        Shared stop-loss/target projection — same ATR multipliers as
        risk/exit_strategy.py's ExitStrategyEngine (ATR_STOP=2.0,
        PARTIAL_TARGET=2.0, FINAL_TARGET=3.5). `direction` is BUY or
        SELL — callers pass today's fresh signal (scan_symbol, entry)
        or the ALREADY-HELD position's direction (evaluate_position,
        monitoring), whichever is the relevant "am I long or short"
        context for that caller.
        """
        ATR_STOP = 2.0
        PARTIAL_TARGET = 2.0
        FINAL_TARGET = 3.5

        if atr and close_price:
            if direction == "SELL":
                stop_loss = round(close_price + ATR_STOP * atr, 2)
                target1 = round(close_price - PARTIAL_TARGET * atr, 2)
                target2 = round(close_price - FINAL_TARGET * atr, 2)
            else:
                stop_loss = round(close_price - ATR_STOP * atr, 2)
                target1 = round(close_price + PARTIAL_TARGET * atr, 2)
                target2 = round(close_price + FINAL_TARGET * atr, 2)
            risk = abs(close_price - stop_loss)
            reward = abs(target1 - close_price)
            risk_reward = round(reward / risk, 2) if risk else 0.0
        else:
            stop_loss = target1 = target2 = risk_reward = 0.0
        return stop_loss, target1, target2, risk_reward

    def scan_symbol(
        self,
        symbol: str,
        portfolio: dict[str, Any],
        broker_status: dict[str, Any],
        market_state: dict[str, Any],
        bundle: Any = None,
    ) -> ScanResult:
        logger.info("Scanning asset node: %s", symbol)
        diagnostics = {}

        try:
            context = self._evaluate_market_context(symbol, bundle=bundle)
            dataframe = context["dataframe"]
            fundamentals = context["fundamentals"]
            news_score = context["news_score"]
            final_decision = context["final_decision"]
            diagnostics.update(context["diagnostics"])
            latest = dataframe.iloc[-1]

            # 7. VALIDATION ENGINE
            validation = self.validation.validate(
                decision=final_decision,
                dataframe=dataframe,
                portfolio=portfolio,
                broker_status=broker_status,
                market_state=market_state,
            )
            diagnostics["validation_passed"] = validation.passed
            diagnostics["validation_action"] = validation.action
            diagnostics["validation_warnings"] = len(validation.warnings)
            diagnostics["validation_rejection_reason"] = validation.rejection_reason

            if not validation.passed:
                logger.info("%s rejected by Validation Engine.", symbol)

            # 8. RISK MANAGER
            risk_result = self.risk.evaluate(
                validation=validation,
                decision=final_decision,
                dataframe=dataframe,
                portfolio=portfolio,
                market=market_state,
            )
            diagnostics["risk_safe"] = risk_result.safe
            diagnostics["risk_grade"] = risk_result.risk_grade
            diagnostics["total_risk"] = round(risk_result.total_risk, 2)

            # 9. POSITION SIZING
            position_result = self.sizer.calculate(
                decision=final_decision,
                validation=validation,
                risk=risk_result,
                dataframe=dataframe,
                portfolio=portfolio,
            )
            diagnostics["quantity"] = position_result.quantity
            diagnostics["position_value"] = round(position_result.position_value, 2)
            diagnostics["allocation"] = round(position_result.allocation_percent, 4)

            # 10. PORTFOLIO RULES
            # average_daily_value (stock's own liquidity, volume x price)
            # is specific to THIS symbol, not portfolio-wide state — inject
            # it into a copy of the portfolio dict so the liquidity
            # participation check has real data instead of defaulting to 0.
            portfolio_with_liquidity = dict(portfolio)
            portfolio_with_liquidity["average_daily_value"] = (
                latest.get("volume_sma_20", 0.0) * latest.get("close", 0.0)
            )

            portfolio_result = self.rules.evaluate(
                decision=final_decision,
                validation=validation,
                risk=risk_result,
                sizing=position_result,
                portfolio=portfolio_with_liquidity,
            )
            diagnostics["portfolio_allowed"] = portfolio_result.allowed
            diagnostics["portfolio_score"] = round(portfolio_result.portfolio_score, 2)
            diagnostics["portfolio_rule_reason"] = getattr(
                portfolio_result, "rejection_reason", None
            ) or "OK"

            # Stop-loss / targets use the SAME ATR multipliers as
            # risk/exit_strategy.py's ExitStrategyEngine (ATR_STOP=2.0,
            # PARTIAL_TARGET=2.0, FINAL_TARGET=3.5), so these scan-time
            # projections match what that engine will compute once a
            # position is actually open. Shared with evaluate_position()
            # via _compute_stop_loss_targets() — see that method.
            stop_loss, target1, target2, risk_reward = self._compute_stop_loss_targets(
                direction=final_decision.action,
                close_price=diagnostics.get("latest_close", 0.0),
                atr=diagnostics.get("atr_14", 0.0),
            )
            diagnostics["stop_loss"] = stop_loss
            diagnostics["target1"] = target1
            diagnostics["target2"] = target2
            diagnostics["risk_reward"] = risk_reward

            # RESOLVE FINAL SCORES
            # NOTE: even for NO_TRADE, report the stronger side's ACTUAL
            # computed score/probability (not a forced 0). Zeroing these
            # out made it impossible to audit *why* a stock was rejected
            # (e.g. "BUY score 68, still NO_TRADE" is useful; "score 0"
            # hides that entirely).
            if final_decision.action == "BUY":
                final_score = final_decision.buy_score
                probability = final_decision.buy_probability
            elif final_decision.action == "SELL":
                final_score = final_decision.sell_score
                probability = final_decision.sell_probability
            elif final_decision.buy_score >= final_decision.sell_score:
                final_score = final_decision.buy_score
                probability = final_decision.buy_probability
            else:
                final_score = final_decision.sell_score
                probability = final_decision.sell_probability

            return ScanResult(
                symbol=symbol,
                action=final_decision.action,
                score=round(final_score, 2),
                probability=round(probability, 2),
                confidence=round(final_decision.confidence, 2),
                ranking=round(final_decision.ranking, 2),
                position_size=position_result.quantity,
                portfolio_allowed=portfolio_result.allowed,
                diagnostics=diagnostics,
            )

        except Exception as exc:
            logger.exception("Scanner compilation error for %s", symbol)
            diagnostics["error"] = str(exc)
            return ScanResult(
                symbol=symbol,
                action="ERROR",
                score=0.0,
                probability=0.0,
                confidence=0.0,
                ranking=0.0,
                position_size=0,
                portfolio_allowed=False,
                diagnostics=diagnostics,
            )

    def evaluate_position(
        self,
        symbol: str,
        position: dict[str, Any],
        portfolio: dict[str, Any],
        broker_status: dict[str, Any],
        market_state: dict[str, Any],
        bundle: Any = None,
    ) -> ScanResult:
        """
        MONITORING-ONLY evaluation for an EXISTING open position.

        Reuses _evaluate_market_context() — the exact same market
        analysis (data, features, fundamentals/news/regime, BUY/SELL
        strategy, scoring, probability, decision) as scan_symbol() — so
        there is zero duplicated scoring/probability/confidence/decision
        logic between the entry and monitoring paths.

        Unlike scan_symbol(), this method NEVER runs entry-only
        validations:
          - duplicate_position   (this IS an existing position — not a
                                   duplicate, by definition)
          - max_positions        (we are not adding a new slot)
          - capital allocation for opening a NEW trade
          - new-entry portfolio limits (PositionSizingEngine /
            PortfolioRulesEngine are entry-sizing concerns — not called
            here at all)

        It still runs ValidationEngine (for genuine data-quality checks
        — NaN, market hours, minimum history, average volume, etc. —
        which ARE relevant while monitoring) and RiskManager (for real
        market/volatility/portfolio risk metrics), but with `symbol`
        excluded from the `open_positions` dict passed to them, so the
        entry-only checks above correctly evaluate as if this position
        didn't need "room" to be opened — because it already IS open.
        """
        logger.info("Evaluating existing position: %s", symbol)
        diagnostics: dict[str, Any] = {}

        try:
            context = self._evaluate_market_context(symbol, bundle=bundle)
        except Exception as exc:
            logger.exception("Data Fetch stage failed for %s", symbol)
            diagnostics["error"] = str(exc)
            diagnostics["error_type"] = type(exc).__name__
            diagnostics["error_stage"] = "Data Fetch"
            return ScanResult(
                symbol=symbol, action="ERROR", score=0.0, probability=0.0,
                confidence=0.0, ranking=0.0, position_size=0,
                portfolio_allowed=False, diagnostics=diagnostics,
            )

        try:
            dataframe = context["dataframe"]
            final_decision = context["final_decision"]
            diagnostics.update(context["diagnostics"])

            # Exclude THIS symbol from open_positions so duplicate_position
            # / max_positions evaluate correctly for a MONITORING check
            # (see docstring above) — everything else about the portfolio
            # (cash, other positions, exposure) stays real/unchanged.
            monitoring_portfolio = dict(portfolio)
            monitoring_portfolio["open_positions"] = {
                sym: pos
                for sym, pos in portfolio.get("open_positions", {}).items()
                if sym != symbol
            }

            validation = self.validation.validate(
                decision=final_decision,
                dataframe=dataframe,
                portfolio=monitoring_portfolio,
                broker_status=broker_status,
                market_state=market_state,
                skip_position_count=True,
            )
            diagnostics["validation_passed"] = validation.passed
            diagnostics["validation_action"] = validation.action
            diagnostics["validation_warnings"] = len(validation.warnings)
            diagnostics["validation_rejection_reason"] = validation.rejection_reason

            risk_result = self.risk.evaluate(
                validation=validation,
                decision=final_decision,
                dataframe=dataframe,
                portfolio=monitoring_portfolio,
                market=market_state,
            )
            diagnostics["risk_safe"] = risk_result.safe
            diagnostics["risk_grade"] = risk_result.risk_grade
            diagnostics["total_risk"] = round(risk_result.total_risk, 2)

            # Stop-loss uses the POSITION'S HELD direction (not today's
            # fresh signal) — monitoring cares about "given I am long
            # this stock, where is my stop", regardless of what a brand
            # new scan would decide today.
            held_direction = position.get("direction", "BUY")
            stop_loss, target1, target2, risk_reward = self._compute_stop_loss_targets(
                direction=held_direction,
                close_price=diagnostics.get("latest_close", 0.0),
                atr=diagnostics.get("atr_14", 0.0),
            )
            diagnostics["stop_loss"] = stop_loss
            diagnostics["target1"] = target1
            diagnostics["target2"] = target2
            diagnostics["risk_reward"] = risk_reward

            return ScanResult(
                symbol=symbol,
                action=final_decision.action,
                score=round(final_decision.confidence, 2),
                probability=round(
                    final_decision.buy_probability
                    if held_direction == "BUY" else final_decision.sell_probability,
                    2,
                ),
                confidence=round(final_decision.confidence, 2),
                ranking=round(final_decision.ranking, 2),
                position_size=0,          # not applicable — not sizing a new entry
                portfolio_allowed=False,  # not applicable — see docstring
                diagnostics=diagnostics,
            )

        except Exception as exc:
            logger.exception("Evaluation stage failed for %s", symbol)
            diagnostics["error"] = str(exc)
            diagnostics["error_type"] = type(exc).__name__
            diagnostics["error_stage"] = "Evaluation"
            return ScanResult(
                symbol=symbol,
                action="ERROR",
                score=0.0,
                probability=0.0,
                confidence=0.0,
                ranking=0.0,
                position_size=0,
                portfolio_allowed=False,
                diagnostics=diagnostics,
            )

    def scan_symbols(
        self,
        symbols: list[str],
        portfolio: dict[str, Any],
        broker_status: dict[str, Any],
        market_state: dict[str, Any],
        bundles: dict[str, Any] | None = None,
    ) -> list[ScanResult]:
        logger.info("Starting scan pass of %d target nodes.", len(symbols))
        results: list[ScanResult] = []
        total = len(symbols)
        bundles = bundles or {}

        for index, symbol in enumerate(symbols, start=1):
            logger.info("[%d/%d] Sizing target context: %s", index, total, symbol)
            result = self.scan_symbol(
                symbol=symbol,
                portfolio=portfolio,
                broker_status=broker_status,
                market_state=market_state,
                bundle=bundles.get(symbol),
            )
            results.append(result)

        valid_results = [r for r in results if r.action != "ERROR"]
        executable_results = [
            r
            for r in valid_results
            if r.portfolio_allowed and r.action in ("BUY", "SELL")
        ]

        # Rank Results top-down
        executable_results.sort(
            key=lambda r: (r.ranking, r.confidence, r.score, r.probability),
            reverse=True,
        )

        for rank, result in enumerate(executable_results, start=1):
            result.diagnostics["rank"] = rank
            result.diagnostics["scanner_score"] = round(
                (
                    result.ranking * 0.40
                    + result.confidence * 0.30
                    + result.score * 0.20
                    + result.probability * 0.10
                ),
                2,
            )

        max_trade_candidates = int(market_state.get("max_trade_candidates", 20))

        # Additive only — does NOT change execution behavior. Stashes the
        # FULL per-symbol result list (including non-executable BUY/SELL
        # signals and their rejection diagnostics) so callers can report
        # "why wasn't this candidate executed" without re-scanning or
        # duplicating any scoring/validation/risk logic.
        self._last_full_scan_results = results

        return executable_results[:max_trade_candidates]

    @staticmethod
    def export_dataframe(results: list[ScanResult]) -> pd.DataFrame:
        rows = [
            {
                "Symbol": r.symbol,
                "Action": r.action,
                "Score": round(r.score, 2),
                "Probability": round(r.probability, 2),
                "Confidence": round(r.confidence, 2),
                "Ranking": round(r.ranking, 2),
                "Position": r.position_size,
                "Portfolio": r.portfolio_allowed,
            }
            for r in results
        ]
        return pd.DataFrame(rows)


# Alias assignment for orchestrator import fallback safety mapping
Scanner = MarketScanner
