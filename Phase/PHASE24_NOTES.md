# Phase 24 — broker.py Dead-Code Fix (DONE) + orchestrator.py (NOT done, explained)

Follow-up to PHASE23_NOTES.md's Part B findings. You said do all 3 items
together. Implemented 1 of 3 cleanly; found the 2nd is much bigger than
described; declined to fabricate a fix for the 3rd (real per-stock
circuit data) rather than pretend something is "wired" when it isn't.

## DONE — `execution/broker.py`'s circuit_breaker dead code

Moved the `circuit_breaker`/`spread`/`volatility` rejection checks to
right after `volatility`/`spread` are read from `market_state` (still
before the fill-probability simulation runs), so an order with an active
circuit breaker, spread > 5%, or volatility > 20% is now actually
rejected instead of always falling through to the fill simulation.
Thresholds (`0.05`, `0.2`) were already written by whoever authored this
code — not invented here, just made reachable.

**Found something bigger while doing this — flagging, not fixing.** The
dead code didn't end at the circuit_breaker check. From the original
`return result` (line 408) all the way to a SECOND `return result` near
the end of the function (line ~823) — roughly **400 lines** — is ALL
unreachable: a retry mechanism, order-book-depth simulation, an
execution-quality scoring model, a detailed brokerage-fee breakdown,
audit-warning checks, and a second, more elaborate `OrderResult`
construction. None of it runs, regardless of input, for as long as that
early return has existed. `place_order()` currently returns the much
simpler result built earlier in the function (basic fill probability +
slippage + partial fill + market impact — no retries, no order-book
model, no detailed fee breakdown).

Did NOT touch that block. Reasons:
- It's a completely separate decision from "fix circuit_breaker" — do you
  want the more elaborate execution simulation to become the live one
  (replacing the simpler one currently active), or is the simpler one
  what you actually want and the rest should just be deleted as
  confirmed-dead cleanup? That's a real product choice, not a bug fix —
  reviving 400 lines of a different simulation model changes brokerage
  costs, fill quality metrics, and diagnostics shape for every backtest
  order going forward. Not something to guess at.
- `BrokerEngine.place_order()` is called from `analytics/backtest_engine.py`
  and `execution/trade_executor.py` — not the live daily paper-trading
  cycle — so this doesn't block anything currently running, no urgency
  to decide immediately.

Flagging it here rather than either silently reviving it or silently
deleting it.

## NOT DONE — `orchestrator.py`'s hardcoded `risk_snapshot`

Traced what it would take to make this real, and stopped short of
writing something, because every path available right now would mean
inventing a formula rather than fixing a bug:

```python
risk_snapshot = {"volatility": 0.0, "liquidity_risk": False}
```

`circuit_breaker(risk_check)` needs `volatility` as a fraction (compared
against `0.03`) and `liquidity_risk` as a genuine boolean (`if
risk_check.get("liquidity_risk", False):` — note: NOT a threshold
comparison, a raw truthiness check, so feeding it any nonzero score would
make it fire on almost anything). Checked what's actually available at
that point in `run_cycle()`: `scan_candidates` (this cycle's per-symbol
scan results) expose `diagnostics["total_risk"]`/`diagnostics["risk_safe"]`
— a 0-100 blended score and a boolean — but NOT the individual
`volatility_risk`/`liquidity_risk` sub-scores `RiskResult` computes
internally (those aren't currently surfaced into `scanner.py`'s
diagnostics at all).

I could derive *some* number from `total_risk` (e.g. `avg_total_risk /
1000`) and *some* boolean from `risk_safe` counts, and it would run
without errors — but it would be a formula I invented to make a
previously-hardcoded value "not hardcoded," not a fix grounded in what
`volatility`/`liquidity_risk` are actually supposed to mean. Given
`WiredOrchestrator` has no caller anywhere in the live pipeline (confirmed
in Phase 23 — nothing in `scripts/` invokes it), doing this would produce
code that LOOKS wired but reflects a number I made up, for a code path
nothing runs. That's the same "looks fixed, isn't real" problem this
whole review has been finding and correcting in other people's code —
didn't want to add a new instance of it myself just to close out the
task list.

If you want this done anyway, I need one of:
- A real definition for portfolio/market volatility and liquidity risk at
  this point in the cycle (e.g., surface `RiskResult.volatility_risk`/
  `liquidity_risk` into `scanner.py`'s diagnostics first, then aggregate
  those across `scan_candidates` — a small, honest addition, not a guess),
  with your sign-off on what threshold makes `liquidity_risk` "True", or
- Confirmation that this is low-enough priority to just leave hardcoded
  and documented as such (it already has a comment above it noting it
  should eventually be improved) — since it currently affects nothing.

## PART 3 (from PHASE23_NOTES.md) — still not attempted, same reasoning

Real per-stock NSE circuit-limit data (upper/lower circuit bands) isn't
fetched anywhere in this codebase — the only market-data source is
`yfinance` (`data/market_data.py`), a free/unofficial feed that doesn't
carry NSE-specific circuit-band fields. Making the LIVE system's
`market["circuit_breaker"]` genuinely reflect real conditions needs a
real data source (a broker API like Kite Connect, or a paid NSE data
vendor) that isn't integrated here and that I don't have credentials for
even if I wanted to add the integration code. This is new
data-acquisition work requiring an account/API-key decision on your end,
not something I can wire from what already exists in the repo.

## Files changed

- **`execution/broker.py`** — circuit_breaker/spread/volatility rejection
  checks moved from unreachable dead code to before the fill-probability
  simulation.
- **`tests/test_broker_rejection_checks.py`** (new, 8 tests, mirrored
  BUY/SELL) — each of the 3 rejection reasons fires correctly; normal
  conditions are NOT rejected by these checks (falls through to the
  existing fill simulation instead).

## Verification

```
python3 -m flake8 --config=.flake8 .          # whole repo, clean
python3 -m pytest tests/ -q                    # 97 passed (89 from Phase 23 + 8 new)
```
