# PHASE 12 — Fix #5 (late-entry architecture / Trend-Setup-Entry separation)

Implemented on explicit request ("now fix 5th one, also check wired"). This
is the item the critique itself flagged as the biggest scope/risk on the
whole list — read the scope decision below before treating this as "the
full fix."

---

## What the critique asked for, and what was actually built

The critique's own verdict drew a clear line: the raw ingredients
(`ema_fresh_cross`, `rsi_fresh_cross`, `squeeze_breakout`, `pullback_entry`,
`not_overextended`) already exist from earlier phases — what's missing is
that they're flattened into one weighted blend instead of a real state
machine, so "TREND=bullish + SETUP=old breakout + ENTRY=extended" could
still mathematically qualify if other, unrelated checks compensated. The
critique's own disadvantage section says the FULL fix (new dataclasses,
new decision layer, `qualified` re-derived from state combinations instead
of a threshold, persisted breakout-age tracking across scan days) is
"bigger than anything done in Phases 2-5... needs the heaviest testing of
anything proposed here."

**Decision made here, consistent with how #4/#6/#8/#12 were each scoped
in this review:** implement the smaller, well-bounded half — a genuine new
HARD-REJECT rule that closes the specific gap (staleness isn't gated at
all today), reusing signals that already exist in a single scan (no new
persisted cross-day state) — rather than the full architecture rewrite.
**Not done:** explicit `TrendState`/`SetupState`/`EntryState` dataclasses
replacing the checks dict; `qualified` re-derived purely from state
combinations instead of a score threshold; persisted breakout-age tracking
across scan days. Those remain a separate, larger, not-yet-requested item.

