# Phase 19 — Exit Engine Replacement (Trailing Stop / Break-even / Dual Targets)

## What this phase does

Replaces `risk/exit_engine.py`'s `ExitEngine` (the exit-decision engine
that has actually been live in paper trading) with `risk/exit_strategy.py`'s
`ExitStrategyEngine` — the more sophisticated engine that already existed
in the repo but was **dead code**: `orchestrator.py` instantiated
`execution/tracker.py`'s `PositionTracker` (which owns an
`ExitStrategyEngine` internally) but never called `.update()` on it.

This was a direct follow-up to the risk-management walkthrough: you asked
what `ExitStrategyEngine`/`PositionTracker` would do if actually wired up,
I found 4 crash-causing bugs in the code path that would fire the moment
either was invoked, and you asked to fix both **and** wire it in, choosing
"Replace ExitEngine" (not run two exit engines side by side) when I
flagged that running both together would be two live votes on the same
signal.

## Why replace instead of merge

`ExitEngine` (old, live) is a **weighted-score** engine: 5 components
(technical/fundamental/news/risk/institutional) blend into one 0-100
"case to exit" score against a fixed threshold — binary HOLD/EXIT only,
stop-loss and targets are recomputed fresh from the current close every
cycle (no memory of the position's own price path).

`ExitStrategyEngine` (new, now live) is a **priority-rule** engine with
real path-dependence: trailing stop (never gives back more than 3×ATR
from the position's own peak), break-even stop (once profit clears
1.5×ATR, the stop can never go below entry again), and two profit
targets — a 50% partial exit at 2×ATR and a full exit at 3.5×ATR — plus
the same trend/volatility/time-based checks as before. Running both
would mean two independent brains disagreeing on the same position with
no arbitration rule, so per your choice `ExitEngine` is fully removed
from the live path; `risk/exit_engine.py` itself is left in the repo
(still correct, still independently testable) but nothing imports it
anymore.

## The 4 bugs fixed (all pre-existing, all in previously-dead code paths)

These were never caught before because nothing ever called the code that
contained them.

1. **`execution/tracker.py` — `FinalDecision(signal=...)`.** `signal`
   isn't a real field on `FinalDecision` (the field is `action`), and
   every other mandatory field (`ranking`, `buy_score`, `sell_score`,
   `buy_probability`, `sell_probability`, `expected_return`,
   `expected_drawdown`, `expected_hold_days`) was missing too — this
   raised `TypeError` on the very first call.
2. **`execution/tracker.py` — `self.exit_engine.evaluate(position=pos, ...)`.**
   `pos` is a `PositionState` **dataclass instance**, not a dict — but
   `ExitStrategyEngine.evaluate()` calls `.get(...)` on `position`
   throughout. This would raise `AttributeError` the instant it ran, and
   the call was also missing the required `decision` argument entirely.
3. **`execution/tracker.py` — `ValidationResult(passed=True)`.** Missing
   3 mandatory fields (`action`, `confidence`, `rejection_reason`, none
   of which have defaults) — `TypeError` on the very first call, one
   line before bug #2 would even have been reached.
4. **`execution/tracker.py` — downstream action handling used
   `{"EXIT", "FORCE_EXIT"}` / `"TRAIL"`.** That's `risk/exit_engine.py`'s
   vocabulary. `ExitStrategyEngine` (the class this file actually
   instantiates) has never returned those values — only
   `HOLD` / `PARTIAL_EXIT` / `FULL_EXIT`. Also read `exit_result.new_stop`,
   `exit_result.warnings`, `exit_result.reason` — none of which exist on
   `ExitDecision` (the real fields are `trailing_stop`,
   `diagnostics["warnings"]`, `diagnostics["exit_reason"]`).

All 4 are fixed in `execution/tracker.py`. **`PositionTracker` itself
stays unwired** — `orchestrator.py` still just instantiates it and never
calls `.update()`, same as before. It's now *correct* dead code instead
of *broken* dead code, available if a future live-trading mode wants a
simpler batch position-tracking utility. The actual live wiring goes
directly from `paper_trading_engine.py` into `ExitStrategyEngine`,
reusing `VirtualPortfolio`'s already-existing position state (peak
price tracking, PnL, quantity) instead of standing up a second, redundant
position-tracking system — see "Architecture decision" below.

## The 2 features ported from the old engine ("Port both", as you chose)

`ExitStrategyEngine` as it existed in the repo was missing two safety
behaviors the old live `ExitEngine` had. Both are now present:

