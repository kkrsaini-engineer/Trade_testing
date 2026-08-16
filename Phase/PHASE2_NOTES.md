# Phase 2 — Late-Entry Fix (Early-Entry Engine)

## Files changed
- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`

## What changed (mirrored BUY/SELL, same logic both directions)

Old qualification leaned on EMA/SMA being **already** fully stacked and RSI **already** sitting in the 55-70 (BUY) / 30-45 (SELL) zone — both true only once a move is well underway. That's why entries were landing late, near highs (BUY) or after most of the drop (SELL).

Added 5 new checks to each file, all feeding into the existing weighted score (not new hard gates, except the last one):

1. **`ema_fresh_cross`** — EMA20 crossing EMA50 within the last 3 bars (BUY: crossing above; SELL: crossing below). Catches the trend at its start, not after weeks of being stacked.
2. **`rsi_fresh_cross`** — RSI crossing through the 50 midline within the last 2 bars (BUY: up through 50, capped ≤65; SELL: down through 50, floored ≥35). Catches fresh momentum shift instead of "already deep in the zone."
3. **`squeeze_breakout`** — prior 5 bars all had `bb_width < 4%` (tight consolidation) followed by today's breakout/breakdown + volume spike. Catches a move breaking out of compression, not one that's already run.
4. **`pullback_entry`** — BUY: existing pullback flag + price still above EMA200 (buying the dip in an uptrend). SELL: new "relief-rally rejection" logic — price poked up to EMA20 yesterday and got rejected today, still below EMA200 (shorting the failed bounce in a downtrend).
5. **`not_overextended`** — **hard reject**, not a weighted vote. BUY: price >8% above EMA20 → rejected regardless of score (chasing an extended move). SELL: price >8% below EMA20 → rejected regardless of score (short-squeeze risk on an extended drop). This is the one new gate — everything else above just adds to the existing weighted score like the other technical checks.

## Testing done
- 5 synthetic BUY scenarios (fresh EMA cross, overextended hard-reject, RSI fresh cross, squeeze breakout, plain default healthy case) — all pass, run together in one sequential script with fully independent per-scenario data.
- 6 synthetic SELL scenarios (same 5 mirrored + relief-rally rejection) — all pass.
- Full existing `pytest tests/` suite — 4/4 still pass, no regressions.
- `flake8` against the repo's `.flake8` config — exit code 0 on both files.

## Note on an earlier false alarm
While building the test script I hit what looked like an order-dependent failure (a plain default case failing only when run after other scenarios). Root cause was a bug in my *test script* — comparing pandas boolean values with `is True`/`is False` (identity check) instead of `==`; pandas returns `numpy.bool_`, which is never `is` Python's `True`. Once fixed to `==`, everything passes consistently. The actual strategy code in `buy_strategy.py`/`sell_strategy.py` was correct the whole time — nothing there was order-dependent or stateful.

## Not included in this phase
Per the agreed 3-phase plan: PCR/Open Interest module, FII/DII data — deferred to Phase 3.