**Important clarification on the critique's own example:** "TREND=bullish
+ SETUP=old breakout + ENTRY=extended" combines two conditions. The
EXTENDED half of that was **already** a hard reject before this phase
(`not_overextended`, tightened further by fix #12's ATR-normalization) —
an extended setup already cannot qualify regardless of score, with or
without this fix. The genuinely missing piece — confirmed by tracing the
existing code before writing anything — was **staleness on its own**: a
setup can be well within the ATR-normalized extension cap (not "too far"
from EMA20) and still be stale (nothing timing-specific happening today),
and that dimension had zero gating before this fix. This fix targets that
specific, previously-unfixed gap.

---

## Fix applied

A new hard-reject check, `checks["not_stale_entry"]`, added right after
fix #12's `not_overextended` in both `buy_strategy.py` and
`sell_strategy.py`:

```python
FRESH_TRIGGER_CHECKS = (
    "ema_fresh_cross", "rsi_fresh_cross",
    "squeeze_breakout", "pullback_entry", "confirmed_breakout",  # confirmed_breakdown for SELL
)
has_fresh_trigger = any(checks.get(key, False) for key in FRESH_TRIGGER_CHECKS)
is_running_move = bool(row.get("is_breakout", False))  # is_breakdown for SELL
setup_is_stale = is_running_move and not has_fresh_trigger

checks["not_stale_entry"] = not setup_is_stale
```

**"Stale" defined precisely:** `is_breakout` (`close > the prior 20-day
high`, see `features/indicators/breakout.py`) is running — the move is
underway — but NONE of the checks that specifically detect a fresh,
timing-sensitive trigger fired today. `confirmed_breakout` (today's volume
+ volume-spike confirmation) counts as "fresh enough" even on an
already-running breakout — a currently-strengthening move with real volume
behind it today is a legitimately different situation from a dormant one
with nothing happening.

**No new state needed:** every signal `has_fresh_trigger` checks already
looks back a few rows within the SAME scan's dataframe
(`EMA_CROSS_LOOKBACK`, `RSI_CROSS_LOOKBACK`, `SQUEEZE_LOOKBACK` — all
small, existing, single-scan lookback windows), not across separate scan
days. `is_breakout`/`is_breakdown` are already-computed columns. This is
why the fix stayed within the existing checks-dict shape, matching the
"stayed within the existing shape" pattern the critique noted about
Phases 2-5.

**Wired the same way as fix #12's `not_overextended`:**

- Added to `tier1_and_context_keys` (excluded from the Tier2 factor-score
  average — it's a hard reject, not a graded vote, mirroring
  `not_overextended`'s exclusion).
- Added to `qualified`'s formula: `tier1_passed and overall_score >=
  QUALIFY_THRESHOLD and checks["not_overextended"] and
  checks["not_stale_entry"]` — this is the literal implementation of
  "hard NO_TRADE even if score=92," now for the staleness dimension.
  Confirmed via a live test: a synthetic row scoring well into qualifying
  territory produces `action=NO_TRADE` purely because of this new check.
- A dedicated reasons line at the check's computation site, plus a
  second summary reason line near "Weighted score:" (mirroring
  `not_overextended`'s existing two-reasons pattern).
- Fix #15's `state_narrative` updated to reflect this: `setup_state` now
  has a `STALE_BREAKOUT`/`STALE_BREAKDOWN` value distinct from `NONE`,
  and `entry_state` has a new `REJECTED_STALE_ENTRY` value (checked
  before `REJECTED_OVEREXTENDED` in the priority chain, since they're
  independent hard rejects).

---

## REAL BEHAVIOR CHANGE — flagged explicitly, this is the important part

Unlike fix #12 (which was recalibrated specifically to minimize
unintended behavior drift for typical stocks), this fix is a **genuinely
new gate with no prior equivalent** — some fraction of currently-qualifying
BUY/SELL setups that are NOT extended but ARE stale (an established,
running breakout with no fresh trigger today) will now correctly get
rejected that previously were not. This is the intended effect of the
fix, not a bug, but its real-world magnitude cannot be validated in this
sandbox — there is no historical data access here to backtest how large a
fraction of currently-qualifying trades this affects. The critique itself
called for "the heaviest testing of anything proposed here"; the testing
done for this delivery (below) proves the logic is CORRECT (does what it's
designed to do, doesn't crash, integrates cleanly with #12/#15, all prior
regression tests still pass unchanged), not that the CALIBRATION (which
specific checks count as "fresh enough") is optimal for real trading
outcomes. Recommend watching paper-trading `NO_TRADE` rates and the new
`REJECTED_STALE_ENTRY` reason specifically once this is live, the same way
fix #12's multiplier would benefit from real-data tuning.

**Advantage:** closes a real, previously-unfixed gap — a stale setup can
no longer slip through purely because other, unrelated checks (fundamentals,
oscillators, sector) compensate in the weighted blend; this was exactly
the critique's core complaint. **Disadvantage:** uncalibrated against real
outcomes (same caveat as fix #12, for the same sandbox-access reason); a
plausible source of over-rejection if "fresh enough" is defined too
narrowly — worth monitoring, not blocking, given the fix is directionally
correct and reasonably conservative (fresh-volume confirmation alone
already counts as fresh).

---

## Wiring verification

`checks["not_stale_entry"]` is computed and gates `qualified` entirely
inside `BuyStrategyEngine.evaluate()`/`SellStrategyEngine.evaluate()` —
the SAME methods `execution/scanner.py:447/454` call on every real scan
(no scanner.py changes were needed for the gating itself, since
`buy_decision.action`/`buy_decision.passed` were already wired end-to-end
from earlier phases). Confirmed via a live `evaluate()` call: a
synthetic row with `is_breakout=True` and no fresh trigger (confirmed
`confirmed_breakout=False` in diagnostics) produces `action=NO_TRADE`
purely from this new check, while an otherwise-identical row WITH fresh
volume confirmation (`confirmed_breakout=True`) produces `action=BUY` —
proving the gate is live and directionally correct, not a dead check.
Also confirmed `state_narrative` (fix #15) correctly reflects the new
outcome (`SetupState=STALE_BREAKOUT`, `EntryState=REJECTED_STALE_ENTRY`)
on the same test row — the two fixes integrate cleanly.

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_strategy.py`,
  `strategy/sell_strategy.py` — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check — clean.
- Full regression re-run of all 6 prior phase test scripts — all pass, no
  regressions (existing fixtures default `is_breakout`/`is_breakdown` to
  `False`, so the new check doesn't fire unexpectedly on any of them —
  confirmed by inspection, not just by the tests happening to pass).
- New targeted smoke tests (this phase): stale-vs-fresh comparison with
  the same `is_breakout=True` but different volume confirmation, proving
  the gate fires/doesn't-fire correctly and that `action` genuinely flips
  from `NO_TRADE` to `BUY` purely based on it (i.e. score alone would
  have qualified); `state_narrative` integration check.

## Files changed

- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
