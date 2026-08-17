# Phase 20 — Kelly, Risk:Reward Display, Stop/Target Consolidation

Follow-up to Phase 19's exit-engine wiring. You flagged 3 issues; this
phase addresses 2 of them with code changes and leaves 1 untouched per
your own choice.

## 1. Kelly Criterion — left as-is (your choice)

Checked the data before proposing anything: this environment has only
~22 closed trades total in `storage/trades/trades_master.csv`. Real
Kelly calibration needs a (setup_score → historical win-rate) table
bucketed by score range — with only 22 trades total, any bucket would
have far too few samples to mean anything. The codebase's own existing
convention (`analytics/learning_engine.py`) already refuses to draw
conclusions below `n=10` per bucket elsewhere — calibrating Kelly now
would violate that same standard the codebase already holds itself to.

You chose to leave it exactly as-is. No code changed for this item —
`risk/position_sizing.py`'s `KELLY_CALIBRATED = False` /
`FALLBACK_KELLY_FRACTION = 0.5` stay exactly as they were, already
documented in the existing NOTE comment there. Revisit once there's
meaningfully more closed-trade history.

## 2. Risk:Reward display — relabeled honestly (your choice)

**The actual finding was bigger than what you flagged.** You pointed
out target1's R:R is always ~1.0 — correct, and I confirmed it's not
"~1.0", it's **mathematically exactly 1.0, every time, for every
symbol, forever** — not a display rounding artifact. But target2 has
the *identical* structural problem: its R:R is exactly 1.75, always.
Both are pure constants by construction, because the formula is
`target = close ± k×ATR`, `stop = close ∓ m×ATR` — any fixed k/m always
produces the constant ratio k/m, regardless of what values k and m are
set to. **No choice of multipliers under this formula shape can make
the ratio vary symbol-to-symbol** — that would require targets based on
something other than a fixed multiple of the same day's ATR (support/
resistance, volatility percentile, etc.) — the "bigger redesign" option
you declined for now.

Given that, your choice — relabel honestly — was the only option that
doesn't either (a) keep presenting a fake per-trade metric or (b) change
live trade economics for no real benefit. Implemented:

- `risk/stop_target.py` (new, see item 3) exposes
  `TARGET1_R_MULTIPLE = 1.0` and `TARGET2_R_MULTIPLE = 1.75` as named
  constants — the model's fixed R-multiple design, stated as what it is.
- `execution/scanner.py` no longer computes/stores `diagnostics["risk_reward"]`
  (a single number that only ever reflected target1's constant anyway).
  Now stores `diagnostics["target1_r_multiple"]` /
  `diagnostics["target2_r_multiple"]` — the honest constants.
- `scripts/generate_full_report.py`'s CSV: `RiskReward` column replaced
  with `Target1_RMultiple` / `Target2_RMultiple` (two columns, since one
  number can't represent both targets' multiples). Checked — nothing
  else in the repo reads the old `RiskReward` column.
- `paper_trading/paper_trading_engine.py`'s `_format_buy_report()` (this
  method is dead code — never called anywhere, a leftover from before
  new-entry logic moved to `scripts/morning_executor.py` — but fixed
  anyway in case it's ever revived, same as Phase 19's treatment of the
  other dead formatter methods): `(Risk:Reward 1:{rr:.2f})` →
  `({r_multiple:.2f}R)`, sourced from the same honest constants instead
  of a recomputed ratio.

## 3. Stop/target formula duplication — consolidated (your choice)

New `risk/stop_target.py` — the single canonical implementation, now
imported by all three previous copies:

- `execution/scanner.py`'s `_compute_stop_loss_targets()` — now a thin
  wrapper delegating to the shared function.
- `risk/exit_strategy.py`'s `ExitStrategyEngine.evaluate()` — its
  INITIAL stop-loss + profit-target computation (NOT the trailing
  stop/break-even/dynamic-stop sections, which are genuinely unique to
  this live engine and stay local) now calls the shared function too.
  `ATR_STOP`/`PARTIAL_TARGET`/`FINAL_TARGET` class attributes are kept
  (aliased from the shared module) so nothing that reads them breaks.
- `risk/position_sizing.py`'s `stop_distance` calculation — now calls
  `stop_target.stop_distance()`.

**Confirmed a real discrepancy while investigating** (not just a
theoretical drift risk): `position_sizing.py`'s formula already had the
`close × 1%` floor; `scanner.py` and `exit_strategy.py`'s live stop did
not. For a very low-ATR stock, sizing was assuming a WIDER (more
conservative) stop than what would actually trigger a real exit — net
effect was safe (smaller position than the true risk technically
allowed), not dangerous, but genuinely inconsistent. Per your choice,
the floor is now applied **everywhere**, including the live exit stop —
target1/target2 scale off the same (possibly floor-adjusted) distance as
the stop, so the R-multiple invariant (item 2 above) holds exactly even
when the floor — not raw ATR — is what's driving the numbers.

Left alone, deliberately: `exit_strategy.py`'s trailing stop
(`ATR_TRAILING=3.0`), break-even trigger, and the "DYNAMIC STOP
ADJUSTMENT" HOLD-case tightening (`current_price ± atr*2.0`) — these are
genuinely separate live-exit-engine concepts with no equivalent in
`scanner.py`'s static display or `position_sizing.py`'s entry sizing,
so consolidating them wouldn't remove any real duplication.

## Files changed

- **`risk/stop_target.py`** (new) — canonical `compute_stop_loss_targets()`,
  `stop_distance()`, `ATR_STOP`/`PARTIAL_TARGET`/`FINAL_TARGET`/
  `MIN_STOP_PERCENT`/`TARGET1_R_MULTIPLE`/`TARGET2_R_MULTIPLE`.
- **`execution/scanner.py`** — `_compute_stop_loss_targets()` now
  delegates to the shared module (3-tuple return, was 4 with the fake
  `risk_reward`); both call sites updated to store the honest
  R-multiple diagnostics instead.
- **`risk/exit_strategy.py`** — initial stop-loss + profit-target
  computation now uses the shared formula; class attributes aliased
  from the shared module.
- **`risk/position_sizing.py`** — `stop_distance` now calls the shared
  `stop_target.stop_distance()`.
- **`paper_trading/paper_trading_engine.py`** — dead
  `_format_buy_report()`'s target block relabeled honestly (fixed while
  here, matches Phase 19's treatment of the other dead formatters).
- **`scripts/generate_full_report.py`** — `RiskReward` CSV column
  replaced with `Target1_RMultiple`/`Target2_RMultiple`.
- **`tests/test_stop_target.py`** (new, 7 tests) — BUY/SELL normal-ATR
  levels, floor activation (both directions), the R-multiple invariant
  holding across many inputs (with a small tolerance for 2-decimal
  paisa-rounding noise at very small absolute distances — the
  pre-rounding math is exact, only display rounding introduces this),
  zero-input safety, the documented constant values.

## Verification

```
python3 -m flake8 --config=.flake8 .          # whole repo, clean
python3 -m pytest tests/ -q                    # 35 passed (28 from Phase 19 + 7 new)
```
