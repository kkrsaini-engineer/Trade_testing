# PHASE 14 — MFI meaningful-use fix

Implemented on explicit request, after you pointed out (correctly, verified
against the actual code before agreeing) that `checks["mfi"]` was
technically "wired" but used in a way that threw away almost all of MFI's
actual signal.

---

## What was wrong, confirmed before touching anything

```python
checks["mfi"] = 50 <= row["mfi_14"] <= 80          # BUY
checks["mfi"] = 20 <= row["mfi_14"] <= 50           # SELL
mfi_component = 100.0 if checks.get("mfi") else 0.0  # feeds volume_factor_score at 15% weight
```

MFI is a 0-100 continuous oscillator. Collapsing it to a binary in/out-of-band
flag meant MFI=90 (deep overbought — the single most information-rich MFI
reading for reversal risk) scored **identically to MFI=10** (deep oversold)
— both got 0. All of the magnitude/extreme information MFI actually exists
to provide was discarded; only "is it a boring mid-range value" survived.

## Fix applied

`checks["mfi"]` itself is untouched (still drives the reasons text and the
Tier2 "Technical confirmation: X/Y" informational count). Only
`mfi_component` — the thing that actually reaches `volume_factor_score` —
changed, from a binary cliff to a continuous, direction-aware trapezoid.

**BUY** (`strategy/buy_strategy.py`):

```
mfi <= 20   -> 20            (money flowing OUT — contradicts a BUY thesis
                               outright, floored not zeroed)
20-50       -> ramps 20->100 (strengthening bullish flow)
50-80       -> 100            (healthy bullish flow — same sweet spot the
                               old binary check used)
80-100      -> ramps 100->40 (overbought/reversal risk — tapered, not
                               zeroed, since strong momentum can persist)
```

**SELL** (`strategy/sell_strategy.py`) — exact mirror around 50, since SELL
wants LOW MFI (bearish outflow) instead of high:

```
mfi >= 80   -> 20             (heavy buying inflow — contradicts a SELL
                                thesis outright)
50-80       -> ramps 20->100 as mfi falls (strengthening bearish flow)
20-50       -> 100             (healthy bearish flow — same sweet spot the
                                old binary check used)
0-20        -> ramps 100->40 as mfi falls further (deep-oversold
                                bounce-risk — tapered, not zeroed)
```

Both floors (20 low-extreme, 40 high-extreme) are non-zero on purpose — an
extreme MFI reading contradicts the setup's thesis but doesn't necessarily
mean nothing else about the setup is valid, so it's penalized, not treated
as equivalent to "worst possible volume."

**NaN handling:** if `mfi_14` is NaN (insufficient history), returns a
neutral 50.0 rather than falling through to a wrong branch or crashing —
verified directly (see Testing below).

**REAL BEHAVIOR CHANGE (flagged explicitly):** `volume_factor_score` will
shift for every symbol/day where MFI is outside the old 50-80 (BUY) /
20-50 (SELL) band — previously contributing a flat 0 to that 15% slice,
now contributing a graded 20-100 value. This is the intended effect (MFI's
actual signal now reaches the score), not a side effect.

---

## Wiring verification

`_mfi_component_score()` is computed directly inside
`BuyStrategyEngine.evaluate()`/`SellStrategyEngine.evaluate()` — the same
methods `execution/scanner.py:447/454` call on every real scan — and feeds
`volume_factor_score` exactly where the old binary version did (no change
to how it's consumed downstream). Confirmed via live `evaluate()` calls
sweeping MFI from 0 to 100 in both BUY and SELL: `volume_factor_score`
changes continuously and in the correct direction at every point, mirrors
correctly between BUY/SELL (verified the exact numbers match the trapezoid
math by hand), and NaN input produces a finite, neutral contribution
instead of a crash or silently-wrong branch.

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_strategy.py`,
  `strategy/sell_strategy.py` — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Full regression re-run of all 6 prior phase test scripts — all pass, no
  regressions (existing fixtures' default MFI values land inside the old
  50-80/20-50 sweet spot, so they hit the flat `100.0` region of the new
  trapezoid too — same numbers as before).
- New targeted smoke test (this phase): swept raw MFI 0/5/20/35/50/65/80/
  90/100 through live `evaluate()` calls for both BUY and SELL, confirmed
  `volume_factor_score` at every point matches the trapezoid formula by
  hand-calculation, confirmed BUY/SELL are exact mirrors of each other,
  confirmed NaN MFI produces a finite neutral score rather than a crash.

## Files changed

- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
