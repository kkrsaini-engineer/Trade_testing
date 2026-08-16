"""
NSE Delivery Data Provider.

Responsibilities:
- Fetch the daily NSE "Security-wise Delivery Position" bhavcopy
  (one CSV covering every EQ-series symbol for a trading day).
- Return a {symbol: {field: value}} lookup.
- No strategy/decision logic.

Context: decision/validation_engine.py reads
`latest.get("delivery_percentage", 100.0)` — that field previously had
NO producer anywhere in the codebase, so the default (100.0, always
passes the `>= 20.0` liquidity gate) was the only value it could ever
see. This module gives it a real value when the fetch succeeds, and
deliberately does NOT force a fake number when it fails — see
DeliveryDataProvider.fetch_latest() for why.

LIQUIDITY FIELDS (added alongside delivery_percent): the same bhavcopy
CSV also carries TTL_TRD_QNTY, NO_OF_TRADES, TURNOVER_LACS, CLOSE_PRICE
and PREV_CLOSE — previously fetched and then silently discarded (only
SYMBOL, SERIES, DELIV_PER were read). These extra columns are what
strategy/*_scoring.py's _liquidity_score() and
data/liquidity_history.py's rolling-window builder use to tell a
"broad-participation" trading day (many small trades) apart from a
"block-deal-driven" one (few huge trades) — see PHASE18_NOTES.md for the
full reasoning. No new network call or data source was added; this is
purely reading columns that were already being downloaded and thrown
away.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from core.logger import get_logger

logger = get_logger(__name__)

_BHAVCOPY_URL = "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
_WARMUP_URL = "https://www.nseindia.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/csv,application/csv,text/plain,*/*",
}

# How many calendar days back to try before giving up (covers weekends/
# holidays and the file not being published yet if run early evening).
_MAX_DAYS_BACK = 5
_REQUEST_TIMEOUT_SECONDS = 15
_RETRY_ATTEMPTS = 2

_DELIVERY_CACHE_PATH = "storage/reports/delivery_data_cache.json"

# Columns pulled from the bhavcopy beyond SYMBOL/SERIES/DELIV_PER, and
# the field name each is stored under in the returned per-symbol dict.
# All are optional per-symbol — a row missing/unparsable for one of
# these still keeps whatever other fields DID parse (see _parse_csv),
# same "don't fabricate, just omit" convention as delivery_percent.
_EXTRA_COLUMNS = {
    "TTL_TRD_QNTY": "ttl_trd_qnty",
    "NO_OF_TRADES": "no_of_trades",
    "TURNOVER_LACS": "turnover_lacs",
    "CLOSE_PRICE": "close_price",
    "PREV_CLOSE": "prev_close",
}


class DeliveryDataProvider:
    """Fetch NSE-wide delivery-percentage + liquidity data, once per
    call, for the most recent trading day that has a published
    bhavcopy."""

    def fetch_latest(self) -> dict[str, dict[str, float]]:
        """Returns {symbol_without_suffix: {field: value}}.

        Fields (each optional per-symbol — read with .get()):
          - delivery_percent
          - ttl_trd_qnty, no_of_trades, turnover_lacs (liquidity inputs)
          - close_price, prev_close (for Amihud illiquidity)

        Returns {} (empty, NOT a fabricated default) if every attempt
        fails — callers must treat an empty dict as "no live delivery
        data available" and fall back to whatever they were already
        doing (validation_engine.py's own `.get(..., 100.0)` default),
        rather than this module inventing a number. Forcing a fake
        value here would just move the "always passes" problem instead
        of fixing it.
        """
        session = requests.Session()
        session.headers.update(_HEADERS)

        # NSE's archive endpoints reliably 403 without cookies from a
        # prior visit to the main site — this warm-up request is not
        # optional. If it fails, the CSV fetch below will very likely
        # also fail, but we still try (some environments/CDN nodes
        # don't enforce this).
        try:
            session.get(_WARMUP_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("NSE warm-up request failed (%s) — attempting bhavcopy fetch anyway.", exc)

        today = date.today()
        for days_back in range(_MAX_DAYS_BACK):
            target_date = today - timedelta(days=days_back)
            data = self._fetch_for_date(session, target_date)
            if data:
                self._write_cache(data, target_date)
                return data

        logger.warning(
            "NSE delivery data unavailable after trying %d day(s) back — "
            "falling back to last cached snapshot, if any.",
            _MAX_DAYS_BACK,
        )
        return self._read_cache()

    def fetch_range(
        self,
        trading_days: int,
        max_calendar_days_back: int = 45,
        request_delay_seconds: float = 1.5,
    ) -> dict[date, dict[str, dict[str, float]]]:
        """Fetch bhavcopy data for the most recent `trading_days`
        trading days, walking back from YESTERDAY (never today — today
        belongs to fetch_latest()'s normal daily-scan path, so a
        backfill run doesn't race or duplicate work with it).

        Unlike fetch_latest() (single most-recent day, with a cache
        fallback for the daily scan path), this makes one HTTP request
        PER trading day and is meant for a one-time historical backfill
        (see scripts/backfill_liquidity_history.py) — it does not read
        or write the single-day cache.

        Returns {date: {symbol: {field: value}}}, in chronological
        order (oldest date first) so a caller appending each date to a
        rolling history (data/liquidity_history.py) processes them in
        the right order.

        Stops once `trading_days` successful fetches are collected, or
        once `max_calendar_days_back` calendar days have been tried,
        whichever comes first. The calendar-day cap exists because
        bhavcopy is only published on actual trading days (~250/year,
        with holiday clusters) — without a cap, a long enough gap (a
        multi-day outage, or the archive being unreachable) would keep
        walking back indefinitely.

        `request_delay_seconds` politely spaces out the (up to
        max_calendar_days_back) requests this makes in one run — unlike
        fetch_latest()'s single request per scan, a backfill run can
        make dozens in quick succession.
        """
        session = requests.Session()
        session.headers.update(_HEADERS)
        try:
            session.get(_WARMUP_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("NSE warm-up request failed (%s) — attempting range fetch anyway.", exc)

        results: dict[date, dict[str, dict[str, float]]] = {}
        today = date.today()

        for days_back in range(1, 1 + max_calendar_days_back):
            if len(results) >= trading_days:
                break
            target_date = today - timedelta(days=days_back)
            day_data = self._fetch_for_date(session, target_date)
            if day_data:
                results[target_date] = day_data
                logger.info("Backfill: fetched bhavcopy for %s (%d symbols).", target_date.isoformat(), len(day_data))
            if request_delay_seconds > 0:
                time.sleep(request_delay_seconds)

        # The loop above walks backwards from yesterday, so `results` is
        # populated newest-first — return it oldest-first instead, since
        # that's the order a rolling-history append needs.
        return dict(sorted(results.items()))

    def _fetch_for_date(self, session: requests.Session, target_date: date) -> dict[str, dict[str, float]]:
        url = _BHAVCOPY_URL.format(ddmmyyyy=target_date.strftime("%d%m%Y"))

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = session.get(url, timeout=_REQUEST_TIMEOUT_SECONDS)
                if resp.status_code == 404:
                    # No bhavcopy for this date (holiday/weekend/not yet
                    # published) — not an error, just try an earlier date.
                    return {}
                resp.raise_for_status()
                return self._parse_csv(resp.text)
            except Exception as exc:
                logger.warning(
                    "Delivery data fetch attempt %d/%d for %s failed: %s",
                    attempt, _RETRY_ATTEMPTS, target_date.isoformat(), exc,
                )
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(2.0)

        return {}

    @staticmethod
    def _parse_csv(raw_text: str) -> dict[str, dict[str, float]]:
        from io import StringIO

        df = pd.read_csv(StringIO(raw_text))
        df.columns = [c.strip().upper() for c in df.columns]

        required = {"SYMBOL", "SERIES", "DELIV_PER"}
        missing = required.difference(df.columns)
        if missing:
            logger.warning("Delivery bhavcopy missing expected columns: %s", sorted(missing))
            return {}

        # SERIES == "EQ" -> ordinary equity delivery-based trading only
        # (excludes BE/BZ/derivatives-linked series with different
        # delivery semantics).
        df["SERIES"] = df["SERIES"].astype(str).str.strip()
        df = df[df["SERIES"] == "EQ"]

        # Extra liquidity columns are optional at the CSV level (an
        # older/changed NSE export missing one of them should not break
        # delivery_percent, which is the one field this module has
        # always guaranteed) — only read columns that are actually
        # present.
        present_extra = {col: field for col, field in _EXTRA_COLUMNS.items() if col in df.columns}
        if len(present_extra) < len(_EXTRA_COLUMNS):
            missing_extra = sorted(set(_EXTRA_COLUMNS) - set(present_extra))
            logger.warning("Delivery bhavcopy missing optional liquidity columns: %s", missing_extra)

        result: dict[str, dict[str, float]] = {}
        for _, row in df.iterrows():
            symbol = str(row["SYMBOL"]).strip()
            entry: dict[str, float] = {}

            raw_deliv = str(row["DELIV_PER"]).strip()
            if raw_deliv not in ("", "-"):
                try:
                    entry["delivery_percent"] = float(raw_deliv)
                except ValueError:
                    pass

            for col, field in present_extra.items():
                raw_val = str(row[col]).strip()
                if raw_val in ("", "-"):
                    continue
                try:
                    entry[field] = float(raw_val)
                except ValueError:
                    continue

            if entry:
                result[symbol] = entry

        return result

    @staticmethod
    def _write_cache(data: dict[str, dict[str, float]], as_of: date) -> None:
        try:
            path = Path(_DELIVERY_CACHE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"as_of": as_of.isoformat(), "data": data}, f)
        except Exception as exc:
            logger.warning("Failed to write delivery data cache: %s", exc)

    @staticmethod
    def _read_cache() -> dict[str, dict[str, float]]:
        try:
            path = Path(_DELIVERY_CACHE_PATH)
            if not path.exists():
                return {}
            with open(path) as f:
                payload = json.load(f)
            logger.warning(
                "Using cached delivery data from %s (live fetch failed today).",
                payload.get("as_of", "unknown date"),
            )
            data: dict[str, Any] = payload.get("data", {})
            # Backward compatibility: a cache file written before this
            # liquidity extension stores {symbol: float} (delivery_percent
            # only), not {symbol: {field: value}}. Wrap old-format entries
            # instead of crashing/silently losing them, so an existing
            # committed cache from before this change keeps working.
            wrapped: dict[str, dict[str, float]] = {}
            for symbol, value in data.items():
                if isinstance(value, dict):
                    wrapped[symbol] = value
                else:
                    try:
                        wrapped[symbol] = {"delivery_percent": float(value)}
                    except (TypeError, ValueError):
                        continue
            return wrapped
        except Exception as exc:
            logger.warning("Failed to read delivery data cache: %s", exc)
            return {}


def symbol_without_suffix(symbol: str) -> str:
    """'RELIANCE.NS' -> 'RELIANCE' (NSE bhavcopy symbols have no suffix)."""
    return symbol.split(".")[0].upper()
