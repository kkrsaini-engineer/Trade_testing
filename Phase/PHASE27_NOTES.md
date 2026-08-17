# Phase 27 — orchestrator.py / scanner.py market_open/holiday Hardcode (DONE)

Follow-up to PHASE26_NOTES.md's "Still open" list. You approved the
recommended split: fix this trivial, zero-live-risk item now; leave
Point 15, Point 16, `broker.py`'s dead code, and
`orchestrator.py`'s `risk_snapshot` for later (each needs either a
separate isolated phase or a decision from you first).

## What was wrong

Both `orchestrator.py`'s `run_cycle()` and `execution/scanner.py`'s
`prepare_orders()` hardcoded `market_open=True`/`holiday=False`
unconditionally — the exact same bug pattern, in two places, with a
`TODO: replace with a real NSE market-hours/holiday-calendar check`
comment already sitting on the orchestrator.py copy.

**Confirmed while fixing this**: neither hardcode has ANY live effect
today. `orchestrator.py`'s `WiredOrchestrator.run_cycle()` has no
caller anywhere in `scripts/` (established in PHASE23_NOTES.md).
`execution/scanner.py`'s `prepare_orders()` has no caller anywhere in
this codebase either — not even `orchestrator.py` itself, despite that
class's own docstring claiming it "matches the exact call inside
orchestrator.py (Step 7)" (orchestrator.py's `run_cycle()` actually
calls `scan_symbols()` directly, never `prepare_orders()` — that
docstring claim is stale/false, same class of self-contradicting
comment as `daily_scan.py`'s in Phase 26). Fixed anyway for
correctness/consistency, since this was cheap and genuinely zero-risk
(no live path touched either way), not because anything currently
depends on it.

## What was fixed

New shared helper, `core/trading_calendar.py`'s `market_open_now(now=
None)` — reuses the already-existing `is_trading_day()` (weekday +
NSE holiday calendar) and adds a normal-session-hours check (9:15 AM –
3:30 PM IST, `MARKET_OPEN_TIME`/`MARKET_CLOSE_TIME` constants). Both
`orchestrator.py` and `execution/scanner.py` now call this ONE shared
function instead of each hardcoding `True`/`False` independently —
avoids the exact kind of drift (two copies of "the same" logic quietly
diverging) this whole review keeps finding elsewhere.

`holiday` is derived directly from `is_trading_day()` at each call
site (`not is_trading_day(now.date())`) — a trading day is, by this
codebase's existing definition, "not Saturday, not Sunday, not an NSE
holiday," so `holiday=True` now correctly covers weekends too, not
just the festival-holiday list (a strictly more correct interpretation
than the field name alone might suggest, but consistent with how
`validation_engine.py` already treats `weekend`/`holiday` as related-
but-separate checks).

## Files changed

- **`core/trading_calendar.py`** — new `market_open_now()`,
  `MARKET_OPEN_TIME`/`MARKET_CLOSE_TIME` constants.
- **`orchestrator.py`**, **`execution/scanner.py`** — both switched from
  hardcoded `market_open`/`holiday` to the shared helper.
- **`tests/test_trading_calendar_market_open.py`** (new, 7 tests) —
  session-hours boundaries, weekend, NSE holiday.

## Verification

```
python3 -m pytest tests/ -q                    # 168 passed (161 prior + 7 new)
python3 -m flake8 --config=.flake8 .            # whole repo, clean
```

## Still open (unchanged from PHASE26_NOTES.md)

- Point 15 (explicit `position_state` state machine) — recommended to
  stay its own isolated phase.
- Point 16 (30-day flat time-exit → thesis-decay-based) — recommended
  to stay its own isolated phase.
- `execution/broker.py`'s ~400 lines of dead simulation code — blocked
  on your revive-vs-delete decision.
- `orchestrator.py`'s hardcoded `risk_snapshot` — blocked on either a
  real volatility/liquidity_risk data source (needs surfacing
  `RiskResult.volatility_risk`/`liquidity_risk` into scanner.py
  diagnostics first, plus your sign-off on the `liquidity_risk`
  threshold) or your confirmation to leave it hardcoded/documented.
