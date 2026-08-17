"""
NSE Circuit-Limit Detection.

Responsibilities:
- Fetch NSE's per-stock price-band assignments (2%/5%/10%/20%) from the
  free, public sec_list.csv file — the only official free NSE source for
  this, and only a coarse/occasionally-updated snapshot list, not a
  daily circuit-hit report (see PHASE25_NOTES.md for the research this
  is based on — NSE's bhavcopy itself carries NO circuit-limit field at
  all, confirmed against both current and legacy bhavcopy formats).
- Detect, from a single day's OHLC bar, whether that day likely closed
  circuit-locked (upper or lower) — a HEURISTIC, not a guaranteed
  ground-truth signal: closing at the session extreme (== high for an
  up-move, == low for a down-move) combined with a % move from the
  previous close landing near a known circuit-band percentage.

No strategy/decision logic — this module only fetches/computes signals.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

from core.logger import get_logger

logger = get_logger(__name__)

_SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
_WARMUP_URL = "https://www.nseindia.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/regulations/daily-price-bands-reports",
}

_REQUEST_TIMEOUT_SECONDS = 15
_RETRY_ATTEMPTS = 2

_CIRCUIT_BANDS_CACHE_PATH = "storage/reports/circuit_bands_cache.json"

# NSE's standard cash-market circuit-filter percentages (source: NSE's
# published sec_list.csv "Band" column + Zerodha's public circuit-limit
# explainer — see PHASE25_NOTES.md). F&O-active stocks don't have a fixed
# band at all (dynamic widening bands instead: starts at 10%, expands in
# stages up to 30%) — this list is only meaningful for non-F&O stocks,
# hence detect_circuit_lock()'s tolerance-based matching, not an exact
# lookup.
STANDARD_BANDS_PERCENT = (2.0, 5.0, 10.0, 20.0)

DEFAULT_TOLERANCE_PERCENT = 0.75


class CircuitBandsProvider:
    """Fetch and cache NSE's per-symbol circuit-band assignment list."""

    def fetch_bands(self) -> dict[str, float]:
        """Returns {NSE_SYMBOL: band_percent}, e.g. {"ACC": 20.0, ...}.

        Deliberately returns an EMPTY dict (not a fabricated default band
        for every symbol) on total failure — same reasoning as
        data/fii_dii_data.py: callers must treat a missing entry as
        "unknown band, fall back to checking against all standard
        bands", never assume any particular one.
        """
        session = requests.Session()
        session.headers.update(_HEADERS)

        try:
            session.get(_WARMUP_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning(
                "NSE warm-up request failed (%s) — attempting sec_list fetch anyway.",
                exc,
            )

        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                resp = session.get(_SEC_LIST_URL, timeout=_REQUEST_TIMEOUT_SECONDS)
                resp.raise_for_status()
                result = self._parse_csv(resp.text)
                if result:
                    self._write_cache(result)
                    return result
            except Exception as exc:
                logger.warning(
                    "sec_list.csv fetch attempt %d/%d failed: %s",
                    attempt,
                    _RETRY_ATTEMPTS,
                    exc,
                )
                if attempt < _RETRY_ATTEMPTS:
                    time.sleep(2.0)

        logger.warning(
            "Circuit-band list unavailable live — falling back to last cached snapshot, if any."
        )
        cached = self._read_cache()
        return cached or {}

    @staticmethod
    def _parse_csv(text: str) -> dict[str, float]:
        bands: dict[str, float] = {}

        lines = text.strip().splitlines()

        if not lines:
            return bands

        header = [c.strip().upper() for c in lines[0].split(",")]

        try:
            symbol_idx = header.index("SYMBOL")
            band_idx = header.index("BAND")
        except ValueError:
            logger.warning(
                "sec_list.csv header did not contain expected columns: %r",
                header,
            )
            return bands

        for line in lines[1:]:
            parts = line.split(",")

            if len(parts) <= max(symbol_idx, band_idx):
                continue

            symbol = parts[symbol_idx].strip().upper()

            try:
                band = float(parts[band_idx].strip())
            except ValueError:
                continue

            if symbol:
                bands[symbol] = band

        return bands

    @staticmethod
    def _write_cache(bands: dict[str, float]) -> None:
        try:
            path = Path(_CIRCUIT_BANDS_CACHE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(
                    {"cached_on": date.today().isoformat(), "bands": bands},
                    f,
                )
        except Exception as exc:
            logger.warning("Failed to write circuit-bands cache: %s", exc)

    @staticmethod
    def _read_cache() -> dict[str, float] | None:
        try:
            path = Path(_CIRCUIT_BANDS_CACHE_PATH)

            if not path.exists():
                return None

            with open(path) as f:
                payload = json.load(f)

            logger.warning(
                "Using cached circuit-band list from %s (live fetch failed today).",
                payload.get("cached_on", "unknown date"),
            )
            return payload.get("bands")
        except Exception as exc:
            logger.warning("Failed to read circuit-bands cache: %s", exc)
            return None


def detect_circuit_lock(
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    prev_close: float | None,
    band_percent: float | None = None,
    tolerance_percent: float = DEFAULT_TOLERANCE_PERCENT,
) -> dict[str, Any]:
    """Heuristic circuit-lock detection from a single day's OHLC bar.

    NOT a ground-truth signal — NSE's bhavcopy carries no circuit-limit
    field at all, so this infers the likely circuit state from price
    shape: closing at the session extreme (== high for an up-move, ==
    low for a down-move) combined with a % move from the previous close
    landing close to a known circuit-band percentage (the symbol's
    actual assigned band if known via `band_percent`, else any of the 4
    standard bands).

    Reliably catches a FULL-DAY freeze (open==high==low==close at the
    band). Under-detects a circuit that was touched and traded back off
    intraday (still a big move, but high != low that day). Unreliable
    for F&O-active stocks (dynamic, non-round bands — see module
    docstring) — callers should treat a "circuit_likely" result on an
    F&O-active symbol with extra skepticism, or skip the check for those
    symbols entirely.

    Returns a diagnostics dict — never raises, never fabricates a result
    on missing/invalid input (returns circuit_likely=False, not a guess).
    """

    result: dict[str, Any] = {
        "circuit_likely": False,
        "direction": None,
        "full_day_freeze": False,
        "move_percent": 0.0,
        "band_used": band_percent,
    }

    if prev_close is None or prev_close <= 0 or close is None:
        return result

    move_percent = ((close - prev_close) / prev_close) * 100.0

    result["move_percent"] = round(move_percent, 4)

    candidate_bands = (
        [band_percent] if band_percent is not None else list(STANDARD_BANDS_PERCENT)
    )

    near_a_band = any(
        abs(abs(move_percent) - band) <= tolerance_percent for band in candidate_bands
    )

    if not near_a_band:
        return result

    # close is always within [low, high] by construction of a real OHLC
    # bar — >=/<= (not ==) is used defensively against float precision
    # noise while remaining logically equivalent to "closed at the high"
    # / "closed at the low".
    closed_at_high = high is not None and close >= high

    closed_at_low = low is not None and close <= low

    if move_percent > 0 and closed_at_high:

        result["circuit_likely"] = True

        result["direction"] = "upper"

    elif move_percent < 0 and closed_at_low:

        result["circuit_likely"] = True

        result["direction"] = "lower"

    if result["circuit_likely"] and open_price is not None:

        result["full_day_freeze"] = open_price == high == low == close

    return result
