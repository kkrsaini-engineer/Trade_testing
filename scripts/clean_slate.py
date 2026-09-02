"""
CLEAN SLATE — Reset all accumulated trading/analysis data.

Deletes the data-accumulation files identified as containing a mix of
pre-fix (buggy) and post-fix trade records, so future analysis starts
from a genuinely clean, consistent baseline.

SAFETY: defaults to a DRY RUN (lists what would be deleted, deletes
nothing). Pass --confirm to actually perform the deletion.

Explicitly protected (never touched by this script):
  - storage/watchlist/nifty500.json   (the stock watchlist — config, not trade data)
  - reports/backtest_baseline.json    (about to be overwritten by a fresh backtest run anyway)
  - reports/backtest_result_latest.json
  - reports/regression_result_latest.json

Usage:
    python scripts/clean_slate.py              # dry run — shows what would be deleted
    python scripts/clean_slate.py --confirm     # actually deletes

TELEGRAM NOTIFICATION (added 2026-08-31): a completion notification for
this exact script was reported as previously working, but was found
NOT present in this repo's history (nor PRO_TRADER's) — only in a
third, separate repo not covered by this fix. Added here for parity,
using the same core/notifications.notify() helper every other
production script already uses. Only fires on an actual --confirm run
(a dry run changes nothing, so nothing to announce as "complete").

BUGFIXES (2026-09-02), found by auditing this script against what the
pipeline actually writes today:
  1. Five entries below used to say ".json" for files the pipeline has
     since switched to writing as ".jsonl" (analysis_history,
     learning_picks_history, learning_metrics_history,
     learning_observations, market_intelligence_log). Path(...).exists()
     on the wrong extension always reported "already absent", so a
     --confirm run silently never deleted these — confirmed live on
     GitHub: all 5 real .jsonl files were still sitting there,
     untouched, after the most recent clean-slate run. Extensions
     corrected below.
  2. TradeStore.save_trade() (storage/trades/trade_store.py) writes one
     loose per-trade detail file directly into storage/trades/ for
     every trade — paper_<SYMBOL>_<epoch_ms>.json or
     trade_<epoch_ms>.json — separate from the storage/trades/diary/
     directory this script already deletes. These were never covered
     at all, so pre-reset trade files kept accumulating forever
     alongside fresh post-reset ones (confirmed live on GitHub: 170+
     such files present, many pre-dating the last clean-slate run).
     Now swept via FILE_GLOBS_TO_DELETE below.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.notifications import notify  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402

FILES_TO_DELETE = [
    # Trades / Portfolio
    "storage/trades/trades_master.csv",
    "storage/trades/virtual_portfolio_state.json",
    "storage/trades/learning_exclusions.json",
    # Reports / Analysis
    "reports/full_report.csv",
    "reports/daily_scan_results.csv",
    "reports/paper_trading_daily_report.csv",
    "reports/analysis_history.jsonl",
    "reports/analysis_summary.json",
    "reports/learning_observation_latest.json",
    "reports/learning_picks_history.jsonl",
    "reports/learning_metrics_history.jsonl",
    "reports/optimizer_recommendations_latest.json",
    "reports/sector_performance_latest.json",
    "reports/paper_trading_summary_latest.json",
    # Storage / Reports (logs + caches)
    "storage/reports/learning_observations.jsonl",
    "storage/reports/market_intelligence_log.jsonl",
    "storage/reports/master_report.csv",
    "storage/reports/mi_last_summary_state.json",
    "storage/reports/telegram_dedup.json",
    "storage/reports/macro_headlines_cache.json",
]

# Loose per-trade detail files written directly into storage/trades/ by
# TradeStore.save_trade() — one file per trade, NOT inside the diary/
# directory below, so they need their own glob-based sweep rather than
# an exact-path entry in FILES_TO_DELETE above.
FILE_GLOBS_TO_DELETE = [
    "storage/trades/paper_*.json",
    "storage/trades/trade_*.json",
]

DIRECTORIES_TO_DELETE = [
    "storage/trades/diary",
]

# Explicitly never touched, listed here only so it's visible in a diff
# if someone is tempted to add it above.
PROTECTED = [
    "storage/watchlist/nifty500.json",
    "reports/backtest_baseline.json",
    "reports/backtest_result_latest.json",
    "reports/regression_result_latest.json",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually perform the deletion. Without this flag, only a dry-run preview is shown.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("CLEAN SLATE" + (" (DRY RUN — nothing will be deleted)" if not args.confirm else " (LIVE — deleting now)"))
    print("=" * 60)

    found_files = [f for f in FILES_TO_DELETE if Path(f).exists()]
    missing_files = [f for f in FILES_TO_DELETE if not Path(f).exists()]
    found_dirs = [d for d in DIRECTORIES_TO_DELETE if Path(d).exists()]

    # Sorted for stable, reproducible output across runs/platforms.
    found_glob_files = sorted(
        str(p) for pattern in FILE_GLOBS_TO_DELETE for p in Path(".").glob(pattern)
    )

    print(f"\nFiles to delete ({len(found_files)} found, {len(missing_files)} already absent):")
    for f in found_files:
        size = Path(f).stat().st_size
        print(f"  [{'DELETE' if args.confirm else 'WOULD DELETE'}] {f} ({size:,} bytes)")
    for f in missing_files:
        print(f"  [skip — already absent] {f}")

    print(f"\nLoose per-trade files to delete ({len(found_glob_files)} found, "
          f"matching {', '.join(FILE_GLOBS_TO_DELETE)}):")
    for f in found_glob_files:
        size = Path(f).stat().st_size
        print(f"  [{'DELETE' if args.confirm else 'WOULD DELETE'}] {f} ({size:,} bytes)")

    print(f"\nDirectories to delete ({len(found_dirs)} found):")
    for d in found_dirs:
        n_files = sum(1 for _ in Path(d).rglob("*") if _.is_file())
        print(f"  [{'DELETE' if args.confirm else 'WOULD DELETE'}] {d}/ ({n_files} files inside)")

    print(f"\nProtected — never touched: {', '.join(PROTECTED)}")

    if not args.confirm:
        print("\nDry run complete. Re-run with --confirm to actually delete these files.")
        return

    print("\nDeleting...")
    for f in found_files:
        Path(f).unlink()
        print(f"  Deleted: {f}")
    for f in found_glob_files:
        Path(f).unlink()
        print(f"  Deleted: {f}")
    for d in found_dirs:
        shutil.rmtree(d)
        print(f"  Deleted: {d}/")

    print("\nDone. All data-accumulation files cleared. Config/watchlist/backtest-baseline untouched.")

    # Named individually — this list is short and bounded (FILES_TO_DELETE +
    # DIRECTORIES_TO_DELETE have a fixed, small size).
    named_deleted = found_files + [f"{d}/" for d in found_dirs]
    deleted_lines = (
        "\n".join(f"  - {f}" for f in named_deleted) if named_deleted else "  (nothing found to delete)"
    )
    # Loose per-trade files are summarized as a COUNT, not listed one by
    # one — there can be hundreds of these, and Telegram messages have a
    # hard length limit (4096 chars), so listing every filename here
    # would risk silently truncating or failing the notification itself.
    glob_summary = (
        f"\n\nLoose per-trade files deleted: {len(found_glob_files)} "
        f"(matching {', '.join(FILE_GLOBS_TO_DELETE)})"
        if found_glob_files else ""
    )
    notify(
        event_type="clean_slate_complete",
        message=(
            "🧹 Clean Slate Complete\n\n"
            f"Files deleted ({len(named_deleted)}):\n{deleted_lines}"
            f"{glob_summary}\n\n"
            f"Protected (untouched): {', '.join(PROTECTED)}"
        ),
        dedup_key=f"clean_slate_complete::{now_ist().strftime('%Y-%m-%d %H:%M:%S.%f')}",
    )


if __name__ == "__main__":
    main()
