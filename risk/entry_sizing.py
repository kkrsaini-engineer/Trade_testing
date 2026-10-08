"""
2026-10-08 (user-approved "A-plan") — ONE place for the rules that decide
how many new positions a morning may open and how much money each gets.

Used by BOTH scripts/morning_executor.py (live paper trading) and
analytics/backtest_engine.py, so the backtest cannot drift from live.
Pure functions, no I/O. Direction-neutral: a SELL position is sized and
counted exactly like a BUY.

Rules (all as a fraction of TOTAL capital, not of what is left):
  - each position gets POSITION_FRACTION (5%) of total capital;
  - never below MIN_POSITION_VALUE (Rs 10k) — below that the flat Rs 15.93
    DP charge alone eats ~0.16%+ of the position (round trip ~0.6% at
    Rs 5k vs ~0.39% at Rs 25k);
  - total money in positions may not pass MAX_EXPOSURE (85%, so 15% stays
    as cash) — the last entry is shrunk to fit, not just refused;
  - one morning may deploy at most MAX_MORNING_DEPLOY (40%) — a day's
    entries are one correlated bet on the same market move;
  - at most MAX_NEW_ENTRIES_PER_DAY (10) new positions per morning,
    best-ranked first.
"""

from __future__ import annotations

import os

DEFAULT_MAX_NEW_ENTRIES_PER_DAY = 10
POSITION_FRACTION = 0.05
MIN_POSITION_VALUE = 10_000.0
MAX_EXPOSURE = 0.85
MAX_MORNING_DEPLOY = 0.40

# Live executor only (env override); the backtest takes the cap as an argument.
# 0 = no cap.
MAX_NEW_ENTRIES_PER_DAY = int(os.getenv("MAX_NEW_ENTRIES_PER_DAY", str(DEFAULT_MAX_NEW_ENTRIES_PER_DAY)))


def entry_allocation(
    total_capital: float,
    available_capital: float,
    used_capital: float,
    deployed_this_morning: float,
) -> tuple[float, str | None]:
    """
    Rupees to put into ONE new position, or (0.0, reason) when the morning
    has no room left for a position of at least MIN_POSITION_VALUE.

    The reason names the limit that ran out first, so the executor can say
    why it stopped ("exposure", "morning deploy limit", "cash").
    """
    total = float(total_capital)
    target = total * POSITION_FRACTION
    room = {
        "exposure limit": total * MAX_EXPOSURE - float(used_capital),
        "morning deploy limit": total * MAX_MORNING_DEPLOY - float(deployed_this_morning),
        "cash": float(available_capital),
    }
    allocation = min(target, *room.values())
    # The Rs 10k floor never exceeds the normal 5% size itself, so a small
    # test account (5% = Rs 5k) still trades at its normal size.
    if allocation >= min(MIN_POSITION_VALUE, target):
        return allocation, None
    return 0.0, min(room, key=room.get)


def quantity_for(allocation: float, price: float) -> int:
    """Whole shares that fit in `allocation` (0 if one share costs more)."""
    if price is None or price <= 0 or allocation <= 0:
        return 0
    return int(allocation // price)
