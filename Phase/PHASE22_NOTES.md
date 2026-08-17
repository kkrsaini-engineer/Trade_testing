# Phase 22 — Wiring Real Drawdown / Daily-Loss Data + Graduated Throttling

Follow-up to PHASE21_NOTES.md's "9 + 10" section, where implementing your
originally-requested graduated drawdown bands + daily-loss hierarchy
turned out to require wiring real input data first (the inputs were
always `0.0`/`False` in the live pipeline — see that file for the full
discovery). You confirmed: mark-to-market basis, auto-link
`emergency_stop` to the new graduated systems' most severe stage, and fix
`circuit_breaker`'s wrong-data-source bug in the same phase. All three
implemented, plus the originally-designed graduated bands/hierarchy on
top of them.

## 1. New shared module — `risk/portfolio_limits.py`

Single source of truth for drawdown bands and daily-loss stages, per the
design confirmed earlier:

- **Drawdown**: 0-5% normal (×1.0) / 5-10% reduced (×0.75) / 10-15%
  heavily reduced (×0.50) / >15% halt (×0.0, new trades rejected).
  Replaces the old flat `>20%` reject gate.
- **Daily loss**: warning 2% (no restriction) / risk_reduction 3% (×0.50
  allocation) / trading_halt 4% (new trades rejected) / emergency 5%
  (hard risk-manager override). Replaces THREE independently-hardcoded
  copies (portfolio_rules.py's 3%, risk_manager.py's `5.0` unit-bug
  constant, and validation_engine.py's own separate 3% — see below).
- `is_emergency_condition()` — True when drawdown hits `halt` OR
  daily-loss hits `emergency`. This is now the real trigger source for
  `emergency_stop` (point 13's gap, closed).

Dependency-free by design (no imports from elsewhere in the repo) so both
`risk/` and `portfolio/` can import it without a circular import.

## 2. Real equity tracking — `portfolio/portfolio.py`

`PortfolioState` gains three fields: `peak_equity`, `day_start_equity`,
`current_trading_day`. New `PortfolioEngine.update_equity_tracking(trading_day)`:

- Computes current mark-to-market equity (`available_capital` + open
  positions' market value — same formula `VirtualPortfolio.snapshot()`
  already used independently for `portfolio_value`).
- Updates `peak_equity = max(peak_equity, current_equity)` — never
  regresses on a pullback (tested).
- Resets `day_start_equity` to the current equity ONLY when
  `trading_day` differs from the last recorded one — captures the
  baseline once per day, before that day's activity, and holds steady
  for the rest of the day (tested, including that a new day's baseline
  can be BELOW the original capital without carrying forward yesterday's
  loss).

`snapshot()` now returns real `max_drawdown`, `daily_loss`, and
`emergency_stop` (via `portfolio_limits.is_emergency_condition()`) —
computed fresh from `peak_equity`/`day_start_equity`/current equity, not
read from a key nobody ever set.

## 3. Wiring into the daily cycle — `paper_trading/paper_trading_engine.py`

`run_cycle()` calls `update_equity_tracking(today)` twice:

1. At the very top, right after `today` is computed — using YESTERDAY's
   persisted closing equity (each cycle is a fresh process per
   `VirtualPortfolio`'s own docstring) as today's starting baseline,
   before any of today's monitoring/entries/exits run.
2. Right after `mark_to_market()` near the end — refreshes `peak_equity`
   with the day's final numbers. `day_start_equity` is untouched on this
   second call (same `trading_day`, so it's a no-op on that field).

## 4. Persistence — `paper_trading/virtual_portfolio.py`

`peak_equity`/`day_start_equity`/`current_trading_day` added to the saved
JSON. Backward-compatible with the existing state file on disk (tested):
missing keys default `peak_equity`/`day_start_equity` to `total_capital`
(a clean "no drawdown/loss yet" baseline) and `current_trading_day` to
`""` (forces the very next cycle to treat it as a new day and capture a
correct baseline).

## 5. Graduated throttling wired into `risk/portfolio_rules.py`

- `MAX_DRAWDOWN = 0.20` class constant removed — the old binary gate is
  gone, replaced entirely (not layered) by
  `portfolio_limits.drawdown_band_label()`/`drawdown_multiplier()`.
- Daily-loss binary gate (`>= 0.03`) replaced by
  `portfolio_limits.daily_loss_stage()`.
- `allocation_allowed` now additionally scales by
  `drawdown_multiplier(portfolio_drawdown)` and
  `daily_loss_multiplier(daily_loss)`, on top of (not instead of) the
  existing governance-score cascade — drawdown is still also one of the 9
  `governance_checks` (now `!= "halt"` instead of `<= 0.20`), but that
  alone only ever contributes a small, equally-weighted slice of
  `portfolio_score`; these two multipliers make severity-specific scaling
  explicit, matching what you asked for.
- New diagnostics: `drawdown_band`, `daily_loss_stage`.

## 6. `risk/risk_manager.py` — unit bug fixed + circuit_breaker wiring fixed

- `MAX_DAILY_LOSS` was hardcoded `5.0` — a **unit bug**. Every other
  `daily_loss` reader in the codebase treats it as a 0.0-1.0 fraction
  (0.05 = 5%); with `daily_loss` now actually being computed as a real
  fraction (point 5), a lingering `>= 5.0` comparison would have been
  permanently dead (would require a 500%+ loss). Now sourced from
  `portfolio_limits.DAILY_LOSS_EMERGENCY` (0.05) — fixes the bug and
  removes the last of the three drifted daily-loss thresholds.
- `circuit_breaker` was read via `latest.get("circuit_breaker", False)` —
  `latest = dataframe.iloc[-1]`, a price/indicator DataFrame row. No
  producer anywhere in the data/feature pipeline ever wrote a
  `circuit_breaker` column onto that dataframe — this check could never
  fire, full stop, regardless of real market conditions. Now reads
  `market.get("circuit_breaker", False)` — the actual dict callers pass
  real market-condition flags through. **Still defaults to False** — no
  producer currently sets `market["circuit_breaker"]` either, so this
  alone doesn't make the check live; it fixes the wrong-data-structure
  bug so a future real producer (e.g. wiring in `orchestrator.py`'s own
  working `circuit_breaker()` method, or real NSE circuit-filter data)
  only has to populate one correct key, not also fix this read. That
  further wiring is out of scope for this phase — flagging it here so
  it isn't mistaken for "circuit breaker is now fully live."

## 7. `decision/validation_engine.py` — the third duplicate, found and fixed

Not in the original point-10 scope — found while tracing daily_loss's
producers in Phase 21. This file had its OWN, fully independent copies of
the same three checks:

- `MAX_DAILY_LOSS = 0.03` (a third hardcoded daily-loss threshold, on top
  of portfolio_rules.py's 3% and risk_manager.py's since-fixed unit-bug
  5.0) — removed, now uses `portfolio_limits.daily_loss_stage()`,
  rejecting at the same `trading_halt`/`emergency` stages
  `portfolio_rules.py` does.
- `checks["drawdown"] = max_drawdown <= 0.20` (a second hardcoded 20%
  drawdown cutoff) — now uses `portfolio_limits.drawdown_band_label() !=
  "halt"`.
- `checks["circuit"]` — same `latest`-instead-of-`market_state` bug as
  risk_manager.py, same fix (reads `market_state` instead).

Left untouched: `MAX_WEEKLY_LOSS`/`MAX_MONTHLY_LOSS` and their checks —
same "never populated" shape almost certainly applies to `weekly_loss`/
`monthly_loss` too, but that's a new, separate finding outside what this
phase was scoped to fix. Not silently ignored — flagging it here in case
you want it looked at next.

## Files changed

- **`risk/portfolio_limits.py`** (new) — shared bands/stages/multipliers.
- **`portfolio/portfolio.py`** — `peak_equity`/`day_start_equity`/
  `current_trading_day` state fields, `update_equity_tracking()`,
  `snapshot()` now returns real `max_drawdown`/`daily_loss`/
  `emergency_stop`.
- **`paper_trading/virtual_portfolio.py`** — persists the 3 new state
  fields, backward-compatible defaults for existing saved state.
- **`paper_trading/paper_trading_engine.py`** — 2 `update_equity_tracking()`
  call sites in `run_cycle()`.
- **`risk/portfolio_rules.py`** — graduated drawdown/daily-loss throttling
  replacing the old binary gates; 2 new diagnostics.
- **`risk/risk_manager.py`** — `MAX_DAILY_LOSS` unit-bug fix +
  `circuit_breaker` read-source fix.
- **`decision/validation_engine.py`** — same two fixes as risk_manager.py
  (this file's own independent copies), plus removed its own duplicate
  `MAX_DAILY_LOSS` constant.
- **`tests/test_portfolio_limits.py`** (new, 14 tests) — bands/stages/
  multipliers/emergency-condition, boundary values.
- **`tests/test_portfolio_equity_tracking.py`** (new, 11 tests) — peak
  tracking never regresses, daily baseline resets on a new trading day
  and doesn't carry over prior-day losses, emergency_stop auto-links,
  open-position market value counts toward equity.
- **`tests/test_portfolio_rules.py`** (new, 8 tests) — each drawdown band
  and daily-loss stage's effect on `allowed`/`allocation_allowed`.
- **`tests/test_validation_engine_risk_checks.py`** (new, 6 tests) —
  circuit/daily_loss/drawdown checks against the shared module.
- **`tests/test_risk.py`** — updated the Phase 21 circuit_breaker tests
  to inject the flag via `market` (matching the now-fixed read source)
  instead of the dataframe.

## Verification

```
python3 -m flake8 --config=.flake8 .          # whole repo, clean
python3 -m pytest tests/ -q                    # 84 passed (45 from Phase 21 + 39 new)
```

Also manually verified: `VirtualPortfolio` state round-trips through
save/reload correctly, and loading the actual OLD-format
`virtual_portfolio_state.json` shape (predating this phase) doesn't crash
and produces sensible drawdown/daily-loss numbers from the backward-compat
defaults.