**1. Risk-unsafe hard exit.** If `RiskManager` flags a position unsafe
(circuit breaker, emergency stop, daily loss lock, VIX spike, event day,
news shock), the old engine force-exited immediately regardless of the
weighted score. `ExitStrategyEngine` already had an `emergency_exit`
mechanism (a boolean flag on the `position` dict) that nothing ever set —
it's now wired: `paper_trading_engine.py` sets
`position["emergency_exit"] = not risk_result.safe` every cycle, with a
descriptive `emergency_exit_reason` (e.g. *"Risk engine flagged this
symbol as unsafe (grade: D, total_risk: 78/100)."*) that now flows
through to `ExitDecision.reasons` instead of a generic message.

**2. Intraday touch detection.** The old engine used the day's high/low
to catch a stop-loss or target that was touched intraday even if the
close price later recovered past it, and reported a `suggested_exit_price`
so a real fill price (not a later, unrelated close) drives realized P&L.
`ExitStrategyEngine` only compared against the close. Added
`day_high`/`day_low` reads and touch-price logic to all three checks
(stop-loss, partial target, final target) plus a new
`ExitDecision.suggested_exit_price` field, same convention as the old
engine's `ExitEvaluation.suggested_exit_price`.

## New capability this unlocks: real partial profit-booking

The old `ExitEngine` was binary HOLD/EXIT — partial exits were never
possible in production even though the notification text mentioned
"Target 1 (Partial)". `ExitStrategyEngine`'s `PARTIAL_EXIT` action is now
actually executed: `paper_trading_engine.py` calls
`PortfolioEngine.partial_exit()` to book ~50% of the position (rounded to
whole shares) at the target/touch price, leaving the rest open to trail
further. If the reduction happens to consume the entire remaining
quantity (e.g. a 1-share position), `PortfolioEngine.partial_exit()`
itself falls through to a full close — handled explicitly so diary/trade-
store bookkeeping and the position-count reconciliation in
`run_paper_trading.py` stay accurate either way. Partial exits are logged
to the trade store (`action: "PARTIAL_CLOSE"`), surfaced in the
consolidated Telegram summary under a new "Partial Exits" section, and
returned in `run_cycle()`'s summary dict as `partial_exits_today`.

## Architecture decision: bypass `PositionTracker`, wire directly

`ExitStrategyEngine.evaluate()` needs a `FinalDecision`, a `RiskResult`,
and a `dataframe` — all of which `execution/scanner.py`'s
`evaluate_position()` already computes internally but didn't expose.
Added `diagnostics["_final_decision"]` / `diagnostics["_risk_result"]` as
internal passthrough keys, same leading-underscore convention as the
existing `_dataframe` / `_fundamentals` / `_news_score`.

`paper_trading_engine.py` then builds a held-direction-corrected decision
via `dataclasses.replace(final_decision, action=pos.direction)` — mirrors
`execution/scanner.py`'s own `held_direction` precedent for
stop_loss/target1/target2 — and calls `ExitStrategyEngine.evaluate()`
directly, feeding it `highest_price`/`lowest_price` straight from
`VirtualPortfolio`'s `PortfolioPosition` (which already tracks the
position's genuine peak/trough — confirmed no new persisted field was
needed: the trailing stop is naturally path-dependent through
`highest_price`/`lowest_price`, which are monotonic for the life of the
position, so nothing needs to be separately stored/reloaded across daily
cycles for the trail itself to hold).

`PositionTracker` was NOT used for this wiring — it would duplicate
`VirtualPortfolio`'s position state (a second dict of "open positions",
a second place price/PnL gets updated) for no benefit in this pipeline.

## Field mapping (old `ExitEvaluation` → new `ExitDecision`)

| Concept | Old (`ExitEngine`) | New (`ExitStrategyEngine`) |
|---|---|---|
| Action values | `HOLD` / `EXIT` | `HOLD` / `PARTIAL_EXIT` / `FULL_EXIT` |
| "How strong is the case to exit" | `exit_score` (0-100) vs `threshold` (60) | *(no longer a concept — see below)* |
| Confidence in the decided action | *(none)* | `confidence` (0-100, blended) |
| Why it exited | `hard_risk_reason` / `reasons` | `diagnostics["exit_reason"]` / `reasons` |
| Fill price for a touched stop/target | `suggested_exit_price` | `suggested_exit_price` (same name, ported) |
| Stop level | *(recomputed fresh from ATR each cycle)* | `stop_loss` (dynamic), `trailing_stop` |
| Targets | *(display-only, never triggered an exit)* | `take_profit`, plus `exit_percent`/`PARTIAL_EXIT` for the nearer target |

`diary.add_daily_log()`/`close_trade()`'s `exit_score` field (the on-disk
schema name, kept for stability) is now populated from
`ExitDecision.confidence` — conviction in whatever action was decided —
rather than the old "urgency to exit" score, since that concept doesn't
exist in the new engine. Displayed as "Exit Confidence" in notifications
now, not "Exit Score / threshold". `_classify_exit_trigger()` (used for
the short trigger label — "Stop Loss Hit", "Target Achieved", etc.) was
rewritten to read `ExitDecision.diagnostics`'s own per-condition boolean
flags (`stop_hit`, `final_exit`, `partial_exit`, `trend_reversal`, ...)
in the exact priority order the engine itself evaluates them, instead of
parsing free-text reason strings — more robust than the old approach.

## Known, deliberate gaps (not silently dropped — flagging for a decision)

`ExitEngine` had two components `ExitStrategyEngine` doesn't have an
equivalent for, and these were **not** part of the "Port both" scope we
agreed on, so they were left out rather than guessed at:

