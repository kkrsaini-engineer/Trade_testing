# PHASE 17 — gap_down/gap_up dead-vs-live fix + risk_score recalibration

Implemented on explicit request ("dono karo") for the two open items
flagged at the end of Phase 16.

---

## Part 1: gap_down/gap_up — removed the dead copy, kept the live one

`checks["gap_filter"]` in `strategy/buy_strategy.py`/`sell_strategy.py`
was dead weight — never in any `*_CORE` list, never reached
`tier2_score`, only counted in the informational "Technical confirmation:
X/Y" tally. The exact same `row["gap_down"]`/`row["gap_up"]` value was
separately, live, the single biggest individual penalty in
`buy_scoring.py`/`sell_scoring.py`'s `_risk_score()`.

**Decision:** removed the dead `checks["gap_filter"]` block from both
strategy files, kept `_risk_score()`'s live version unchanged in
principle (only its magnitude changed — see Part 2). Wiring a second live
vote here instead would have recreated the exact
`volatility_state`/`market_regime` duplicate-path problem Phase 16 just
fixed — one live vote per signal, not two.

`row["gap_down"]`/`row["gap_up"]` themselves are untouched — they're
independent columns from `features/indicators/breakout.py`
(`gap_pct >= 1.0`/`<= -1.0`), not derived from the removed check, so
`_risk_score()` still reads them directly with no dependency on anything
removed.

---

## Part 2: risk_score recalibration — floor restored to 0-100

**The problem, exactly:** after Phase 16 removed `volatility_state`/
`market_regime` (duplicates) and this phase removed gap's dead-not-live
distinction, `_risk_score()`'s remaining 3 real inputs — gap, extreme
RSI, below-average volume — kept their OLD magnitudes (30/10/5 for BUY,
25/10/5 for SELL), which only ever summed to 100 when combined with the 3
now-removed inputs. Left alone, worst case only reached 55 (BUY) / 60
(SELL) instead of 0, making `risk_level()`'s `"EXTREME"` tier
unreachable.

**Fix:** proportionally rescaled the 3 survivors so they sum to exactly
100 again, preserving the original author's relative weighting (gap
matters most, then RSI-extreme, then below-average-volume) rather than
inventing new relative importances from nothing:

```
BUY:  gap_down=66.67, rsi_extreme=22.22, low_volume=11.11   (30:10:5 scaled by 100/45)
SELL: gap_up=62.5,    rsi_extreme=25.0,  low_volume=12.5    (25:10:5 scaled by 100/40)
```

This is a proportional rescale, not a re-derivation — there's no
historical/backtest data in this sandbox to calibrate genuinely new
magnitudes from outcomes, same limitation flagged for every other
calibration choice in this review (e.g. Phase 11's ATR-normalization).

---

## Wiring verification

- `checks["gap_filter"]` grepped clean out of both strategy files — zero
  remaining references (would have been a `KeyError` at runtime if any
  survived; confirmed via a live `evaluate()` call completing without
  error).
- `_risk_score()` verified directly: a worst-case row (gap + RSI-extreme
  + low volume, all true) now returns exactly `0.0` for both BUY and
  SELL (previously 55.0/60.0); a clean row still returns `100.0`.
- Full regression suite re-run — all pass, no behavior outside
  `_risk_score()`'s own output range changed unexpectedly.

## Testing performed

- `flake8 --config=.flake8` on all 4 changed files — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed.
- Full regression re-run of all 6 prior phase test scripts — all pass.
- New targeted test (this phase): direct `_risk_score()` calls proving
  the worst-case floor is exactly `0.0` again for both BUY and SELL, and
  a clean row still scores `100.0`; a live `evaluate()` call confirming
  no crash/dangling reference from the removed `gap_filter` check.

## Files changed

- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
- `strategy/buy_scoring.py`
- `strategy/sell_scoring.py`

---

## Where this leaves the "reduce indicators" audit

With Phases 14-17, every "Ichimoku-style" duplicate-vote path found in
the deep-dive audit is now closed: Ichimoku (2 paths), volatility_state
(1 path), market_regime (2 paths), gap_down/gap_up (dead-vs-live
inconsistency). `_risk_score()` now has exactly 3 inputs, each with
exactly one live vote elsewhere in the pipeline where relevant
(gap has no other vote — it's risk-only by design) and no duplicates.
