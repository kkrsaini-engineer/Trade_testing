# PHASE 10 — Fix #8 (sector/breadth placeholders), #11 (no-news confidence), #9 status check

Implemented on explicit request ("now fix 8,9 and 11(also check wired)"),
with fact-checking against actual code before implementing.

---

## #9 — already fixed, nothing to do

**Re-checked the actual code before doing anything** — the pasted #9 text
describes exactly what Phase 6 (earlier in this conversation) already
fixed: `features/indicators/breakout.py` no longer has any
`relative_strength` column; it's `price_vs_20d_mean` end to end
(`breakout.py` → `execution/scanner.py`'s diagnostics →
`scripts/generate_full_report.py`'s `RelativeStrength` CSV column, header
unchanged as promised). Confirmed via a fresh grep across the whole repo —
the only remaining occurrences of the string `"relative_strength"` are
inside comments explaining the old name, not live code. **No action taken
— re-doing an already-complete fix isn't "fixing" anything, just churn.**

---

## Fix #11 — no-news confidence (the narrower, still-real bug)

**Verdict confirmed as scoped: `strategy/buy_scoring.py`'s/`sell_scoring.py`'s
`_confidence()` still had the bug; the Tier3 `tier3_score` blend in
`buy_strategy.py`/`sell_strategy.py` was already correct and untouched.**

`result.news = 0.0` when there's genuinely no news (`has_news=False`).
`result.overall`'s weighted sum already excludes it correctly (via
`scale`/`news_w`), but `_confidence()` built its `mean`/`std` from a flat
array of all 8 (7, before this fix) score components including the
fabricated `result.news = 0.0` — dragging down both the mean and pulling
the std (consistency) around, purely because of a component that was never
actually measured.

**Fix applied:** `_confidence()` now takes a `has_news` flag and excludes
`result.news` from its array entirely when there's no news — mirroring the
`scale`/`news_w` exclusion pattern `result.overall` already used. Applied
to both `buy_scoring.py` and `sell_scoring.py`.

**Advantage:** confidence no longer gets penalized for something the score
itself correctly doesn't penalize — the two numbers are consistent again.
**Disadvantage:** none — small, contained, matches an already-proven
pattern in the same file.

---

## Fix #8 — sector/breadth hardcoded 50.0 placeholders

**Verdict confirmed, and a bonus bug found while fixing it (see below).**

`execution/scanner.py` hardcoded `sector_score = 50.0` and
`dataframe["breadth"] = 50.0` for every real scan — cross-symbol
sector-rotation and market-wide breadth data were never wired into the
per-symbol pipeline. A fabricated `50.0` was indistinguishable downstream
from "measured and genuinely neutral," and fed directly into weighted
blends as if it were real data.

**Bonus bug found:** the old placeholder was a **float** (`50.0`), but
`buy_strategy.py`'s/`sell_strategy.py`'s Tier2 check does
`row.get("breadth", "NEUTRAL") == "STRONG"` — a float can never equal that
string, so `checks["breadth"]` was **unconditionally `False` in every real
scan**, not "neutral," literally always-false. Found while tracing the fix
through, not previously flagged.

**Fix applied — `None` instead of a fabricated number, mirroring the
already-established `has_news`/`news_score=None` convention used
elsewhere in this same codebase:**

- `execution/scanner.py`: `sector_score = None`, `dataframe["breadth"] =
  None`.
- `buy_strategy.py`/`sell_strategy.py`: `checks["sector"]` guarded
  against `None >= 70` raising `TypeError`
  (`sector_score is not None and sector_score >= 70`). The Tier3
  `market_context_score` blend now computes `has_breadth`/`has_sector`
  and **redistributes** their weight (25% each) to `market_score` when
  unavailable, instead of blending in a fabricated 50.0 — the same
  weight-redistribution pattern the `tier3_score` news exclusion already
  used, generalized to this blend too.
