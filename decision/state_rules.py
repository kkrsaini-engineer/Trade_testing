"""
ENTRY STATE-TRANSITION RULE TABLE

Point 15 (ARCHITECTURE_REVIEW_16POINT.md — "State-based structure over
raw check counts"). See PHASE29_NOTES.md for the full write-up.

Both strategy/buy_strategy.py and strategy/sell_strategy.py used to
derive `qualified` from a hand-written `A and B and C and D` boolean
chain, and SEPARATELY — after the fact, purely for display — narrate a
"MarketState=.../TrendState=.../SetupState=.../EntryState=..." string
from a slightly different set of checks. Because the two were computed
independently, they could quietly disagree: `tier1_passed` (a 2-of-3
majority vote across {ema_alignment, market_trend, sma_alignment}) could
pass while the single-check `trend_state` (derived only from
price-vs-EMA20) said the opposite direction — e.g. a BUY could go
through as `action=BUY / EntryState=TRIGGERED` while its own
`TrendState=DOWNTREND`. See PHASE29_NOTES.md for a concrete before/after
example.

This module is the single source of truth instead: an ORDERED,
first-match-wins table that takes the states/facts already computed by
the caller and returns BOTH the qualification verdict and the
EntryState label in one place, so they can never diverge again. It is
direction-agnostic — BUY and SELL both call the exact same function,
passing in their own direction-specific state labels (e.g.
"DOWNTREND" is the unfavorable TrendState for a BUY, "UPTREND" is the
unfavorable TrendState for a SELL) — so a change to the rule ORDER or
ADDING a new rule automatically applies identically to both directions,
instead of relying on two hand-maintained copies staying in sync.

Deliberately narrow scope (see PHASE29_NOTES.md's scope discussion):
this table reuses ONLY the states/checks that already existed as hard
gates or presentational states in buy_strategy.py/sell_strategy.py
(TrendState, SetupState=stale, not_overextended, tier1_passed, the
score threshold). It does NOT introduce a new MarketState-based hard
reject (e.g. "BEAR market -> hard-reject all BUYs") — that would be a
materially different, much larger trading-behavior change with no
precedent in this codebase, and was explicitly scoped OUT.
"""

from __future__ import annotations

from dataclasses import dataclass

# EntryState labels — shared vocabulary for both directions.
REJECTED_TREND = "REJECTED_TREND"
REJECTED_STALE_ENTRY = "REJECTED_STALE_ENTRY"
REJECTED_OVEREXTENDED = "REJECTED_OVEREXTENDED"
WAITING = "WAITING"
TRIGGERED = "TRIGGERED"


@dataclass(slots=True, frozen=True)
class EntryStateResult:
    """The table's verdict: `entry_state` is now the ONE label that
    also directly determines `qualified`/`action` downstream — not a
    separate narration of a decision made elsewhere."""

    entry_state: str
    qualified: bool
    reject_reason: str | None


def evaluate_entry_state(
    *,
    trend_state: str,
    unfavorable_trend_state: str,
    tier1_passed: bool,
    setup_state: str,
    stale_setup_state: str,
    stale_reason: str,
    not_overextended: bool,
    overextended_reason: str,
    overall_score: float,
    qualify_threshold: float,
) -> EntryStateResult:
    """Ordered, first-match-wins entry-state rule table.

    Parameters are already-computed facts/states from the caller (no
    indicator math happens here) — this function only decides how they
    COMBINE into a final verdict:

    1. `trend_state == unfavorable_trend_state` -> hard reject. Closes
       the exact contradiction described in the module docstring: even
       if the 2-of-3 `tier1_passed` majority vote passed, a TrendState
       that's outright pointed the wrong way for this trade direction
       vetoes it, regardless of score.
    2. `not tier1_passed` -> hard reject (unchanged from before —
       core-trend majority vote failed).
    3. `setup_state == stale_setup_state` -> hard reject (unchanged —
       a running move with no fresh trigger today, chasing risk).
    4. `not not_overextended` -> hard reject (unchanged — ATR-normalized
       chase-risk cap breached).
    5. `overall_score < qualify_threshold` -> WAITING, not a hard
       reject — everything about the setup is fine, the score just
       isn't there yet.
    6. Otherwise -> TRIGGERED, qualified.
    """

    if trend_state == unfavorable_trend_state:
        return EntryStateResult(
            REJECTED_TREND, False,
            f"Rejected: TrendState={trend_state} contradicts the trade "
            f"direction, regardless of score.",
        )

    if not tier1_passed:
        return EntryStateResult(
            REJECTED_TREND, False,
            "Rejected: core trend not confirmed (Tier 1), regardless of score.",
        )

    if setup_state == stale_setup_state:
        return EntryStateResult(REJECTED_STALE_ENTRY, False, stale_reason)

    if not not_overextended:
        return EntryStateResult(REJECTED_OVEREXTENDED, False, overextended_reason)

    if overall_score < qualify_threshold:
        return EntryStateResult(WAITING, False, None)

    return EntryStateResult(TRIGGERED, True, None)
