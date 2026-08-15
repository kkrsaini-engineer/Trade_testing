"""
NSE Delivery Data Provider.

Responsibilities:
- Fetch the daily NSE "Security-wise Delivery Position" bhavcopy
  (one CSV covering every EQ-series symbol for a trading day).
- Return a {symbol: delivery_percentage} lookup.
- No strategy/decision logic.

Context: decision/validation_engine.py reads
`latest.get("delivery_percentage", 100.0)` — that field previously had
NO producer anywhere in the codebase, so the default (100.0, always
passes the `>= 20.0` liquidity gate) was the only value it could ever
see. This module gives it a real value when the fetch succeeds, and
deliberately does NOT force a fake number when it fails — see
DeliveryDataProvider.fetch_latest() for why.
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


class DeliveryDataProvider:
    """Fetch NSE-wide delivery-percentage data, once per call, for the
    most recent trading day that has a published bhavcopy."""

    def fetch_latest(self) -> dict[str, float]:
        """Returns {symbol_without_suffix: delivery_percent}.

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

    def _fetch_for_date(self, session: requests.Session, target_date: date) -> dict[str, float]:
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
    def _parse_csv(raw_text: str) -> dict[str, float]:
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

        result: dict[str, float] = {}
        for _, row in df.iterrows():
            symbol = str(row["SYMBOL"]).strip()
            raw_deliv = str(row["DELIV_PER"]).strip()
            if raw_deliv in ("", "-"):
                continue
            try:
                result[symbol] = float(raw_deliv)
            except ValueError:
                continue

        return result

    @staticmethod
    def _write_cache(data: dict[str, float], as_of: date) -> None:
        try:
            path = Path(_DELIVERY_CACHE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"as_of": as_of.isoformat(), "data": data}, f)
        except Exception as exc:
            logger.warning("Failed to write delivery data cache: %s", exc)

    @staticmethod
    def _read_cache() -> dict[str, float]:
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
            return payload.get("data", {})
        except Exception as exc:
            logger.warning("Failed to read delivery data cache: %s", exc)
            return {}


def symbol_without_suffix(symbol: str) -> str:
    """'RELIANCE.NS' -> 'RELIANCE' (NSE bhavcopy symbols have no suffix)."""
    return symbol.split(".")[0].upper()
