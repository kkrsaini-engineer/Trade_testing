"""
Rolling liquidity-history accumulator.

NSE's daily bhavcopy only ever gives ONE day's numbers per fetch — there
is no historical bulk-download in this pipeline's normal daily run (see
data/delivery_data.py). To get a genuine multi-day rolling average (the
same thing volume_sma_20 already does for `volume`, mirrored here for
trade-count / turnover / Amihud-illiquidity), each day's single-day
numbers must be appended to a small persisted history file, with old
entries pruned once the window is full.

Persistence: storage/reports/liquidity_history.json, committed back to
the repo by daily_scan.yml (same mechanism already used for
delivery_data_cache.json — see that workflow's
`git add -A -- reports/ storage/trades/ storage/reports/` step) — so the
window survives across GitHub Actions runs without needing to re-fetch
old bhavcopy files.

COLD-START, STATED HONESTLY: the very first time this ships, every
symbol's history is empty. It takes WINDOW_DAYS trading days of normal
runs for a symbol's window to fill up. Before MIN_HISTORY_DAYS entries
exist, rolling_liquidity_stats() returns None on purpose — callers must
fall back to a neutral score rather than compare today's numbers against
too little history (comparing day 1 against itself would trivially
always score "perfectly normal", which is misleading, not accurate).

MISSED DAYS: if a day's bhavcopy fetch fails (holiday/network/NSE
outage), that day simply never gets appended — no entry, no gap-filled
guess. This mirrors data/delivery_data.py's own convention: absence is
represented as an omitted field, never a fabricated value.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)

_HISTORY_PATH = "storage/reports/liquidity_history.json"

# How many trading-day entries to keep per symbol. Matches the 20-day
# window already used elsewhere in this codebase (volume_sma_20, OBV's
# rolling window, CMF) for consistency rather than inventing a new
# lookback period.
WINDOW_DAYS = 20

# Minimum entries (excluding today) required before a rolling comparison
# is trusted — see the cold-start note above. Below this, a symbol's
# rolling stats are too thin to mean anything and callers should treat
# it as "no signal yet", not a real reading.
MIN_HISTORY_DAYS = 5


def load_history() -> dict[str, list[dict[str, Any]]]:
    """Read the persisted history file. Returns {} on any failure
    (missing file, corrupt JSON, unexpected shape) — never raises,
    same convention as DeliveryDataProvider._read_cache()."""
    try:
        path = Path(_HISTORY_PATH)
        if not path.exists():
            return {}
        with open(path) as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            logger.warning("Liquidity history file has unexpected shape — ignoring.")
            return {}
        return payload
    except Exception as exc:
        logger.warning("Failed to read liquidity history: %s", exc)
        return {}


def save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    """Persist the (already pruned) history dict. Failure here is
    logged, not raised — a scan run must never crash because the
    liquidity-history file couldn't be written."""
    try:
        path = Path(_HISTORY_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(history, f)
    except Exception as exc:
        logger.warning("Failed to write liquidity history: %s", exc)


def append_and_prune(
    history: dict[str, list[dict[str, Any]]],
    today_data: dict[str, dict[str, float]],
    as_of: date,
    window_days: int = WINDOW_DAYS,
) -> dict[str, list[dict[str, Any]]]:
    """Append today's per-symbol bhavcopy numbers to `history` in place,
    pruning each symbol's entries down to the most recent `window_days`.

    Re-run safe: calling this twice for the same `as_of` date replaces
    (not duplicates) that date's entry per symbol — a scan re-run on the
    same day does not distort the rolling average.

    `today_data` is the {symbol: {field: value}} dict returned by
    DeliveryDataProvider.fetch_latest() (or a single-symbol subset of
    it) — only symbols present in it are touched; every other symbol's
    existing history is left untouched.
    """
    as_of_str = as_of.isoformat()

    for symbol, fields in today_data.items():
        entries = history.setdefault(symbol, [])
        entries[:] = [e for e in entries if e.get("date") != as_of_str]
        entries.append(
            {
                "date": as_of_str,
                "ttl_trd_qnty": fields.get("ttl_trd_qnty"),
                "no_of_trades": fields.get("no_of_trades"),
                "turnover_lacs": fields.get("turnover_lacs"),
                "close_price": fields.get("close_price"),
                "prev_close": fields.get("prev_close"),
            }
        )
        entries.sort(key=lambda e: e.get("date", ""))
        if len(entries) > window_days:
            del entries[: len(entries) - window_days]

    return history


def rolling_liquidity_stats(
    history: dict[str, list[dict[str, Any]]],
    symbol: str,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> dict[str, float] | None:
    """Returns rolling-window stats for `symbol`, or None if there isn't
    enough history yet to trust a comparison (cold-start guard — see
    module docstring). Callers MUST treat None as "no rolling signal
    available" and fall back to a neutral score, never fabricate one
    from too little data.

    Returned dict (only keys with enough underlying data are included):
      - window_size: how many day-entries this average was built from
      - avg_trade_size: mean of (ttl_trd_qnty / no_of_trades) across
        the window — a big/small trade-size day compares against this
      - avg_amihud: mean of (|pct_change| / turnover_lacs) across the
        window — the Amihud (2002) illiquidity proxy; higher means the
        price moves more per unit of turnover, i.e. thinner liquidity
    """
    entries = history.get(symbol, [])
    if len(entries) < min_history_days:
        return None

    trade_sizes: list[float] = []
    amihud_values: list[float] = []
    for e in entries:
        qty = e.get("ttl_trd_qnty")
        trades = e.get("no_of_trades")
        turnover = e.get("turnover_lacs")
        close = e.get("close_price")
        prev_close = e.get("prev_close")

        if qty and trades and trades > 0:
            trade_sizes.append(qty / trades)

        if turnover and turnover > 0 and close is not None and prev_close:
            pct_change = abs((close - prev_close) / prev_close)
            amihud_values.append(pct_change / turnover)

    if not trade_sizes and not amihud_values:
        return None

    stats: dict[str, float] = {"window_size": float(len(entries))}
    if trade_sizes:
        stats["avg_trade_size"] = sum(trade_sizes) / len(trade_sizes)
    if amihud_values:
        stats["avg_amihud"] = sum(amihud_values) / len(amihud_values)
    return stats


def today_trade_quality(fields: dict[str, float]) -> dict[str, float]:
    """Computes TODAY's own avg_trade_size / Amihud value (no history
    needed) from a single day's bhavcopy fields, for comparison against
    rolling_liquidity_stats()'s window average. Missing/zero inputs
    simply omit that key — never a fabricated 0 or fallback guess."""
    out: dict[str, float] = {}

    qty = fields.get("ttl_trd_qnty")
    trades = fields.get("no_of_trades")
    if qty and trades and trades > 0:
        out["avg_trade_size"] = qty / trades

    turnover = fields.get("turnover_lacs")
    close = fields.get("close_price")
    prev_close = fields.get("prev_close")
    if turnover and turnover > 0 and close is not None and prev_close:
        out["amihud"] = abs((close - prev_close) / prev_close) / turnover

    return out
