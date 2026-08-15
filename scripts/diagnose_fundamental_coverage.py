"""
DIAGNOSTIC: FUNDAMENTAL DATA COVERAGE ACROSS THE WATCHLIST (READ-ONLY)

Does NOT modify anything, does NOT score/decide anything. Fetches raw
fundamentals for every symbol in the watchlist (the SAME provider
strategy/fundamental_scoring.py's callers use) and counts, per metric,
how many symbols come back with that field missing (None) — this is
the real-world number needed to judge whether the buy_fundamental_score()
/ sell_fundamental_score() "missing data -> treated as bad" default
(see strategy/fundamental_scoring.py's _safe_float() 999.0/0.0 defaults)
is a rare edge case or a common one worth a proper fix.

Lighter than diagnose_buy_pipeline.py / diagnose_buy_sell_aggregate.py:
skips technical indicators, news, and strategy evaluation entirely —
only the fundamentals fetch, so this runs faster across the full
watchlist.

Usage:
    python scripts/diagnose_fundamental_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from data.fundamental_data import FundamentalDataProvider  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402

logger = get_logger(__name__)

# The metrics buy_fundamental_score()/sell_fundamental_score() actually
# use (strategy/fundamental_scoring.py) — kept as a literal list here
# (not imported) so this diagnostic stays read-only and has zero
# dependency on scoring internals that could change independently.
TRACKED_METRICS = [
    "pe", "pb", "peg", "debt_to_equity",
    "revenue_growth", "earnings_growth", "roe", "operating_cashflow",
]

# The 4 metrics that specifically use the 999.0 "looks expensive/risky"
# default (as opposed to the 0.0 default the other 4 use) — the ones
# originally flagged in the chat discussion.
RATIO_METRICS_WITH_999_DEFAULT = ["pe", "pb", "peg", "debt_to_equity"]


def main() -> None:
    symbols = WatchlistManager("storage/watchlist/nifty500.json").load()
    total = len(symbols)
    print(f"Checking fundamental data coverage across {total} watchlist symbols...")
    print("(This fetches ONE thing per symbol — no technicals, no news, no scoring — "
          "so it's much faster than the full pipeline diagnostics.)\n")

    provider = FundamentalDataProvider()

    fetch_failures: list[str] = []
    missing_counts: dict[str, int] = {metric: 0 for metric in TRACKED_METRICS}
    missing_symbols: dict[str, list[str]] = {metric: [] for metric in TRACKED_METRICS}
    fetched_count = 0
    all_ratio_metrics_missing_symbols: list[str] = []

    for i, symbol in enumerate(symbols, start=1):
        try:
            data = provider.fetch(symbol)
        except Exception as exc:
            fetch_failures.append(symbol)
            logger.warning("Fetch failed for %s: %s", symbol, exc)
            continue

        fetched_count += 1
        symbol_missing_ratio_metrics = 0
        for metric in TRACKED_METRICS:
            if data.get(metric) is None:
                missing_counts[metric] += 1
                if len(missing_symbols[metric]) < 15:  # cap the example list, don't spam
                    missing_symbols[metric].append(symbol)
                if metric in RATIO_METRICS_WITH_999_DEFAULT:
                    symbol_missing_ratio_metrics += 1

        if symbol_missing_ratio_metrics == len(RATIO_METRICS_WITH_999_DEFAULT):
            all_ratio_metrics_missing_symbols.append(symbol)

        if i % 50 == 0 or i == total:
            print(f"  ...{i}/{total} checked")

    print("\n" + "=" * 60)
    print("FUNDAMENTAL DATA COVERAGE — RESULTS")
    print("=" * 60)
    print(f"Total watchlist symbols: {total}")
    print(f"Fetch succeeded: {fetched_count}")
    print(f"Fetch FAILED entirely (network/no data): {len(fetch_failures)}")
    if fetch_failures:
        print(f"  Failed symbols (first 15): {fetch_failures[:15]}")

    print(f"\nPer-metric missing count (out of {fetched_count} successfully-fetched symbols):")
    for metric in TRACKED_METRICS:
        count = missing_counts[metric]
        pct = (count / fetched_count * 100) if fetched_count else 0.0
        print(f"  {metric:<20} missing: {count:>4}/{fetched_count} ({pct:5.1f}%)")
        if count > 0:
            print(f"    e.g.: {missing_symbols[metric]}")

    print("\n" + "=" * 60)
    print("Worst-hit cases — symbols where ALL FOUR ratio metrics "
          "(pe/pb/peg/debt_to_equity) are missing simultaneously:")
    print("(these get the full 999.0-default penalty on 40/100 of the fundamental "
          "score's weight at once — this count is what actually decides whether "
          "the 'missing -> treated as bad' fix is worth doing)")
    print("=" * 60)
    pct_worst = (len(all_ratio_metrics_missing_symbols) / fetched_count * 100) if fetched_count else 0.0
    print(f"  {len(all_ratio_metrics_missing_symbols)}/{fetched_count} ({pct_worst:.1f}%)")
    if all_ratio_metrics_missing_symbols:
        print(f"  Symbols: {all_ratio_metrics_missing_symbols}")


if __name__ == "__main__":
    main()
