# Phase 7 — #1 fix: Kelly no longer sized off an uncalibrated probability

## Correction to my earlier assessment

I originally said the real fix needed new data infrastructure (a trade-
outcome calibration table) and would take weeks/months, with only a
"~20-line fixed-risk-% fallback" landable now. Re-reading the full
`risk/position_sizing.py` before touching it turned up something I'd
missed: **a genuine fixed-risk-per-trade path already exists**
(`atr_quantity = risk_per_trade / stop_distance`, where
`risk_per_trade = total_capital * MAX_RISK_PER_TRADE (2%)`) — completely
independent of Kelly. `executable_quantity = min(atr_quantity,
capital_quantity)` already takes the smaller of the two. So this wasn't
"build a fixed-risk-% system from scratch" — it was "stop the OTHER
existing path (`capital_quantity`, Kelly-scaled) from being driven by a
fabricated number." That's a contained code fix, not a new-data project.

## The bug (confirmed, then fixed)

`win_rate` fed into the Kelly formula came from
`BuyProbabilityEngine._win_probability()` — a sigmoid transform of a
composite score, never calibrated against a single closed trade.
`reward_risk` (the formula's other input) came from the same
uncalibrated chain (`expected_return`/`expected_drawdown`). Verified via
a direct test: two identical trades differing ONLY in
`buy_probability` (10% vs. 95%) produced the exact same `kelly_fraction`
in the old code's formula-free world — no wait, the OLD code would have
produced very different `kelly_fraction`/`capital_quantity` for those
two — that's the bug. Also confirmed via the same test that
`capital_quantity` genuinely IS the binding constraint in realistic
cases (not always shadowed by `atr_quantity`) — so this wasn't a
theoretical risk, it actively changed real position sizes.

## The fix

`risk/position_sizing.py`:
- Added `KELLY_CALIBRATED = False` (class constant, flip once real
  calibration data exists) and `FALLBACK_KELLY_FRACTION = 0.5` (fixed,
  applies identically to every qualifying trade).
- `kelly_fraction` is now computed from the old sigmoid formula ONLY if
  `KELLY_CALIBRATED` is `True`; otherwise it's the fixed constant.
- The old `win_rate`/`reward_risk` locals are renamed
  `uncalibrated_win_rate`/`reward_risk` and are still computed and
  written to `diagnostics` (now `diagnostics["uncalibrated_win_rate"]`,
  `diagnostics["reward_risk"]`, `diagnostics["kelly_calibrated"]`) —
  visible in logs/reports for comparison once real calibration exists,
  just no longer driving sizing while uncalibrated.
- `debug_report()` now prints the calibration state and the uncalibrated
  reference value alongside the actual Kelly fraction used, matching
  this conversation's established live-production-visibility pattern.

**`FALLBACK_KELLY_FRACTION = 0.5` is a judgment call, not a uniquely
correct number** — it's the midpoint of the allowed [0,1] range, chosen
so it doesn't produce a jarring size discontinuity vs. what the old
uncalibrated formula typically produced for a barely-qualifying trade
(~0.35-0.40 at the 58-point qualify threshold, by direct calculation).
If you'd rather default to the most conservative end (0.0 → every trade
gets `MIN_CAPITAL_ALLOCATION` only) or a different fixed value, that's a
one-line change (`FALLBACK_KELLY_FRACTION`) — flag it if 0.5 isn't the
right call for your risk appetite.

## What did NOT change

- `atr_quantity`'s fixed-risk-per-trade math — untouched, was already
  correct, was never Kelly-driven.
- `decision.buy_probability`/`sell_probability`/`expected_return`/
  `expected_drawdown` themselves — untouched (still computed, still
  shown in reports/reasons elsewhere) — this fix only stops them from
  feeding the Kelly/capital-allocation formula, it doesn't remove them
  from the system.
- `MIN_QUANTITY=1` force-override (#6 from the earlier review) — NOT
  touched here; that's a separate, still-open issue, not part of this
  request.
- Everything downstream of `kelly_fraction` (volatility/liquidity/
  confidence/risk adjustment factors, MIN/MAX allocation clamps,
  `min(atr_quantity, capital_quantity)`, position value/exposure checks)
  — all untouched, all still apply exactly as before.

## Testing

- `flake8` (exact repo config) — clean.
- `pytest tests/` — 4/4 pass.
- **Direct functional test** (`PositionSizingEngine.calculate()` with two
  otherwise-identical decisions differing only in `buy_probability`
  10% vs. 95%): confirmed `kelly_fraction` and final `quantity` are now
  IDENTICAL between the two (114 shares each), where the old formula
  would have produced very different values — and confirmed
  `atr_quantity` (the real risk-based path) is unaffected (1250 in both
  cases), while `capital_quantity` (114) was the actual binding
  constraint in this test — proof this bug had real, not just
  theoretical, impact on position sizes.
- All previously-passing regression suites from Phases 2-6 (14 early-
  entry scenarios, fundamental-scoring tests, exit-engine FII/DII tests,
  8 tier2-factor tests, 3 scoring-unification tests) re-ran clean as a
  final gate — none overlap with this file, included for completeness.
