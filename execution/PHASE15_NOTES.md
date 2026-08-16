# PHASE 15 — Remove Ichimoku, fix OBV's window

Implemented on explicit request: "ichimoku ko htao, OBV mereko pehle hi
pasand nahi tha but 250 days ki data hata do current trend data count kre
aise baaki kar rahe hai" — i.e. remove Ichimoku entirely, and fix OBV to
count a recent/current window instead of an all-time cumulative sum,
matching the ~14-20-day windows the rest of the pipeline uses.

---

## Ichimoku — removed from every place it influenced a decision

**Found TWO wiring paths, not one — the second was previously
undiscovered and is new information, not something flagged before this
phase.**

1. `checks["ichimoku"]` in `strategy/buy_strategy.py`/`sell_strategy.py`'s
   `TREND_CORE` (already known — this is what the ~52-78-day cloud lag
   discussion was about).
2. **A second, separate `cloud_trend` penalty inside
   `buy_scoring.py`/`sell_scoring.py`'s `_risk_score()`** — found while
   tracing every usage of `cloud_trend` before removing it, not just the
   one place originally reported. `result.risk` (which this penalty is
   part of) is itself weighted into `result.overall` AND separately
   consumed by `buy_probability.py`/`sell_probability.py` with its own
   threshold branches (`score.risk >= 85/70/50`) — meaning Ichimoku's lag
   was reaching the decision through two independent routes, not one.

**Both removed.** `checks["ichimoku"]` computation deleted from both
strategy files, `"ichimoku"` removed from `TREND_CORE` (now 5 checks:
`price_above_ema20`/`price_below_ema20`, `supertrend`, `adx`,
`ema_fresh_cross`, `pullback_entry`), and the `cloud_trend` penalty line
deleted from both `_risk_score()` methods.

**What was deliberately NOT touched:** `features/indicators/ichimoku.py`
itself — still computes `cloud_trend`/`tenkan_sen`/`kijun_sen`/etc. This
column still shows up in `execution/scanner.py`'s diagnostics and
`generate_full_report.py`'s "IchimokuIndicators" report column, for
reference — it just no longer feeds `tier2_score`, `result.overall`, or
`result.risk` anywhere. Removing the underlying indicator module entirely
would touch `technical_features.py`, `run_backtest.py`, the report
generator, and `validation_engine.py`'s NaN-exclusion list for
`chikou_span` — a materially bigger, unrequested change for zero
additional decision-impact benefit over leaving it as inert/informational.

**REAL BEHAVIOR CHANGE:** `trend_factor_score` is now a 5-check average
instead of 6 — expect `Trend=` numbers in the reasons log to shift for
every symbol (confirmed directly: a fixture that showed `Trend=66.7` (4/6)
now shows `Trend=60.0` (3/5) with the same underlying checks — the removed
Ichimoku vote is gone, not silently replaced). `result.risk` will also
shift for any symbol/day where `cloud_trend` was BEAR (BUY)/BULL (SELL).

---

## OBV — fixed the all-time-cumsum window

**What was wrong, confirmed before touching anything:**
`df["obv"] = signed_volume.cumsum()` in `features/indicators/volume.py`
summed signed volume from whichever day the fetched dataframe happens to
start — in production, `data/market_data.py` fetches `period="1y"` (~250
trading days), so `checks["obv"] = row["obv"] > 0` was really testing "has
cumulative signed volume been net positive over the last ~year," dominated
by whichever single day had the largest volume anywhere in that year —
not a "current trend" signal, despite feeding a check that (via
`accumulation`/`distribution`) is meant to represent present-day
buying/selling pressure. Demonstrated directly: a synthetic series with
one huge up-volume day early on, followed by 20 days of steady real
decline, kept OBV's *old* cumsum permanently positive (misread as
"accumulation") for the entire decline; the *new* rolling version reads
correctly negative by the end of that same decline (see Testing).

**Fix applied:**

```python
OBV_WINDOW = 20
df["obv"] = signed_volume.rolling(OBV_WINDOW, min_periods=OBV_WINDOW).sum()
```

20 days chosen to match CMF's own window (also 20-day) — `accumulation`/
`distribution` (`checks["obv"] and checks["cmf"]`) were previously
combining an all-time-cumulative signal with a 20-day signal, two
mismatched time horizons ANDed together; they now operate on the same
horizon. Interpretation is unchanged (positive = net buying pressure over
the window, negative = net selling) — only the window is fixed to recent
data instead of all-time-since-fetch.

**REAL BEHAVIOR CHANGE:** `checks["obv"]` (and therefore
`checks["accumulation"]`/`checks["distribution"]`, and therefore
`volume_core_score`) will now reflect the last 20 days only, not the
trailing year. For any symbol where an old, no-longer-relevant volume
spike was keeping OBV artificially positive/negative, this will flip.
This is the intended effect — it's the entire point of the fix — not a
side effect. `obv_14`/`obv` values printed anywhere (reports, diagnostics)
will be on a much smaller absolute scale than before (a 20-day rolling sum
vs a 250-day-and-growing cumulative total) — this is expected and correct,
not a bug.

**Scope note:** only the `obv` column formula changed. No other module in
the codebase reads `df["obv"]` directly outside
`buy_strategy.py`/`sell_strategy.py`'s `checks["obv"]` (confirmed via a
full-repo grep before making this change) — no other consumer to update.

---

## Wiring verification

- Ichimoku: confirmed via live `evaluate()` calls that `reasons` no longer
  contains any "Ichimoku"/"cloud" text, `TREND_CORE`'s average is now
  computed over 5 checks (verified the `Trend=` number in the reasons log
  shifted correctly for an unchanged fixture), and grepped both strategy
  files afterward to confirm zero remaining `checks["ichimoku"]`
  references (would have been a `KeyError` at runtime if any survived).
- OBV: confirmed via a standalone synthetic-data test (see above) that the
  new rolling window produces the mechanically-correct answer in a case
  specifically designed to expose the old bug (one big early spike no
  longer masks a genuine later decline).
- Both changes are live in `BuyStrategyEngine.evaluate()`/
  `SellStrategyEngine.evaluate()` and `features/indicators/volume.py`'s
  `VolumeIndicators.calculate()` — the same methods
  `execution/scanner.py:447/454` call on every real scan, and the same
  feature pipeline every scan runs before that.

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_strategy.py`,
  `strategy/sell_strategy.py`, `strategy/buy_scoring.py`,
  `strategy/sell_scoring.py`, `features/indicators/volume.py` — clean
  (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Full regression re-run of all 6 prior phase test scripts — all pass.
  Note: `test_tier2_factor_restructure.py`'s printed `Trend=`/`Passed=X/Y`
  numbers changed (expected — one fewer TREND_CORE check, one fewer total
  check) but all assertions (which check formulas and gating logic, not
  hardcoded totals) still pass.
- New targeted tests (this phase): synthetic OBV series proving the
  rolling-window fix produces the mechanically correct answer where the
  old cumsum did not; live `evaluate()` calls confirming zero remaining
  Ichimoku references in reasons/checks for both BUY and SELL.

## Files changed

- `features/indicators/volume.py`
- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
- `strategy/buy_scoring.py`
- `strategy/sell_scoring.py`
