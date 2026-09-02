"""
MARKET INTELLIGENCE ENGINE

Completely independent from the production trading strategy. This is
NOT part of the Decision Engine and NEVER generates BUY, SELL, or EXIT
signals — it never calls the scanner, Entry Engine, or Exit Engine, and
never modifies the Virtual Portfolio, Trade Diary, or any probability.

Purpose: research, monitoring, and early warning ONLY.

    News / Macro / Results / Global Events
        |
    Research (this module)
        |
    Telegram Alert (advisory text only, via core.notifications)
        |
    Human review — the human (or the existing Exit Engine, on its own
    separate schedule) decides what, if anything, to do about it.

Everything this engine observes is stored to
storage/reports/market_intelligence_log.jsonl for the Analysis/Learning/
Optimizer modules to consume later — it does not feed back into today's
trading decisions directly.

Reuses existing infrastructure rather than duplicating it:
    - market/macro_intelligence.py's sector_bias() for theme detection
    - data/news_data.py for news fetching (same provider as the scanner)
    - news/sentiment_engine.py for sentiment scoring
    - core/notifications.py for severity classification + dedup + sending
      (the SAME shared helper every other module uses — no separate copy)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.logger import get_logger
from core.notifications import notify, severity_from_magnitude
from core.trading_calendar import now_ist
from data.news_data import NewsDataProvider
from market import macro_intelligence
from news.sentiment_engine import SentimentEngine

logger = get_logger(__name__)

LOG_PATH = "storage/reports/market_intelligence_log.jsonl"

# Sector impact themes reused as-is from market/macro_intelligence.py —
# NOT reimplemented here (see that module for the full theme table).
MACRO_KEYWORDS = [kw for keywords, _ in macro_intelligence.THEMES for kw in keywords]

# Thresholds for "is this significant enough to alert on" — advisory
# only, tune freely without touching any production trading threshold.
NEWS_ALERT_THRESHOLD = 0.5   # |signed bias| beyond this is "significant"
SENTIMENT_SCORE_NEUTRAL = 50.0


def _signed_bias(scored_item: dict[str, Any]) -> float:
    """
    SentimentEngine.evaluate() returns an UNSIGNED 0-100 magnitude in
    "impact_score" (50=weak/neutral, 100=strong) plus a separate polarity
    string in "sentiment" (POSITIVE/NEGATIVE/NEUTRAL) — it does not encode
    direction as a signed number itself. This converts the pair into a
    single signed bias in roughly [-1, +1] for this engine's own use.
    """
    impact = float(scored_item.get("impact_score", 50.0))
    magnitude = max(0.0, (impact - 50.0) / 50.0)  # 0 (neutral) .. 1 (max)
    polarity = scored_item.get("sentiment", "NEUTRAL")
    if polarity == "POSITIVE":
        return magnitude
    if polarity == "NEGATIVE":
        return -magnitude
    return 0.0


class MarketIntelligenceEngine:
    """
    Research-only. Call run() once per scheduled run with the list of
    currently open positions (symbol + direction) — it never reads or
    writes the Virtual Portfolio/Trade Diary itself, keeping it
    decoupled; the caller supplies the position list.
    """

    def __init__(
        self,
        news_provider: NewsDataProvider | None = None,
        sentiment_engine: SentimentEngine | None = None,
    ):
        self.news_provider = news_provider or NewsDataProvider()
        self.sentiment_engine = sentiment_engine or SentimentEngine()

    # ==========================================================
    # MAIN ENTRY POINT
    # ==========================================================

    def run(self, open_positions: list[dict[str, Any]]) -> dict[str, Any]:
        """
        open_positions: [{"symbol": "INFY.NS", "direction": "BUY", "sector": "IT"}, ...]
        Read-only input — this engine never mutates portfolio state.

        Sequencing (see data/news_data.py history for why): company-news
        analysis for ALL positions runs FIRST — this is the proven
        reliable path. Macro-news fetch happens AFTER, once real yfinance
        traffic has already flowed for this run, instead of before it —
        an artificial single warm-up call didn't resolve the issue in
        production, so this leverages the ACTUAL working call sequence.
        """
        position_observations = []
        alerts_sent = []

        # Pass 1 — company-news analysis for every position.
        for pos in open_positions:
            obs = self._analyze_company_news(pos)
            position_observations.append(obs)
            if obs["alert_triggered"]:
                sent = notify(
                    event_type="market_intelligence",
                    message=obs["alert_body"],
                    severity=obs["severity"],
                    dedup_key=obs["signature"],
                )
                if sent:
                    alerts_sent.append(obs["alert_body"])

        # Macro-news fetch — now happens AFTER real per-symbol traffic.
        macro_headlines = self._safe_fetch_market_news()
        macro_observation = self._analyze_macro(macro_headlines)

        # Pass 2 — macro-bias check, only for positions that didn't
        # already trigger a company-news alert (same precedence as before).
        for obs in position_observations:
            updated = self._apply_macro_check(obs, macro_headlines)
            if updated["alert_triggered"] and updated["alert_body"] not in alerts_sent:
                sent = notify(
                    event_type="market_intelligence",
                    message=updated["alert_body"],
                    severity=updated["severity"],
                    dedup_key=updated["signature"],
                )
                if sent:
                    alerts_sent.append(updated["alert_body"])

        record = {
            "timestamp": time.time(),
            "macro": macro_observation,
            "positions": position_observations,
            "alerts_sent": alerts_sent,
        }
        self._store(record)

        # Final clean summary (Telegram beautification) — built entirely
        # from fields already computed above (alert_triggered, severity,
        # macro_risk_score, global_risk_level). No new calculations.
        if open_positions:
            notify(
                event_type="market_intelligence_summary",
                message=self._build_summary(position_observations, macro_observation),
                dedup_key=f"mi_summary::{now_ist().strftime('%Y-%m-%d %H:%M:%S.%f')}",
            )

        logger.info(
            "Market Intelligence run complete: %d positions checked, %d alerts sent.",
            len(open_positions), len(alerts_sent),
        )
        return record

    def _build_summary(
        self, position_observations: list[dict[str, Any]], macro_observation: dict[str, Any]
    ) -> str:
        """Clean final summary — uses only pre-existing fields
        (alert_triggered, severity, macro_risk_score/global_risk_level).
        No new metrics, no new scoring."""
        safe = [o["symbol"] for o in position_observations if not o["alert_triggered"]]
        watch = [
            o["symbol"] for o in position_observations
            if o["alert_triggered"] and o["severity"] in ("🟢 LOW", "🟡 MEDIUM")
        ]
        high_risk = [
            o["symbol"] for o in position_observations
            if o["alert_triggered"] and o["severity"] in ("🟠 HIGH", "🔴 CRITICAL")
        ]

        lines = [
            "📊 Market Intelligence Summary",
            "",
            f"Open Positions Checked: {len(position_observations)}",
            f"Safe Holdings: {len(safe)}" + (f" ({', '.join(safe)})" if safe else ""),
            f"Watchlist Holdings: {len(watch)}" + (f" ({', '.join(watch)})" if watch else ""),
            f"High Risk Holdings: {len(high_risk)}" + (f" ({', '.join(high_risk)})" if high_risk else ""),
        ]

        flagged = [o for o in position_observations if o["alert_triggered"]]
        if flagged:
            lines.append("")
            lines.append("Negative News:")
            for o in flagged:
                lines.append(f"• {o['symbol']} — {o['severity']}")

        lines.append("")
        lines.append(f"Macro Risk: {macro_observation['global_risk_level']} "
                     f"({macro_observation['macro_risk_score']}/100)")

        return "\n".join(lines)

    # ==========================================================
    # MACRO ANALYSIS (research only — no signals)
    # ==========================================================

    def _safe_fetch_market_news(self) -> list[str]:
        try:
            return self.news_provider.fetch_market_news()
        except Exception as exc:
            logger.warning("Market news fetch failed: %s", exc)
            return []

    def _analyze_macro(self, headlines: list[str]) -> dict[str, Any]:
        text = " ".join(h.lower() for h in headlines)
        critical_events = [kw for kw in MACRO_KEYWORDS if kw in text]

        macro_risk_score = min(100.0, len(critical_events) * 20.0)

        scored = self.sentiment_engine.evaluate([{"title": h} for h in headlines]) if headlines else []
        if scored:
            avg_bias = sum(_signed_bias(s) for s in scored) / len(scored)
            sentiment_score = max(0.0, min(100.0, SENTIMENT_SCORE_NEUTRAL + avg_bias * 50.0))
        else:
            sentiment_score = SENTIMENT_SCORE_NEUTRAL

        if macro_risk_score >= 60:
            global_risk_level = "HIGH"
        elif macro_risk_score >= 30:
            global_risk_level = "MEDIUM"
        else:
            global_risk_level = "LOW"

        return {
            "overall_market_sentiment_score": round(sentiment_score, 2),
            "macro_risk_score": round(macro_risk_score, 2),
            "global_risk_level": global_risk_level,
            "critical_events": critical_events,
            "headlines_seen": len(headlines),
        }

    # ==========================================================
    # PER-POSITION RESEARCH (advisory only)
    # ==========================================================

    def _analyze_company_news(self, position: dict[str, Any]) -> dict[str, Any]:
        """Company-specific news analysis only — macro check is applied
        separately afterward, once macro_headlines has been fetched
        (see run())."""
        symbol = position["symbol"]
        direction = position.get("direction", "BUY")
        sector = position.get("sector")

        try:
            company_news = self.news_provider.fetch(symbol=symbol, limit=10)
        except Exception as exc:
            logger.warning("Company news fetch failed for %s: %s", symbol, exc)
            company_news = []

        scored_news = self.sentiment_engine.evaluate(company_news) if company_news else []
        avg_impact = (
            sum(_signed_bias(n) for n in scored_news) / len(scored_news)
            if scored_news else 0.0
        )
        top_headline = ""
        if scored_news:
            top_headline = max(scored_news, key=lambda n: abs(_signed_bias(n))).get("title", "")

        alert_triggered = False
        alert_body = None
        signature = None
        severity = None

        adverse_signal = -avg_impact if direction == "BUY" else avg_impact

        if avg_impact != 0.0 and abs(avg_impact) >= NEWS_ALERT_THRESHOLD and adverse_signal > 0:
            polarity = "Negative" if direction == "BUY" else "Positive"
            signature = f"news::{symbol}::{top_headline}"
            alert_triggered = True
            severity = severity_from_magnitude(avg_impact)
            alert_body = (
                f"{polarity} news detected for {symbol}"
                + (f": {top_headline}" if top_headline else ".") + "\n"
                f"You currently hold a {direction} position.\n"
                f"Please review this position.\n"
                f"No automatic action has been taken."
            )

        return {
            "symbol": symbol,
            "direction": direction,
            "sector": sector,
            "news_impact_score": round(avg_impact, 3),
            "macro_bias": 0.0,  # filled in by _apply_macro_check, if it runs
            "alert_triggered": alert_triggered,
            "alert_body": alert_body,
            "signature": signature,
            "severity": severity,
        }

    @staticmethod
    def _apply_macro_check(obs: dict[str, Any], macro_headlines: list[str]) -> dict[str, Any]:
        """Applies the macro-bias check AFTER company-news analysis —
        only if a company-news alert didn't already trigger (same
        precedence as before, just evaluated in a second pass)."""
        if obs["alert_triggered"]:
            return obs  # company-news alert already took priority

        symbol = obs["symbol"]
        direction = obs["direction"]
        sector = obs["sector"]
        macro_bias = macro_intelligence.sector_bias(macro_headlines, sector) if sector else 0.0
        obs["macro_bias"] = macro_bias

        adverse_macro = -macro_bias if direction == "BUY" else macro_bias
        if macro_bias != 0.0 and abs(macro_bias) >= 0.3 and adverse_macro > 0 and sector:
            obs["signature"] = f"macro::{sector}::{symbol}"
            obs["alert_triggered"] = True
            obs["severity"] = severity_from_magnitude(macro_bias)
            obs["alert_body"] = (
                f"Macro development detected affecting the {sector} sector.\n"
                f"This may affect your {direction} position in {symbol}.\n"
                f"No automatic action has been taken."
            )
        return obs

    # ==========================================================
    # STORAGE (for future Analysis/Learning/Optimizer consumption)
    # ==========================================================

    def _store(self, record: dict[str, Any]) -> None:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
