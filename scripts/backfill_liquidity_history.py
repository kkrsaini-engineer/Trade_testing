"""
One-time liquidity-history backfill.

WHY: strategy/*_scoring.py's _liquidity_score() only trusts a rolling
trade-size/Amihud comparison once a symbol has MIN_HISTORY_DAYS (5)
entries in storage/reports/liquidity_history.json, and the window isn't
FULL (WINDOW_DAYS=20) until 20 daily scan runs have happened — see
PHASE18_NOTES.md's cold-start caveat. This script closes that gap in
one run: it fetches the last WINDOW_DAYS (20) trading days' bhavcopy
files directly from NSE archives — the exact same file
data/delivery_data.py's fetch_latest() fetches for a single day, just
walked back over multiple dates via the new fetch_range() method — and
seeds the history file immediately, so the rolling window is already
full from the very first scan after this runs, instead of taking a
month of normal daily runs to warm up.

SCOPE: only symbols in storage/watchlist/nifty500.json are kept. The
bhavcopy itself covers the whole market (thousands of symbols); filtering
down to the actual watchlist keeps the persisted history file roughly
the same size it would naturally reach through 20 days of normal
scanning, not the entire market.

Run ONCE, manually:
    python3 scripts/backfill_liquidity_history.py

Re-run safe: append_and_prune() replaces (never duplicates) a same-date
entry per symbol, so running this again just re-fetches and overwrites
with the same numbers — harmless, just spends NSE requests it didn't
need to.

NOT part of daily_scan.yml's cron — that keeps doing single-day
appends via execution/scanner.py exactly as before (see PHASE18_NOTES.md
Part 3's wiring). This script is a manual/one-off bootstrap only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import get_logger  # noqa: E402
from data.delivery_data import DeliveryDataProvider, symbol_without_suffix  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from data import liquidity_history  # noqa: E402

logger = get_logger(__name__)

WATCHLIST_PATH = "storage/watchlist/nifty500.json"


def main() -> None:
    watchlist = WatchlistManager(WATCHLIST_PATH).load()
    if not watchlist:
        logger.warning("Watchlist is empty (%s) — nothing to backfill.", WATCHLIST_PATH)
        return
    watchlist_bare = {symbol_without_suffix(s) for s in watchlist}

    provider = DeliveryDataProvider()
    logger.info(
        "Fetching up to %d trading days of bhavcopy for %d watchlist symbols "
        "(this makes one NSE request per trading day, with a polite delay "
        "between requests — expect this to take a few minutes)...",
        liquidity_history.WINDOW_DAYS, len(watchlist_bare),
    )
    by_date = provider.fetch_range(trading_days=liquidity_history.WINDOW_DAYS)

    if not by_date:
        logger.warning("No bhavcopy data could be fetched for any day in the lookback window — nothing backfilled.")
        return

    history = liquidity_history.load_history()
    for target_date, day_data in by_date.items():  # already chronological, oldest first
        filtered = {sym: fields for sym, fields in day_data.items() if sym in watchlist_bare}
        liquidity_history.append_and_prune(history, filtered, target_date)
        # Save after every day, not just once at the end — resumable if
        # this crashes or is interrupted partway through.
        liquidity_history.save_history(history)
        logger.info(
            "Backfilled %s: %d/%d watchlist symbols matched.",
            target_date.isoformat(), len(filtered), len(watchlist_bare),
        )

    if len(by_date) < liquidity_history.WINDOW_DAYS:
        logger.warning(
            "Only %d/%d trading days backfilled (bhavcopy archive gap, network "
            "issue, or a long holiday stretch in the lookback window). Some "
            "symbols may still be below MIN_HISTORY_DAYS (%d) or have a partial "
            "window — this is not an error. Re-run this script later, or let "
            "daily scans fill the remaining days naturally.",
            len(by_date), liquidity_history.WINDOW_DAYS, liquidity_history.MIN_HISTORY_DAYS,
        )
    else:
        logger.info(
            "Backfill complete: %d trading days collected for %d watchlist symbols. "
            "The rolling window is now full — tonight's daily scan will use it "
            "immediately instead of starting cold.",
            len(by_date), len(watchlist_bare),
        )


if __name__ == "__main__":
    main()
