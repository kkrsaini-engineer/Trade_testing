# Phase 5 — Scoring-Engine Unification (the "is this actually wired?" fix)

## What was found

`execution/scanner.py` runs TWO parallel, independent scoring paths for
every symbol, both feeding `decision/decision_engine.py`:

1. `BuyStrategyEngine`/`SellStrategyEngine` (`strategy/buy_strategy.py`/
   `sell_strategy.py`) — the files worked on across this entire
   conversation (dedup fixes, exact-formula early-entry checks, the
   Phase 4 Trend/Momentum/Volume/Volatility factor-score restructuring).
   Its output (`buy_decision`) only acted as a pass/fail **gate**
   (`buy_decision.passed`) plus 30% weight inside one threshold check
   (`buy_combined`).

2. `BuyScoringEngine`/`SellScoringEngine` (`strategy/buy_scoring.py`/
   `sell_scoring.py`) — a **completely separate, independently
   hand-rolled** ~14-check point-based technical score that never once
   reused any of the strategy-file logic. Its output (`buy_score`) feeds
   `BuyProbabilityEngine`, and **`buy_score`+`buy_probability` are 100%
   of `buy_strength`** in `decision_engine.py` — which is what decides
   BUY-vs-SELL conflict resolution AND is exactly what
   `scripts/generate_full_report.py` sorts by
   (`pending_candidates.sort(key=lambda r: r.ranking, ...)`) to pick the
   final top-N trade candidates.

Net effect before this fix: every technical-quality improvement made
earlier in this conversation controlled *whether a stock could qualify
at all*, but **not which qualifying stocks actually got ranked/selected**
— that was still driven by a cruder, independent scorer that still had
the same `macd_cross`+`macd_histogram` duplicate-vote bug already fixed
in `buy_strategy.py`/`sell_strategy.py`.

## The fix

`BuyScoringEngine.score()`/`SellScoringEngine.score()` now take the
already-computed `buy_decision`/`sell_decision` as a required parameter
and set `result.technical = buy_decision.tier2_score` /
`sell_decision.tier2_score` directly, instead of recomputing an
independent point score. The old `_technical_score()` methods (in both
files) are removed entirely — not deprecated, not left dead — since they
duplicated logic that's now centralized in one place.

`execution/scanner.py`'s two call sites were updated to pass
`buy_decision=buy_decision` / `sell_decision=sell_decision` through
(both were already computed earlier in the same `scan_symbol()`/
`evaluate_position()` call, so this adds zero extra computation — it
reuses a value that used to just be logged/gated and discarded).

### What this changes going forward

Every future fix to `buy_strategy.py`/`sell_strategy.py`'s technical
checks (and everything already done in Phases 2–4 of this conversation)
now automatically reaches `buy_score.technical` → `buy_strength` →
`ranking` → the actual top-N trade selection in
`generate_full_report.py`, not just the qualify/reject gate. One
technical-scoring implementation instead of two silently drifting ones.

### What did NOT change

- `_fundamental_score()` in both scoring files — left as-is. It already
  called `buy_fundamental_score()`/`sell_fundamental_score()` directly
  (the same function `buy_strategy.py`/`sell_strategy.py` use), so there
  was no duplication there to begin with — only `technical` had this
  problem.
- `_liquidity_score()`, `_volatility_score()`, `_risk_score()`,
  `_normalize()` (news/market/sector) — untouched. These don't exist in
  `BuyStrategyEngine` at all (no equivalent to unify against), and
  weren't part of what was flagged.
- `BuyProbabilityEngine`/`SellProbabilityEngine` — untouched, they
  already consume `BuyScore`/`SellScore` generically and needed no
  change; they automatically pick up the new `technical` value.
- `decision/decision_engine.py` — untouched. No interface changed;
  `buy_score`/`sell_score` still have the exact same dataclass shape.
- `overall_score`, `QUALIFY_THRESHOLD`, Tier 1 — all still out of scope,
  same as Phase 4.

## Testing

- `pytest tests/` — 4/4 pass.
- `flake8` (repo's exact `.flake8` config) — clean on all 5 touched
  files (`buy_strategy.py`, `sell_strategy.py`, `buy_scoring.py`,
  `sell_scoring.py`, `scanner.py`).
- No circular-import issue introduced (`buy_scoring.py` → imports
  `strategy.buy_strategy` for the `BuyDecision` type; `buy_strategy.py`
  does not import `buy_scoring.py` back — verified by direct import).
- All previously-passing scenario/regression suites from Phases 2–4
  still pass unchanged (7+7 early-entry scenarios, fundamental-scoring
  exclude-and-renormalize tests, exit-engine FII/DII tests, the 8
  tier2-factor-restructuring tests).
- **New**: `test_scoring_unification.py` — confirms
  `buy_score.technical == buy_decision.tier2_score` /
  `sell_score.technical == sell_decision.tier2_score` exactly (not just
  "close"), and proves the wiring is real (not coincidental) by showing
  a regime change (RANGE_BOUND vs TRENDING ADX) that moves
  `tier2_score` produces the identical move in `buy_score.technical`.
  Also ran a full end-to-end smoke chain
  (`BuyStrategyEngine` → `BuyScoringEngine` → `BuyProbabilityEngine` →
  `DecisionEngine`) confirming `buy_score.technical` genuinely reaches
  `final_decision.ranking`.

## Still true, not addressed here (out of scope for this fix)

`execution/scanner.py` still hardcodes `sector_score=50.0` and
`dataframe["breadth"]=50.0` for every real scan (see Phase 4 notes) —
that gap is unrelated to this unification and still needs real
cross-symbol sector-rotation / market-breadth data wired in separately.
