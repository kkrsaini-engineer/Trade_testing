"""
MODULE 5 — REGRESSION FRAMEWORK (CLI)

Thin command-line wrapper around analytics.regression_validator
.RegressionValidator (the single canonical implementation — no logic
duplicated here).

Roadmap coverage (Phase 2, Module 5):
    [x] Every future feature compared against the previous baseline
        — compare() diffs 9 metrics (win_rate, profit_factor, max_drawdown,
          cagr, expectancy, sharpe, sortino, avg_rr, total_trades) against
          reports/backtest_baseline.json and produces a KEEP/REVERT-style verdict.

Compares the latest backtest result (reports/backtest_result_latest.json,
produced by scripts/run_backtest.py) against a stored baseline
(reports/backtest_baseline.json). If no baseline exists yet, saves the
current result AS the new baseline instead of comparing (nothing to
compare against on the first run).

Every future strategy change should be re-backtested and re-checked
here before being considered "safe" — this is purely a comparison
report, it never modifies strategy code itself.

Usage:
    python scripts/regression_check.py
    python scripts/regression_check.py --set-baseline   # force current run to become the new baseline
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from analytics.regression_validator import RegressionValidator  # noqa: E402

logger = get_logger(__name__)

CURRENT_PATH = "reports/backtest_result_latest.json"
BASELINE_PATH = "reports/backtest_baseline.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--set-baseline", action="store_true",
        help="Save the current backtest result as the new baseline instead of comparing.",
    )
    args = parser.parse_args()

    if not Path(CURRENT_PATH).exists():
        print(f"No backtest result found at {CURRENT_PATH} — run scripts/run_backtest.py first.")
        return

    with open(CURRENT_PATH) as f:
        current = json.load(f)

    if args.set_baseline or not Path(BASELINE_PATH).exists():
        Path("reports").mkdir(exist_ok=True)
        with open(BASELINE_PATH, "w") as f:
            json.dump(current, f, indent=2, default=str)
        reason = "explicitly requested" if args.set_baseline else "no prior baseline existed"
        print(f"Saved current backtest result as the new baseline ({reason}). Nothing to compare yet.")
        return

    with open(BASELINE_PATH) as f:
        previous = json.load(f)

    # 2026-10-06 (BUG_AUDIT_2026-10-05_PROFITABILITY.md M9): the backtest
    # engine was rewritten to replay the real live flow. A baseline
    # produced by a different engine version measures a different
    # system, so comparing against it would give a meaningless
    # KEEP/REVERT verdict — replace the baseline instead.
    if previous.get("engine_version") != current.get("engine_version"):
        with open(BASELINE_PATH, "w") as f:
            json.dump(current, f, indent=2, default=str)
        print(
            f"Baseline was produced by backtest engine version "
            f"{previous.get('engine_version', '(pre-2026-10-06)')}, current is "
            f"{current.get('engine_version')} — not comparable. Saved the current "
            f"result as the new baseline instead."
        )
        return

    validator = RegressionValidator()
    result = validator.compare(previous, current)
    report_text = validator.report(result)
    print(report_text)

    Path("reports").mkdir(exist_ok=True)
    with open("reports/regression_result_latest.json", "w") as f:
        json.dump(result, f, indent=2, default=str)

    notify(
        event_type="regression_check",
        message=f"🔍 Regression Check\n\n{report_text}",
        dedup_key=f"regression_check::{time.strftime('%Y-%m-%d %H:%M:%S')}",
    )


if __name__ == "__main__":
    main()
