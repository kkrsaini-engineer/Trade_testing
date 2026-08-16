"""
Bhavcopy fetch status log.

Tiny, purpose-built persisted log answering exactly one question: for
each recent scan day, was NSE bhavcopy (the delivery%/liquidity data
source — see data/delivery_data.py) actually available, or not? Used to
drive a Telegram notification (see scripts/generate_full_report.py)
when today's fetch failed outright or fell back to an earlier day's
data, with a trailing few-day audit trail alongside it.

Distinct from data/liquidity_history.py's per-symbol rolling window —
that records the fetched VALUES for symbols that matched; this records
the FETCH OUTCOME itself, once per scan day, independent of which
individual symbols happened to match.

Persisted: storage/reports/bhavcopy_status_log.json, committed back to
the repo by daily_scan.yml's existing commit step (same mechanism as
every other storage/reports/ file in this pipeline).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)

_STATUS_LOG_PATH = "storage/reports/bhavcopy_status_log.json"

# How many days of status history to retain — generous (well beyond the
# 4-day window the Telegram notification shows) so a longer lookback is
# available if ever needed, while still bounding file size.
MAX_LOG_ENTRIES = 30

STATUS_OK = "OK"          # today's bhavcopy, correctly dated, used as-is
STATUS_STALE = "STALE"    # an earlier day's bhavcopy was used (walk-back fallback)
STATUS_FAILED = "FAILED"  # no bhavcopy data at all (live fetch + cache both failed)


def load_status_log() -> dict[str, dict[str, Any]]:
    """Returns {date_str: {status, as_of, symbols_matched}}. {} on any
    read failure — never raises."""
    try:
        path = Path(_STATUS_LOG_PATH)
        if not path.exists():
            return {}
        with open(path) as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read bhavcopy status log: %s", exc)
        return {}


def record_status(
    scan_date: date,
    status: str,
    as_of: date | None,
    symbols_matched: int,
) -> dict[str, dict[str, Any]]:
    """Records today's fetch outcome (replacing, not duplicating, an
    existing entry for the same scan_date — re-run safe), prunes to the
    most recent MAX_LOG_ENTRIES, persists, and returns the updated log
    (so the caller can build a notification from it without a second
    disk read)."""
    log = load_status_log()
    log[scan_date.isoformat()] = {
        "status": status,
        "as_of": as_of.isoformat() if as_of else None,
        "symbols_matched": symbols_matched,
    }
    if len(log) > MAX_LOG_ENTRIES:
        for old_date in sorted(log.keys())[: len(log) - MAX_LOG_ENTRIES]:
            del log[old_date]

    try:
        path = Path(_STATUS_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(log, f)
    except Exception as exc:
        logger.warning("Failed to write bhavcopy status log: %s", exc)

    return log


def recent_entries(log: dict[str, dict[str, Any]], days: int = 4) -> list[tuple[str, dict[str, Any]]]:
    """The most recent `days` entries from `log`, oldest first — as
    (date_str, entry) pairs, ready for formatting into a notification.
    Fewer than `days` entries are returned as-is if the log doesn't
    have that much history yet (no fabricated padding)."""
    dates_sorted = sorted(log.keys())
    return [(d, log[d]) for d in dates_sorted[-days:]]
