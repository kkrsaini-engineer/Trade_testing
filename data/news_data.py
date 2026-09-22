"""
News data provider.

Responsibilities:
- Fetch raw news headlines
- Normalize output
- No sentiment analysis
- No AI/event detection
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yfinance as yf

from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)

# Broad-market index tickers used as macro-headline sources. Using more
# than one avoids a single-point-of-failure: if one index's news feed
# is sparse/empty on a given fetch, the others can still surface real
# macro headlines instead of the whole macro-risk check going blind.
# One large, liquid representative stock per sector used in this
# system's own taxonomy — chosen so a sector-specific macro theme
# (e.g. pharma tariffs, real-estate rate sensitivity) has a genuine
# chance of surfacing, not just broad economy-wide news.
MACRO_NEWS_SOURCES = [
    "RELIANCE.NS",     # Energy
    "TCS.NS",          # IT / Technology
    "HDFCBANK.NS",     # Financial Services
    "MARUTI.NS",       # Consumer Cyclical
    "ITC.NS",          # Consumer Defensive
    "SUNPHARMA.NS",    # Healthcare
    "LT.NS",           # Industrials
    "NTPC.NS",         # Utilities
    "TATASTEEL.NS",    # Basic Materials
    "DLF.NS",          # Real Estate
    "BHARTIARTL.NS",   # Communication Services
]

# How many attempts (including the first) before giving up on a fetch.
_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.5

MACRO_CACHE_PATH = "storage/reports/macro_headlines_cache.json"

# BUGFIX (2026-09-19, post-Phase-5 re-audit — same gap
# BUG_AUDIT_2026-09-18.md item #17 fixed for data/delivery_data.py,
# data/fii_dii_data.py, and market/circuit_bands.py, missed here):
# _load_macro_cache() stored a "fetched_at" timestamp but never checked
# it, so a run where every live macro-news source is down/blocked would
# keep serving the same increasingly-stale headline set forever, with no
# way to tell "yesterday's headlines" from "three-week-old headlines" in
# the logs. This feeds market/macro_intelligence.py's daily macro-bias
# check for every symbol -- the same pipeline as the already-fixed
# CRITICAL #4 word-boundary bug. Past this many days, the cache is
# treated as unusable.
_MACRO_CACHE_MAX_AGE_DAYS = 7


def _fetch_ticker_news_with_retry(ticker_symbol: str) -> list[dict[str, Any]] | None:
    """Fetch .news for one ticker, retrying on transient failures.
    Returns None only if every attempt failed with an exception —
    an empty (but successful) result returns [] as normal."""
    last_exc: Exception | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return yf.Ticker(ticker_symbol).news
        except Exception as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS:
                logger.warning(
                    "Fetch attempt %d/%d failed for %s: %s — retrying.",
                    attempt, _RETRY_ATTEMPTS, ticker_symbol, exc,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
    logger.warning("All %d fetch attempts failed for %s: %s", _RETRY_ATTEMPTS, ticker_symbol, last_exc)
    return None


class NewsDataProvider:
    """Fetch raw company news."""

    def fetch(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        Fetch recent news for a symbol.

        Returns:
            List of normalized news dictionaries.
        """
        news = _fetch_ticker_news_with_retry(symbol)
        if news is None:
            raise DataError(f"Unable to fetch news for '{symbol}'.")

        if not news:
            logger.warning("No news found for %s", symbol)
            return []

        results: list[dict[str, Any]] = []

        for item in news[:limit]:
            # yfinance 0.2.x nests the real article fields under
            # item["content"] (title, publisher, link, pubDate, type) —
            # NOT at the top level. This was the actual root cause of
            # the "macro news always returns 0 headlines" issue: reading
            # item.get("title") directly always returned None/empty,
            # even though the raw fetch genuinely had real articles.
            # Try the new nested path first, fall back to the old flat
            # path for compatibility with any other yfinance version.
            content = item.get("content") or {}
            title = content.get("title") or item.get("title")
            publisher = (
                (content.get("provider") or {}).get("displayName")
                or item.get("publisher")
            )
            link = (
                (content.get("canonicalUrl") or {}).get("url")
                or item.get("link")
            )
            item_type = content.get("contentType") or item.get("type")
            uuid = content.get("id") or item.get("uuid") or item.get("id")

            ts = item.get("providerPublishTime")
            published = (
                datetime.fromtimestamp(ts).isoformat()
                if isinstance(ts, (int, float))
                else content.get("pubDate")
            )

            results.append(
                {
                    "symbol": symbol,
                    "title": title,
                    "publisher": publisher,
                    "published_at": published,
                    "link": link,
                    "type": item_type,
                    "uuid": uuid,
                }
            )

        logger.info("Loaded %d news items for %s", len(results), symbol)
        return results

    def fetch_market_news(self, limit: int = 20) -> list[str]:
        """
        Fetch broad market/macro headlines (not company-specific) — used
        by market/macro_intelligence.py to detect macro themes (wars, oil
        supply shocks, rate decisions, etc.) that individual per-company
        news wouldn't reliably surface.

        Resilience (this fetch affects EVERY open position's macro-risk
        check in one shot, so a gap here is high-impact):
        - Queries multiple large-cap bellwether stocks spanning
          different sectors (MACRO_NEWS_SOURCES) as a broad-market news
          proxy — NOT index tickers, since ^NSEI/^BSESN's own .news
          field was confirmed (in production) to consistently return
          empty even when individual stock tickers reliably return
          real headlines.
        - Retries each source on transient failure.
        - If every source comes back genuinely empty, falls back to the
          last successfully-fetched non-empty headline set (cached on
          disk) rather than silently treating it as "no macro risk" —
          yesterday's macro headlines are usually still relevant, not
          instantly stale.
        """
        combined: list[str] = []
        any_source_had_content = False

        for source in MACRO_NEWS_SOURCES:
            news = _fetch_ticker_news_with_retry(source)
            if news is None:
                continue  # this source failed entirely, try the next
            if not news:
                logger.warning("Market news fetch for %s returned 0 headlines.", source)
                continue
            # Title lives under item["content"]["title"] in yfinance
            # 0.2.x, not at the top level — this was the actual root
            # cause of "0 headlines" despite genuinely fetching real
            # articles. Same fallback pattern as fetch() above.
            titles = []
            for item in news[:limit]:
                content = item.get("content") or {}
                title = content.get("title") or item.get("title")
                if title:
                    titles.append(title)
            if titles:
                any_source_had_content = True
                combined.extend(titles)
                logger.info("Loaded %d market/macro headlines from %s.", len(titles), source)

        # De-duplicate while preserving order (different indices often
        # surface the same broad-market headline).
        seen = set()
        deduped = []
        for title in combined:
            if title not in seen:
                seen.add(title)
                deduped.append(title)
        deduped = deduped[:limit]

        if deduped:
            self._save_macro_cache(deduped)
            return deduped

        if not any_source_had_content:
            logger.warning(
                "All macro news sources (%s) returned 0 headlines this run — "
                "falling back to last known-good cached headlines if available.",
                ", ".join(MACRO_NEWS_SOURCES),
            )
        cached = self._load_macro_cache()
        if cached:
            logger.warning(
                "Using %d cached macro headline(s) from a previous successful "
                "fetch (today's live fetch was empty).", len(cached),
            )
            return cached

        logger.warning("No live or cached macro headlines available — macro risk analysis sees an empty list.")
        return []

    @staticmethod
    def _save_macro_cache(headlines: list[str]) -> None:
        try:
            Path(MACRO_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(MACRO_CACHE_PATH, "w") as f:
                json.dump({"headlines": headlines, "fetched_at": time.time()}, f)
        except OSError as exc:
            logger.warning("Could not write macro headline cache: %s", exc)

    @staticmethod
    def _load_macro_cache() -> list[str]:
        try:
            with open(MACRO_CACHE_PATH) as f:
                data = json.load(f)

            fetched_at = data.get("fetched_at")
            if isinstance(fetched_at, (int, float)):
                age_days = (time.time() - fetched_at) / 86400.0
                if age_days > _MACRO_CACHE_MAX_AGE_DAYS:
                    logger.error(
                        "Macro headline cache is %.1f day(s) old — past the "
                        "%d-day staleness limit. Refusing to use it; treating "
                        "this as no macro headlines available.",
                        age_days, _MACRO_CACHE_MAX_AGE_DAYS,
                    )
                    return []

            return data.get("headlines", [])
        except (OSError, json.JSONDecodeError):
            return []
