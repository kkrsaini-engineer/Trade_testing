# PHASE 8 — Fix #2 (SELL market-score direction) + Fix #3 (missing-fundamentals coverage)

Two independent fixes from the 16-point architecture review, implemented on
explicit request ("1,9,13 fix ho gya ab 2 or 3 kro").

---

## Fix #2 — SELL market-score direction inconsistency

**Problem:** `market_score` is BUY-oriented — `execution/scanner.py` sets it
BULL regime = 75, SIDEWAYS = 50, BEAR regime = 25. For a SELL setup, a BEAR
market should score HIGH (favorable for shorting/exiting longs), not low.
`strategy/sell_strategy.py`'s Tier-3 blend (`inverted_market`) already
inverted this correctly from earlier work. Two other spots that consume the
same `market_score` did NOT invert it:

1. `strategy/sell_strategy.py` — Tier-2 boolean check:
   `checks["market_score"] = market_score >= 60` (wrong direction — this is
   the BUY threshold applied to a SELL check).
2. `strategy/sell_scoring.py` — `result.market = self._normalize(market_score)`
   (fed straight through, unindirected, into `sell_probability.py`'s
   composite and into the final `sell_strength`/ranking).

**Confirmed real-world impact (not theoretical):** `strategy/sell_probability.py`
generates the reason text "Overall market supports bearish trades." when
`score.market >= 70`. Before this fix that could only ever fire on a BULL
market_score of 75 — i.e. the message was firing on bullish markets, exactly
backwards.

**Note on scope:** `strategy/buy_scoring.py`'s `result.market =
self._normalize(market_score)` was checked and is correct as-is — it's the
BUY side, so non-inverted is right. No change made there. (Earlier draft
review language had miscounted this as a 3rd broken spot; re-verified against
actual code before fixing — it isn't.)

**Fix applied:**

```python
# strategy/sell_strategy.py — Tier2 boolean
# was: checks["market_score"] = market_score >= 60
checks["market_score"] = market_score <= 40
```

```python
# strategy/sell_scoring.py — result.market
# was: result.market = self._normalize(market_score)
result.market = 100.0 - self._normalize(market_score)
```

Both mirror the sign/threshold convention `sell_strategy.py`'s Tier-3 blend
already used, so all three SELL market-direction consumers are now
consistent with each other.

**Advantage:** SELL setups are no longer penalized for the market being
bearish (the one condition that should HELP a SELL signal) — this was
silently working against the strategy's own SELL logic on every scan.
**Disadvantage:** none identified — this is a strict directional bug fix
with no new tunable parameters or behavior trade-offs.

---

## Fix #3 — Missing fundamentals ≠ weak fundamentals (coverage exposure)

**Problem:** `strategy/fundamental_scoring.py` already excludes-and-renormalizes
missing metrics (Phase 3 work) instead of penalizing them as bad readings —
but a plain 0-100 score alone can't tell a caller "72/100 built from 8/8
metrics" apart from "72/100 built from 1/8 metrics." The second is far less
trustworthy even though the number looks identical.

**Fix applied:** Added a `FundamentalEvidence` dataclass
(`score`, `coverage`, `available_metrics`, `total_metrics`) and
`buy_fundamental_evaluation()` / `sell_fundamental_evaluation()` functions in
`strategy/fundamental_scoring.py`. `buy_fundamental_score()` /
`sell_fundamental_score()` are kept as thin wrappers around these — **byte-
identical behavior**, verified against the existing test suite
(`/tmp/test_fundamental_scoring.py`: empty=0.0/100.0, all-healthy=100.0,
ROE-missing=100.0, mixed=54.44 — all match exactly, before and after). No
existing caller (`buy_scoring.py`, `sell_scoring.py`, `risk/exit_engine.py`)
needed any change.

`fundamental_coverage` is threaded through:

- `strategy/buy_strategy.py` — new `fundamental_coverage: float = 0.0` field
  on `BuyDecision`, populated from `buy_fundamental_evaluation()`, plus a new
  reasons line: `"Fundamental data coverage: N/8 metrics (XX%)."`
- `strategy/sell_strategy.py` — mirrored exactly (`SellDecision.
  fundamental_coverage`, `sell_fundamental_evaluation()`, matching reasons
  line).
- `execution/scanner.py` — `diagnostics["buy_fundamental_coverage"]` /
  `diagnostics["sell_fundamental_coverage"]` added next to the existing
  `buy_fundamental_health` / `sell_fundamental_weakness` diagnostics.

**Deliberately NOT done:** no gating or rejection logic was added on low
coverage. This is exposure only — what to DO with a low-coverage score
(reject the check, downweight it further, just log it) is a policy decision
left open for a future request, not decided unilaterally here.

**Advantage:** a scan/report can now distinguish a well-evidenced
fundamental score from a thinly-evidenced one instead of treating both as
equally trustworthy — useful immediately in logs/diagnostics, and available
for future gating logic without another refactor.
**Disadvantage:** coverage is exposed but currently unused by any decision
logic — on its own this changes no BUY/SELL outcomes yet. Real value is
realized only once (if) a caller decides to act on it.

---

## Testing performed

- `flake8 --config=.flake8` on all 5 changed files — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check: `execution.scanner`, `risk.exit_engine`,
  `strategy.buy_scoring`, `strategy.sell_scoring` — all import cleanly.
- Full regression re-run of all prior phase test scripts (`test_phase2_buy.py`,
  `test_phase2_sell.py`, `test_fundamental_scoring.py`, `test_exit_fii_dii.py`,
  `test_tier2_factor_restructure.py`, `test_scoring_unification.py`) — all
  pass, no regressions from either fix.
- Manual smoke test: BULL market_score=75 → SELL `checks["market_score"]`
  now `False` (was incorrectly `True`), `sell_score.market` now `25.0` (was
  `75.0`); BEAR market_score=25 → `checks["market_score"]` now `True`,
  `sell_score.market` now `75.0`.

## Files changed

- `strategy/buy_strategy.py`
- `strategy/sell_strategy.py`
- `strategy/sell_scoring.py`
- `strategy/fundamental_scoring.py`
- `execution/scanner.py`
