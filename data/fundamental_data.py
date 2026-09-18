"""
Fundamental data provider.

Responsibilities:
- Fetch company fundamentals
- Normalize values
- No scoring
- No strategy logic
"""

from __future__ import annotations

import time
from typing import Any

import yfinance as yf

from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)

_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 1.5

# Standard GICS industry -> sector mapping, used ONLY as a fallback
# when yfinance's own "sector" field is missing but the more granular
# "industry" field IS present. This is a real classification (not a
# guess) — reduces genuine "UNKNOWN" cases without fabricating data.
# Not exhaustive of every possible yfinance industry string, but
# covers the common ones seen across NSE-listed companies.
_INDUSTRY_TO_SECTOR = {
    "oil & gas e&p": "Energy", "oil & gas midstream": "Energy",
    "oil & gas refining & marketing": "Energy", "oil & gas integrated": "Energy",
    "software - infrastructure": "Technology", "software - application": "Technology",
    "information technology services": "Technology", "semiconductors": "Technology",
    "consumer electronics": "Technology", "electronic components": "Technology",
    "banks - regional": "Financial Services", "banks - diversified": "Financial Services",
    "capital markets": "Financial Services", "credit services": "Financial Services",
    "insurance - life": "Financial Services", "insurance - diversified": "Financial Services",
    "asset management": "Financial Services", "mortgage finance": "Financial Services",
    "auto manufacturers": "Consumer Cyclical", "auto parts": "Consumer Cyclical",
    "specialty retail": "Consumer Cyclical", "apparel manufacturing": "Consumer Cyclical",
    "packaging & containers": "Consumer Cyclical", "leisure": "Consumer Cyclical",
    "packaged foods": "Consumer Defensive", "beverages - non-alcoholic": "Consumer Defensive",
    "household & personal products": "Consumer Defensive", "grocery stores": "Consumer Defensive",
    "tobacco": "Consumer Defensive", "farm products": "Consumer Defensive",
    "drug manufacturers - general": "Healthcare", "drug manufacturers - specialty & generic": "Healthcare",
    "biotechnology": "Healthcare", "diagnostics & research": "Healthcare",
    "medical devices": "Healthcare", "medical care facilities": "Healthcare",
    "engineering & construction": "Industrials", "specialty industrial machinery": "Industrials",
    "railroads": "Industrials", "aerospace & defense": "Industrials",
    "conglomerates": "Industrials", "electrical equipment & parts": "Industrials",
    "utilities - regulated electric": "Utilities", "utilities - renewable": "Utilities",
    "utilities - diversified": "Utilities",
    "steel": "Basic Materials", "copper": "Basic Materials", "chemicals": "Basic Materials",
    "specialty chemicals": "Basic Materials", "agricultural inputs": "Basic Materials",
    "real estate - development": "Real Estate", "real estate services": "Real Estate",
    "reit - diversified": "Real Estate",
    "telecom services": "Communication Services", "entertainment": "Communication Services",
}


_FIELDS = {
    "marketCap": "market_cap",
    "trailingPE": "pe",
    "priceToBook": "pb",
    "pegRatio": "peg",
    "returnOnEquity": "roe",
    "debtToEquity": "debt_to_equity",
    "earningsGrowth": "earnings_growth",
    "revenueGrowth": "revenue_growth",
    "totalCash": "cash",
    "operatingCashflow": "operating_cashflow",
    "ebitda": "ebitda",
    "bookValue": "book_value",
    "sector": "sector",
    "industry": "industry",
}

# BUGFIX (2026-09-18, Phase 5 — see BUG_AUDIT_2026-09-18.md item #15):
# "sector" is the only one of these 13 real fundamental fields ("industry"
# is just a sector-fallback helper, not a scoring input) that had ANY
# missing-rate visibility (see the `if not result.get("sector")` warning
# in fetch() below) — the other 12 could go universally None overnight
# (e.g. Yahoo renaming/retiring a .info key) and nothing would notice:
# every symbol's fetch would still "succeed" (this function never raises
# for a missing field), the field would just quietly read as
# always-missing, indistinguishable in the logs from "normally sparse
# per-symbol data". FundamentalDataProvider now tracks a running
# per-field missing count across a batch of fetch() calls (see
# missing_rate_report() below) so a caller doing a full-universe scan
# (execution/scanner.py) can detect "not just some symbols, EVERY symbol
# is missing this field" and raise it as a real alert instead of it
# passing as normal.
_MISSING_RATE_TRACKED_FIELDS = [
    target_key for target_key in _FIELDS.values() if target_key not in ("sector", "industry")
]


