"""
CLEAN PORTFOLIO STATE (one-time repair utility)

The NaN-propagation bug (see CHANGELOG.md) could have already written
NaN into storage/trades/virtual_portfolio_state.json's cumulative
fields (total_pnl, available_capital, etc.) before the fix. Since NaN
is contagious through addition, once written this corruption persists
across every future day until manually repaired.

This script does NOT run automatically — it is a one-time, explicit
repair step. It:
  1. Loads the persisted state.
  2. Reports EVERY field that is currently NaN (full transparency).
  3. Repairs ONLY the specific NaN fields:
     - total_pnl -> reset to 0.0 (the corrupted historical P&L value
       is unrecoverable — this does not "hide" a live bug, it clears
       already-known-bad data at the user's explicit request)
     - available_capital -> recomputed from total_capital - used_capital
       (now that total_pnl is 0.0, matching portfolio.py's own formula)
     - Any NaN position-level field (current_price, unrealized_pnl,
       etc.) -> that position is left as-is structurally but flagged in
       the report for manual review (positions are NOT silently altered)
  4. Writes the repaired state back, and prints a before/after summary.

Usage:
    python scripts/clean_portfolio_state.py
    python scripts/clean_portfolio_state.py --path storage/trades/virtual_portfolio_state.json
    python scripts/clean_portfolio_state.py --dry-run   # report only, no write

TELEGRAM NOTIFICATION (added 2026-08-31): same parity fix as
scripts/clean_slate.py — see that file's matching note. Fires on the
"already clean" outcome (informational, true regardless of --dry-run)
and on an actual repair (only when a real write happened, i.e. not
--dry-run). A dry run that FOUND corruption but wrote nothing stays
silent here — nothing is "complete" yet, and the printed findings above
are for whoever is running this manually to act on.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402

logger = get_logger(__name__)


def _is_nan(value) -> bool:
    return isinstance(value, float) and math.isnan(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="storage/trades/virtual_portfolio_state.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"No state file found at {path} — nothing to clean.")
        return

    with open(path) as f:
        state = json.load(f)

    findings = []
    repairs = []

    for key in ("total_pnl", "total_pnl_percent", "available_capital", "used_capital", "exposure"):
        if _is_nan(state.get(key)):
            findings.append(f"Portfolio-level field '{key}' is NaN.")

    for symbol, pos in state.get("open_positions", {}).items():
        for field, value in pos.items():
            if _is_nan(value):
                findings.append(f"Open position {symbol}.{field} is NaN.")

    for i, pos in enumerate(state.get("closed_positions", [])):
        for field, value in pos.items():
            if _is_nan(value):
                findings.append(f"Closed position #{i} ({pos.get('symbol', '?')}).{field} is NaN.")

    if not findings:
        print("No NaN corruption found. State file is clean — no changes made.")
        notify(
            event_type="clean_portfolio_state_complete",
            message=(
                "✅ Portfolio Already Clear\n\n"
                f"File: {path}\n"
                "No corruption found — no changes made."
            ),
            dedup_key=f"clean_portfolio_state_complete::{now_ist().strftime('%Y-%m-%d %H:%M:%S.%f')}",
        )
        return

    print(f"Found {len(findings)} NaN field(s):")
    for f_ in findings:
        print(f"  - {f_}")

    if args.dry_run:
        print("\n--dry-run specified: no changes written.")
        return

    # Repair cumulative portfolio-level fields (these are the ones that
    # actually cascade/corrupt every future day — see CHANGELOG.md).
    if _is_nan(state.get("total_pnl")):
        state["total_pnl"] = 0.0
        repairs.append("total_pnl reset to 0.0 (corrupted historical value unrecoverable)")
    if _is_nan(state.get("total_pnl_percent")):
        state["total_pnl_percent"] = 0.0
        repairs.append("total_pnl_percent reset to 0.0")

    used = sum(
        pos.get("quantity", 0) * pos.get("entry_price", 0)
        for pos in state.get("open_positions", {}).values()
        if not _is_nan(pos.get("entry_price")) and not _is_nan(pos.get("quantity"))
    )
    if _is_nan(state.get("available_capital")) or _is_nan(state.get("used_capital")):
        state["used_capital"] = used
        state["available_capital"] = state["total_capital"] + state["total_pnl"] - used
        repairs.append(
            f"available_capital recomputed from total_capital + total_pnl - used_capital = "
            f"{state['available_capital']}"
        )
    if _is_nan(state.get("exposure")):
        state["exposure"] = used / max(state.get("total_capital", 1.0), 1e-9)
        repairs.append(f"exposure recomputed = {state['exposure']}")

    with open(path, "w") as f:
        json.dump(state, f, indent=2)

    print(f"\nRepaired {len(repairs)} field(s):")
    for r in repairs:
        print(f"  - {r}")

    position_level_findings = [f_ for f_ in findings if "position" in f_.lower()]
    if position_level_findings:
        print(
            f"\n{len(position_level_findings)} position-level NaN field(s) were found but "
            f"NOT automatically altered — review these manually (see list above)."
        )

    logger.info("Portfolio state cleaned: %s", path)

    repair_lines = "\n".join(f"  - {r}" for r in repairs)
    manual_review_note = (
        f"\n\n⚠️ {len(position_level_findings)} position-level NaN field(s) "
        "were NOT auto-altered — review manually."
        if position_level_findings else ""
    )
    notify(
        event_type="clean_portfolio_state_complete",
        message=(
            "🛠️ Portfolio State Repaired\n\n"
            f"File: {path}\n"
            f"Repaired ({len(repairs)}):\n{repair_lines}"
            f"{manual_review_note}"
        ),
        dedup_key=f"clean_portfolio_state_complete::{now_ist().strftime('%Y-%m-%d %H:%M:%S.%f')}",
    )


if __name__ == "__main__":
    main()
