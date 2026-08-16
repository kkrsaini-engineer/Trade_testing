# Phase 6 — #9 (RelativeStrength rename) + #13 (dead MTF cleanup)

Both confirmed dead-code/mislabeling issues, zero functional/decision
impact before or after — pure hygiene, done for future-clarity reasons
only (see the fact-check exchange right before this delivery).

## #9 — `relative_strength` → `price_vs_20d_mean`

`features/indicators/breakout.py` computed `close / close.rolling(20).mean()`
and called it `relative_strength` — real relative strength means
`stock_return_N / benchmark_return_N` (vs an index like NIFTY); this had
no benchmark comparison at all, ever (the old code even had a comment
admitting it: *"Benchmark integration will be added by Data Engine
later"*). It was also never read by any BUY/SELL check — only surfaced
in `diagnostics`/the CSV report.

**What changed**: the internal DataFrame column and the
`scanner.py` diagnostics key are renamed to `price_vs_20d_mean`.

**What did NOT change**: the CSV report's column HEADER is still exactly
`"RelativeStrength"` — that's your own locked report schema
(`generate_full_report.py`'s `FIELDNAMES` comment: *"Exact column order
the person asked for"*), untouched on purpose. Only the internal source
key feeding that column was corrected
(`"RelativeStrength": d.get("price_vs_20d_mean")` now, was
`d.get("relative_strength")`). **Your CSV output is byte-identical in
shape — same header, same values, same column position.** Verified with
a direct simulation (see testing below).

## #13 — dead MultiTimeframeEngine disconnected

`features/multi_timeframe.py`'s `MultiTimeframeEngine` computed
`mtf_sma_20/50/200`/`mtf_trend` as **plain rolling means of the same
daily close series** — no real weekly/4H resampling ever happened
despite the name. It ran on every single scan and its output was
consumed by **nothing** (verified: zero references anywhere else in the
codebase).

**What changed**: `features/feature_engineering.py` no longer imports or
calls it — removed the `mtf_engine` constructor param and the
`self._mtf.generate(df)` pipeline step. `features/multi_timeframe.py`'s
own docstring now clearly says it's disconnected and why, so it isn't
mistaken for working MTF support if someone opens that file directly
later.

**What did NOT change**: the file `features/multi_timeframe.py` itself
is still in the repo — a zip upload can add/overwrite files but can't
delete existing ones, so if you want it fully gone you'll need to delete
it manually on GitHub. It's harmless sitting there disconnected (not
imported, not called, not computing anything) — just not deleted by this
delivery.

**Effect on real output**: `mtf_sma_20`, `mtf_sma_50`, `mtf_sma_200`,
`mtf_trend` no longer appear in the engineered dataframe at all — since
nothing downstream ever read them (verified), this changes nothing
observable in reports, decisions, or logs. Confirmed via a live
`FeatureEngineeringEngine().generate()` run (see testing).

## Testing

- `flake8` (exact repo config) — clean on all 5 touched files.
- `pytest tests/` — 4/4 pass.
- Direct import sanity check on all 5 touched modules — no import errors.
- **Functional smoke test**: ran `FeatureEngineeringEngine().generate()`
  on a synthetic 260-row OHLCV dataframe — confirmed no `mtf_*` columns
  are produced anymore, `price_vs_20d_mean` is present and computes
  correctly, `relative_strength` (old name) is gone.
- **CSV-contract verification**: simulated `scanner.py`'s diagnostics
  assignment → `generate_full_report.py`'s row-builder chain directly —
  confirmed the output row is still keyed `"RelativeStrength"` with the
  same value it would have had before, proving the report schema is
  unaffected.
- All previously-passing regression suites from Phases 2-5 (14 early-
  entry scenarios, fundamental-scoring tests, exit-engine FII/DII tests,
  8 tier2-factor tests, 3 scoring-unification tests) re-ran clean —
  these files don't overlap with Phase 6's changes, included as a full
  final gate before delivery.