- **Continuous news-driven exit.** Old engine blended a continuous
  news_score into the weighted exit score. New engine only has a boolean
  `negative_news` flag on the position dict, which nothing currently
  sets (defaults `False` — never fabricated). If you want news to
  meaningfully influence exits again, this needs a threshold decision
  (at what news score does `negative_news` flip to `True`?) — happy to
  wire it if you want it.
- **FII/DII institutional-flow exit.** Same situation — old engine had a
  10% weight on this, new engine has no equivalent input at all.

Also worth knowing: `execution/scanner.py`'s static ATR stop/target
(`stop_loss`/`target1`/`target2` in `result.diagnostics`, used for the
`dist_target1`/`dist_stop` distances shown in the regular Holding Status
notification) is a **separate calculation** from `ExitStrategyEngine`'s
own trailing stop/dual targets — this duplication already existed before
this phase (three separate ATR-stop implementations across
`scanner.py`/`exit_strategy.py`/`position_sizing.py`, noted in an earlier
phase). Not touched here to keep this phase's blast radius scoped to the
exit-engine swap; the new engine's own live values ARE now shown
separately in each Holding Status block (`Exit Engine: ...` line) so the
trailing-stop behavior is actually visible, but the two systems don't
reconcile with each other. A future phase could unify them into one
shared stop/target calculation if that duplication starts causing
confusing notifications.

## Files changed

- **`risk/exit_strategy.py`** — `sizing` parameter made `Optional`
  (confirmed dead inside the method body); added `day_high`/`day_low`
  reads and intraday touch-price logic to stop-loss/partial-target/
  final-target checks; added `suggested_exit_price` field to
  `ExitDecision`; added `emergency_exit_reason` passthrough; surfaced the
  previously-computed-but-dropped `warnings` list into `diagnostics`.
- **`execution/scanner.py`** — `evaluate_position()` now exposes
  `diagnostics["_final_decision"]` / `diagnostics["_risk_result"]`
  (internal passthrough, same convention as `_dataframe`).
- **`execution/tracker.py`** — all 4 bugs above fixed; `summary()`'s
  action-value checks updated to the real `ExitDecision` vocabulary.
- **`paper_trading/paper_trading_engine.py`** — swapped `ExitEngine` for
  `ExitStrategyEngine`; full rewrite of the monitoring loop's exit
  handling (FULL_EXIT / PARTIAL_EXIT / HOLD branches, `partial_exit()`
  wiring, diary/trade-store field mapping); `_classify_exit_trigger()`
  rewritten; removed a block of pre-existing dead code
  (`meaningfully_changed`/`position_status` — computed but never
  consumed by anything, confirmed via full-file trace, predates this
  phase); fixed two other already-dead methods
  (`_format_trade_closed`/`_format_position_update`, never called
  anywhere in the repo) so they don't reference removed fields, in case
  they're ever revived.
- **`scripts/run_paper_trading.py`** — daily CSV report, JSON summary,
  and console output now include partial-exit counts/detail.
- **`tests/test_exit_strategy.py`** (new, 15 tests) — trailing stop
  (rises for BUY / falls for SELL, holds on pullback), break-even
  activation (both directions), dual targets (both directions),
  stop-loss hit via close, intraday touch detection (stop AND target,
  both directions, plus a negative case proving it doesn't
  false-positive), emergency exit (custom reason + fallback generic
  reason), `sizing` optionality, plain HOLD.
- **`tests/test_tracker.py`** (new, 4 tests) — proves all 4 bugs are
  fixed: `update()` runs without crashing, trailing stop persists across
  cycles, a genuine stop breach closes the position, `summary()`/
  `get_snapshot()`/`debug_report()` all work post-fix.
- **`tests/test_paper_trading_exit_wiring.py`** (new, 5 tests) — full
  `PaperTradingEngine.run_cycle()` integration tests against a fake
  scanner but real `VirtualPortfolio`/`TradeDiary`/`TradeStore`/
  `ExitStrategyEngine`: FULL_EXIT closes the position (BUY and SELL),
  PARTIAL_EXIT reduces quantity but leaves it open, HOLD leaves it
  untouched, risk-unsafe forces an emergency FULL_EXIT.

## Verification

```
python3 -m flake8 --config=.flake8 .          # whole repo, clean
python3 -m pytest tests/ -q                    # 28 passed (4 pre-existing + 24 new)
```

## What to watch for once this runs live

- The first few cycles after deploying this will show `PARTIAL_EXIT`
  actually executing for the first time ever in this pipeline — expect
  to see "Partial Exits" sections in the Telegram summary that never
  existed before. This is the new capability, not a bug.
- Watch the Holding Status notification's new `Exit Engine: ...` line
  (trailing stop / confidence) against the existing static
  stop/target line — they will legitimately show different numbers per
  the "Known gaps" section above; that's the two independent
  calculations, not an inconsistency to chase down.
- If the risk-unsafe emergency exit fires more/less often than expected,
  that's `risk_result.safe` — unchanged logic from before, just now
  actually connected to an exit action again.
