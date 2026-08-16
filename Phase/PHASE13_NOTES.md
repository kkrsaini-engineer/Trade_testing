# PHASE 13 — Fix #10/#16 (volume-pressure model)

Implemented on explicit request ("last but not the list, most important
one 10th, fix it and check wired"), fact-checked against actual code
before implementing.

---

## Your stated concern, addressed honestly first

You said your concern is that none of the volume indicators tell you
*exactly* how much of a day's volume was real buying vs real selling, and
that most volume shown is "fake." **That specific thing — a true
buy-volume-vs-sell-volume split — cannot be built from what this pipeline
has access to, and this fix does not claim to solve it.** `volume`,
`OBV`, `CMF`, `MFI` — every volume indicator in this codebase, before and
after this fix — is computed from daily OHLCV bars (open/high/low/close/
volume for the whole day). None of them, and no combination of them, can
tell you how many of today's shares traded were buyer-initiated vs
seller-initiated; that split only exists in tick-level order-flow / Level
2 market-depth data, which is a fundamentally different (and far more
expensive/complex) data feed that doesn't exist anywhere in this codebase
and cannot be fetched from this sandbox. Anyone telling you a same-
timeframe-OHLCV composite gives you that split — however it's packaged —
would be overselling it, and I'm not going to do that here.

**What this fix genuinely does:** NSE separately publishes a real,
independent, settlement-level number — delivery percentage — that tells
you how much of a day's *traded* volume was actually *delivered* (taken
into a demat account, i.e. a real investor decision) versus squared off
intraday (pure day-trading churn, the "fake volume" you're describing).
That's not a buy-vs-sell split, but it IS a genuine, different signal from
`OBV`/`CMF`/`MFI` (which are all price-direction-weighted volume from the
same OHLCV bars, not settlement data) — a low-delivery day is a real
signal that a lot of that day's volume was noise, regardless of which
direction the price moved.

---

## What existed before this fix

`volume`, `volume_spike`, `obv`, `cmf`, `mfi` all existed as separate
checks. Phase 4 grouped `volume_spike` + `accumulation` (an `obv`+`cmf`
composite, fixed for double-counting in fix #7) into a "Volume" factor
score — but `mfi` was computed and never used in that factor at all, and
real delivery-percentage data — already fetched by `data/delivery_data.py`'s
`DeliveryDataProvider` and already wired into
`execution/scanner.py`'s `dataframe["delivery_percentage"]` column from
earlier work in this conversation — was consumed ONLY by
`decision/validation_engine.py`'s binary `>= 20%` liquidity gate, never by
any scoring in `buy_strategy.py`/`sell_strategy.py`.

## Fix applied

`volume_factor_score` (part of `tier2_score`, reaching the real
qualify/rank decision) is now a genuine composite:

```python
DELIVERY_WEIGHT_IN_VOLUME = 0.35
MFI_WEIGHT_IN_VOLUME = 0.15

volume_core_score = _factor_score(VOLUME_CORE)   # existing: volume_spike + accumulation
mfi_component = 100.0 if checks.get("mfi") else 0.0
delivery_pct = row.get("delivery_percentage")

if delivery_pct is not None:
    delivery_component = clamp(float(delivery_pct), 0, 100)
    volume_factor_score = (
        volume_core_score * 0.50   # 1 - 0.35 - 0.15
        + mfi_component * 0.15
        + delivery_component * 0.35
    )
else:
    # No fabricated neutral value — redistribute delivery's weight back
    # to the existing components (same has_X convention as fixes #8/#11).
    volume_factor_score = volume_core_score * 0.85 + mfi_component * 0.15
```

Mirrored identically in `sell_strategy.py`. **Delivery% is NOT inverted
for SELL** (unlike `market_score`, fix #2) — it measures how much of a
day's volume was durable/delivered vs speculative churn, which has no
inherent bullish/bearish direction; high delivery means real conviction
behind the move, whichever direction that move is.

**Honest handling of missing data:** delivery data isn't available for
every symbol/day (real-world NSE bhavcopy coverage gaps, matching the same
kind of missing-data reality fix #3 dealt with for fundamentals). When
unavailable, delivery's weight redistributes back to the existing
direction-vote + MFI components — not a fabricated neutral number, same
convention established in fixes #8/#11. A new `volume_pressure_uses_delivery:
bool` field on `BuyDecision`/`SellDecision` exposes which path was taken,
for auditability.

**REAL BEHAVIOR CHANGE (flagged explicitly):** `volume_factor_score`
numbers will shift for every symbol where delivery data is available —
confirmed via a direct test (identical setup, only `delivery_percentage`
differs: 10% delivery → `volume_factor_score=68.5`, 90% delivery →
`96.5`, same underlying direction/MFI signals). This is the intended
effect (a low-delivery "fake volume" day should genuinely score worse than
a high-delivery one), not a side effect.

---

## Wiring verification

`volume_factor_score`'s new composite computation lives directly inside
`BuyStrategyEngine.evaluate()`/`SellStrategyEngine.evaluate()` — the same
methods `execution/scanner.py:447/454` call on every real scan — and
feeds `tier2_score` exactly the way the old flat-average version did (no
change to how `volume_factor_score` is consumed downstream, only to how
it's computed), meaning it automatically also reaches `buy_scoring.py`/
`sell_scoring.py`'s `result.technical` (via the fix #5-era unification:
`result.technical = buy_decision.tier2_score`) and therefore `buy_strength`
/`sell_strength`/final ranking — the real decision path, not a side
diagnostic. `execution/scanner.py:488/500` additionally expose
`buy_volume_pressure_uses_delivery`/`sell_volume_pressure_uses_delivery`
in diagnostics. Confirmed via a live `evaluate()` call: two otherwise-
identical rows differing only in `delivery_percentage` produce different
`volume_factor_score` values in the expected direction, and the
`volume_pressure_uses_delivery` flag correctly reflects whether delivery
data was present — not a dead field.

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_strategy.py`,
  `strategy/sell_strategy.py`, `execution/scanner.py` — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check — clean.
- Full regression re-run of all 6 prior phase test scripts — all pass, no
  regressions (existing fixtures don't set `delivery_percentage`, so they
  correctly exercise the "unavailable" fallback path and produce
  unchanged numbers).
- New targeted smoke tests (this phase): low-vs-high delivery% comparison
  (BUY and SELL) proving `volume_factor_score` responds correctly and
  proportionally; `volume_pressure_uses_delivery` flag correctness with
  and without delivery data present.

## Files changed

- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
- `execution/scanner.py`