def normalize_fundamentals(info: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Map yfinance's raw camelCase .info keys (trailingPE, returnOnEquity,
    debtToEquity, ...) to the snake_case schema strategy/fundamental_scoring.py
    expects (pe, roe, debt_to_equity, ...). Without this mapping, none of the
    keys match, buy_fundamental_score()/sell_fundamental_score() silently fall
    back to their defaults for every field, and every candidate gets the SAME
    constant fundamental score regardless of the real company data — this was
    confirmed as the root cause of a backtest producing zero BUY trades across
    an entire year (fundamental score constant-favored SELL for every symbol,
    every day). Standalone so both FundamentalDataProvider.fetch() and any
    caller that already has a fetched .info dict (e.g. run_backtest.py) can
    reuse the exact same mapping instead of drifting apart."""
    result: dict[str, Any] = {"symbol": symbol}
    for source_key, target_key in _FIELDS.items():
        result[target_key] = info.get(source_key)
    if not result.get("sector") and result.get("industry"):
        inferred = _INDUSTRY_TO_SECTOR.get(str(result["industry"]).strip().lower())
        if inferred:
            result["sector"] = inferred
    return result


class FundamentalDataProvider:
    """Fetch normalized fundamental metrics."""

    _FIELDS = _FIELDS

    def __init__(self) -> None:
        # Per-field missing count + total successful-fetch count, reset
        # at the start of each scan cycle (a fresh FundamentalDataProvider
        # instance per DataEngine per MarketScanner run already resets
        # this naturally; reset_missing_rate_tracking() exists for any
        # caller that reuses one instance across multiple scan cycles).
        self._fetch_count = 0
        self._missing_counts: dict[str, int] = {
            field: 0 for field in _MISSING_RATE_TRACKED_FIELDS
        }

    def reset_missing_rate_tracking(self) -> None:
        self._fetch_count = 0
        self._missing_counts = {field: 0 for field in _MISSING_RATE_TRACKED_FIELDS}

    def missing_rate_report(self, min_symbols: int = 5) -> dict[str, float] | None:
        """Per-field missing rate (0.0-1.0) across every fetch() call
        since the last reset. Returns None if fewer than `min_symbols`
        symbols have been fetched yet -- a missing-rate computed from a
        handful of symbols is noise, not signal; this is meant to catch
        "the WHOLE watchlist is missing this field", not normal
        per-symbol sparsity."""
        if self._fetch_count < min_symbols:
            return None
        return {
            field: round(count / self._fetch_count, 4)
            for field, count in self._missing_counts.items()
        }

    def fetch(self, symbol: str) -> dict[str, Any]:
        """
        Fetch normalized fundamental data for a symbol.
        """
        info = None
        last_exc: Exception | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                info = yf.Ticker(symbol).info
                break
            except Exception as exc:
                last_exc = exc
                if attempt < _RETRY_ATTEMPTS:
                    logger.warning(
                        "Fundamentals fetch attempt %d/%d failed for %s: %s — retrying.",
                        attempt, _RETRY_ATTEMPTS, symbol, exc,
                    )
                    time.sleep(_RETRY_DELAY_SECONDS)
        if info is None:
            raise DataError(f"Unable to fetch fundamentals for '{symbol}'.") from last_exc

        if not info:
            raise DataError(f"No fundamental data available for '{symbol}'.")

        result = normalize_fundamentals(info, symbol)
        if result.get("sector") and result.get("industry") and result["sector"] != info.get("sector"):
            logger.info(
                "Sector missing for %s — inferred '%s' from industry '%s'.",
                symbol, result["sector"], result["industry"],
            )

        if not result.get("sector"):
            logger.warning(
                "No sector could be determined for %s (yfinance sector/industry both "
                "missing or unmapped) — will fall back to UNKNOWN downstream.", symbol,
            )

        # See _MISSING_RATE_TRACKED_FIELDS's BUGFIX comment above.
        self._fetch_count += 1
        for field in _MISSING_RATE_TRACKED_FIELDS:
            if result.get(field) is None:
                self._missing_counts[field] += 1

        logger.info("Loaded fundamentals for %s", symbol)

        return result