- `buy_scoring.py`/`sell_scoring.py`: `result.overall`'s weight formula
  generalized from a `has_news`-only exclusion to also exclude
  `SECTOR_WEIGHT` when `sector_score` is unavailable (`excluded_weight =
  news + sector`, `scale = 1/(1-excluded_weight)`). `_confidence()`
  likewise excludes `result.sector` from its array when unavailable
  (same fix as #11, generalized).

**REAL BEHAVIOR CHANGE — flagged explicitly, not just cosmetic:** since
sector/breadth are unavailable in every real scan today,
`market_context_score` now equals `market_score` (or `inverted_market` for
SELL) **directly**, at 100% weight — not diluted toward a fabricated 50
for half its weight as before. `result.overall` in both scoring engines
similarly no longer has 10% of its weight silently anchored to a
fabricated neutral sector score. **Tier3/overall_score numbers will shift
for every symbol** as a direct result of this fix — this is the intended,
correct effect of "stop fabricating data," not a side effect to work
around.

**Separately flagged, not fixed (out of scope, zero real impact today):**
`sell_strategy.py`'s `checks["sector"]` uses the same `>= 70` threshold as
`buy_strategy.py`'s (unlike `market_score`, which fix #2 correctly
inverted for SELL) — the reason text ("Sector showing relative weakness")
implies it should be direction-aware, but the caller (`scanner.py`) passes
the identical `sector_score` to both engines. This has zero real effect
while `sector_score` is unavailable (`None`) in every real scan, but is
flagged in-code so it isn't silently reintroduced as a live bug the day
sector data actually gets wired in — the same class of issue fix #2 found
for `market_score`.

**Also NOT done (explicitly out of scope):** wiring real sector-rotation
or market-breadth data. `market/sector_rotation.py`'s
`SectorRotationEngine` and `market/market_breadth.py`'s
`MarketBreadthEngine` **already exist** in the repo but are never fed
real market-wide data by the per-symbol scan pipeline — building that
feed (a cross-symbol sector-index dataframe; market-wide advance/decline
counts) is new data infrastructure, the same class of work as fix #4's
fundamentals point-in-time gap, and this sandbox has no path to verify
against a real NSE data source for it. Once that data exists, this fix's
blend picks it up automatically — no further code change needed in
`buy_strategy.py`/`sell_strategy.py`/`buy_scoring.py`/`sell_scoring.py`.

**Advantage:** removes two fabricated-data channels from every real BUY/
SELL score, closes a check that was silently dead
(`checks["breadth"]` — was always `False` regardless of the fabricated
value's type), and leaves a forward-compatible landing spot for real
sector/breadth data with zero further code change needed. **Disadvantage:**
the flagged real behavior change — every symbol's Tier3/overall scores
shift today, not just once real data exists — needs to be understood as
intentional, not a regression, when reviewing before/after numbers.

---

## Wiring verification (all three)

- **#9**: confirmed already wired since Phase 6 — `execution/scanner.py`
  diagnostics → `generate_full_report.py`'s `RelativeStrength` CSV column,
  live path, no `relative_strength` (old name) anywhere in live code.
- **#11**: `_confidence(result, has_news=has_news)` call sites are inside
  `BuyScoringEngine.score()`/`SellScoringEngine.score()` —
  `execution/scanner.py` calls `self.buy_score.score(...)`/
  `self.sell_score.score(...)` on every real scan (unchanged call sites,
  only the internal `_confidence()` signature changed). Confirmed via a
  direct `_confidence()` call showing a different numeric result with
  `has_news=False` vs not excluding news, and via the full regression
  suite passing with the new signature live.
- **#8**: confirmed via direct source inspection that
  `MarketScanner._evaluate_market_context()` (the method
  `scan_symbol()`/`evaluate_position()` both call on every real scan)
  sets `sector_score = None` / `dataframe["breadth"] = None`. Confirmed
  the full chain (`scanner.py` → `buy_strat.evaluate()`/
  `sell_strat.evaluate()` → `buy_score.score()`/`sell_score.score()`)
  runs end-to-end with these `None` values with **no exceptions** via a
  live `evaluate()` call (not just a unit-level check), and that
  `_confidence()`'s numeric output genuinely changes based on
  `has_sector` (76.17 vs 78.73 in a synthetic test case), confirming the
  exclusion is real, not a no-op.

## Testing performed

- `flake8 --config=.flake8` on all 5 changed files — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check on all touched modules — clean.
- Full regression re-run of all 6 prior phase test scripts — all pass, no
  regressions (Tier3 numbers in these fixtures happen to land on the same
  values before/after in the specific test scenarios used, since those
  fixtures already supplied real breadth/sector data — the fabricated-vs-
  None distinction only bites when sector/breadth are genuinely absent,
  which was exercised separately below).
- New targeted smoke tests (this phase): `sector_score=None` +
  `breadth=None` fed through both `BuyStrategyEngine.evaluate()` and
  `SellStrategyEngine.evaluate()` — no crash, sane `tier3_score`; direct
  `_confidence()` calls confirming `has_sector`/`has_news` measurably
  change the output.

## Files changed

- `execution/scanner.py` (#8)
- `strategy/buy_strategy.py` (#8)
- `strategy/sell_strategy.py` (#8)
- `strategy/buy_scoring.py` (#8, #11)
- `strategy/sell_scoring.py` (#8, #11)
