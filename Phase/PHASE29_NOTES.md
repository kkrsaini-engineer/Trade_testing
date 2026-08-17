# Phase 29 — Point 15: State-Transition Rule Table Replaces the Ad-Hoc Qualification Chain (DONE)

Follow-up to PHASE27/28_NOTES.md's "Still open" list. You said "15 no
fix kro" (fix Point 15). This one needed real back-and-forth before
coding, because my own earlier notes (Phases 25-28) had mislabeled what
Point 15 even is — worth reading in full before the "what was fixed"
section.

## Correction to my own prior notes

Phases 25-28's "Still open" lists all called Point 15 "explicit
`position_state` state machine" — that's wrong. The actual source
(`ARCHITECTURE_REVIEW_16POINT.md`, item #15, "State-based structure over
raw check counts") is about the ENTRY-side decision narrative
(`MarketState`/`TrendState`/`SetupState`/`EntryState`), not a position
lifecycle (OPEN/PARTIAL/CLOSED) state machine. I mislabeled this across
4 phases without ever re-checking the source document. Correcting it
here.

## Scope, confirmed in three rounds

The review document itself lays out two very different implementations:
a low-risk "presentation layer built on top of existing scoring" (which,
it turns out, was **already built** — see below), or a much bigger
"replace scoring with hard state-transition rules," which the review's
own text says is "the same scope/risk as fix #5" (late-entry
architecture) — explicitly flagged in that same document as needing
"the heaviest testing of anything proposed here." You picked the bigger
option, twice, when I offered the safer default. Given the live-trading
risk, before writing any code I researched the actual current
architecture (both buy_strategy.py and sell_strategy.py, ~2700 lines
combined) and came back with a **concrete, evidence-based, tightly
scoped** design — not the open-ended "add MarketState-based hard
rejects" version — which you then confirmed exactly.

## What research found

`buy_strategy.py`/`sell_strategy.py` already compute a
`state_narrative` string (`"MarketState=BULL / TrendState=UPTREND /
SetupState=PULLBACK / EntryState=TRIGGERED"`) — this was the
**presentation-only half of Point 15, already done**, with an explicit
comment on `BuyDecision.state_narrative` saying so. What was missing:
`entry_state` was computed AFTER `qualified` (a separate `A and B and C
and D` boolean chain), purely to narrate it — the two were computed via
overlapping but NOT identical logic, and could disagree.

**Concrete bug this found** (empirically reproduced, not theoretical):
`tier1_passed` is a 2-of-3 majority vote across `{ema_alignment,
market_trend, sma_alignment}`. `trend_state` is derived from a single,
different check (`price_above_ema20` for BUY / `price_below_ema20` for
SELL). These can disagree. Built a real fixture where `ema_alignment`,
`market_trend`, and `sma_alignment` ALL pass (`tier1_passed=True`,
unanimous, not even just 2/3) while `close` sits below `ema_20` (BUY
case) — before this fix:

```
action: BUY   score: 74.75   narrative: ... TrendState=DOWNTREND / EntryState=TRIGGERED
```

A live BUY, printing its own `TrendState=DOWNTREND` in the same log
line. SELL mirror confirmed identically (`TrendState=UPTREND` on a live
SELL).

## What was fixed

**New shared module, `decision/state_rules.py`** — `evaluate_entry_state()`,
an ordered, first-match-wins rule table, direction-agnostic (BUY/SELL
both call the exact same function with direction-specific parameters,
so a rule-order change or a new rule automatically applies to both,
instead of two hand-maintained copies that can drift):

1. `trend_state == unfavorable_trend_state` → hard reject,
   `REJECTED_TREND` — **the new rule**, closes the bug above.
2. `not tier1_passed` → hard reject, `REJECTED_TREND` — unchanged.
3. `setup_state == stale_setup_state` → hard reject,
   `REJECTED_STALE_ENTRY` — unchanged (`not_stale_entry`, fix #5).
4. `not not_overextended` → hard reject, `REJECTED_OVEREXTENDED` —
   unchanged (fix #12).
5. `overall_score < qualify_threshold` → `WAITING`, not a hard reject.
6. Otherwise → `TRIGGERED`, qualified.

**Deliberately excluded** (per your final confirmation): no new
`MarketState`-based hard reject (e.g. "BEAR market ⇒ hard-reject all
BUYs"). That would be a materially bigger, unprecedented trading-
behavior change the review never actually proposed — my first, broader
reading of "poora rule table" over-scoped this, and you correctly
narrowed it back to the review's actual literal example (combining
TREND/SETUP/ENTRY, not MARKET).

**`strategy/buy_strategy.py` / `strategy/sell_strategy.py`** (mirrored
edits) — `trend_state`/`setup_state` are now computed BEFORE
qualification (moved up, no new logic — same formulas as before) and
fed into `evaluate_entry_state()` as genuine inputs. `qualified` and
`entry_state` both now come from that single call, so they can't
diverge again. The old duplicate boolean chain and the old
after-the-fact `entry_state` if/elif cascade are both gone — replaced
by the one table call.

**Minor, intentional behavior narrowing**: previously, if BOTH
`not_overextended` and `not_stale_entry` failed simultaneously, `reasons`
got TWO "Rejected: ..." lines (checked independently). The table is
first-match-wins, so now only the higher-priority one appears. The
final `qualified=False`/`action=NO_TRADE` verdict is unchanged either
way — this only affects how many redundant reject-reason lines show up
in the log for that specific double-failure edge case.

## Verification — real engine, not just the rule table in isolation

Zero existing tests covered `buy_strategy.py`/`sell_strategy.py` before
this phase (confirmed via search — neither file had ANY test file).
Rather than trust hand-derived reasoning about ~2700 lines of nested
checks, every fixture below was built empirically: run against the
REAL `BuyStrategyEngine`/`SellStrategyEngine`, inspected, and only
locked into a test once the actual output matched what the test claims
— including reproducing the bug on the pre-fix code first to prove the
"before" behavior was real, not assumed.

```
python3 -m pytest tests/ -q                    # 210 passed (187 prior + 23 new)
python3 -m flake8 --config=.flake8 .            # whole repo, clean
```

## Files changed

- **`decision/state_rules.py`** (new) — `evaluate_entry_state()`,
  `EntryStateResult`, the shared rule table.
- **`strategy/buy_strategy.py`**, **`strategy/sell_strategy.py`** —
  reordered to compute states before qualification; wired to the shared
  table; updated `state_narrative`/`BuyDecision`/`SellDecision`
  docstrings to reflect the table now being the source of truth, not a
  presentation-only afterthought.
- **`tests/test_state_rules.py`** (new, 13 tests) — the rule table in
  isolation: every branch, priority ordering (including all-4-hard-
  reject-conditions-true-at-once), the WAITING/TRIGGERED boundary, both
  BUY and SELL parameterizations.
- **`tests/test_buy_strategy_state_rules.py`** (new, 5 tests),
  **`tests/test_sell_strategy_state_rules.py`** (new, 5 tests) — real
  `evaluate()` end-to-end: clean pass, the trend-contradiction bug fix
  (the headline case), stale-entry reject, overextended reject (with a
  simultaneous fresh trigger, to prove rule ordering), below-threshold
  WAITING (not a hard reject).

## Still open (unchanged from PHASE27/28_NOTES.md)

- `execution/broker.py`'s ~400 lines of dead simulation code — blocked
  on your revive-vs-delete decision.
- `orchestrator.py`'s hardcoded `risk_snapshot` — blocked on either a
  real volatility/liquidity_risk data source or your confirmation to
  leave it hardcoded/documented.
- `scripts/morning_executor.py` not propagating `candidates_order.json`'s
  real `overall_score`/`ranking` into the diary at entry (flagged in
  PHASE28_NOTES.md, not scoped/fixed).
