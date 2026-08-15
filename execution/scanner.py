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
from data.fii_dii_data import FiiDiiDataProvider
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

    def __init__(self):
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
        self._delivery_data: dict[str, float] | None = None  # lazy-fetched, shared across all symbols in a scan run
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
        if self._market_headlines is None:
            try:
                self._market_headlines = self._news_provider.fetch_market_news()
            except Exception as exc:
                logger.warning("Market news fetch failed: %s", exc)
                self._market_headlines = []
        return self._market_headlines

    def _get_delivery_data(self) -> dict[str, float]:
        """Fetch the NSE-wide delivery-percentage bhavcopy once per scan
        run and cache it (same reasoning as _get_market_headlines() —
        one file covers every symbol, so there's no reason to hit NSE
        once per symbol). Returns {} on total failure; callers must
        treat that as "no live data" and fall back gracefully, not
        substitute a fabricated number."""
        if self._delivery_data is None:
            try:
                self._delivery_data = self._delivery_provider.fetch_latest()
            except Exception as exc:
                logger.warning("Delivery data fetch failed: %s", exc)
                self._delivery_data = {}
        return self._delivery_data

    def _get_fii_dii_data(self) -> dict[str, Any] | None:
        """Fetch NSE-wide FII/DII net activity once per scan run and
        cache it (same reasoning as _get_delivery_data() — one market-
        wide value covers every symbol). Returns None when unavailable;
        callers must treat that as "no signal" and skip the nudge, not
        substitute a fabricated bias."""
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
        deliv_lookup = self._get_delivery_data()
        deliv_value = deliv_lookup.get(symbol_without_suffix(symbol))
        if deliv_value is not None:
            dataframe.loc[dataframe.index[-1], "delivery_percentage"] = deliv_value
            diagnostics["delivery_percentage"] = deliv_value

        # 2b. FUNDAMENTALS / NEWS SENTIMENT / MARKET REGIME
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
        diagnostics["relative_strength"] = round(
            float(latest.get("relative_strength", 0) or 0), 2
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

        # NOTE: Sector rotation needs a cross-symbol sector-index dataframe
        # (see market/sector_rotation.py) which this per-symbol scan does not
        # have available. Using a neutral placeholder until sector index data
        # is wired into DataEngine.
        sector_score = 50.0

        # NOTE: "breadth" (market breadth) is also a market-wide metric
        # (see market/market_breadth.py) that needs advance/decline data
        # across the whole market, not a single symbol. Using a neutral
        # placeholder for the same reason as sector_score above.
        dataframe["breadth"] = 50.0

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
        diagnostics["buy_news_health"] = buy_decision.news_health

        diagnostics["sell_tier1_checks"] = sell_decision.tier1_checks
        diagnostics["sell_tier1_passed"] = sell_decision.tier1_passed
        diagnostics["sell_tier2_score"] = sell_decision.tier2_score
        diagnostics["sell_tier3_score"] = sell_decision.tier3_score
        diagnostics["sell_overall_score"] = sell_decision.overall_score
        diagnostics["sell_qualify_threshold"] = sell_decision.qualify_threshold
        diagnostics["sell_fundamental_weakness"] = sell_decision.fundamental_weakness
        diagnostics["sell_news_negativity"] = sell_decision.news_negativity
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
