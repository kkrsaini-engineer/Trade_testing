# PHASE 16 — Remove volatility_state / market_regime duplicate votes from _risk_score()

Implemented on explicit request, following a deep-dive audit ("aisa koi
aur indicator to nahi jo ichimoku ki tarah behave kar raha ho?") that
traced every input of `_risk_score()` (the second hidden path discovered
while removing Ichimoku in Phase 15) against `strategy/buy_strategy.py`/
`sell_strategy.py`'s live checks.

---

## What the audit found, ranked by severity

1. **`volatility_state` (ATR HIGH/NORMAL/LOW) — exact duplicate, BUY
   only.** `checks["volatility"]` in `VOLATILITY_CORE` (live, part of
   `tier2_score`) and `_risk_score()`'s `volatility_state == "HIGH"`
   penalty (`-20`, live, part of `result.risk` -> `result.overall` +
   separately weighted again in `BuyProbabilityEngine`'s confidence/
   expected_drawdown/risk_level) were testing the exact same field.
   SELL's `_risk_score()` never had a `volatility_state` line — this
   duplicate was BUY-only.
2. **`market_regime` (NIFTY BULL/BEAR) — exact duplicate, both
   directions.** `checks["market_trend"]` in Tier1's hard 2-of-3 gate
   and `_risk_score()`'s `market_regime == "BEAR"`(BUY)/`"BULL"`(SELL)
   penalty (`-20`/`-25`) tested the same field through two independent
   live paths.
3. **`gap_down`/`gap_up` — flagged, NOT fixed this phase.** Dead in
   `strategy/*_strategy.py` (`checks["gap_filter"]` never reaches
   `tier2_score`) but the single largest individual penalty in
   `_risk_score()` (`-30`/`-25`). Not a duplicate (one side is inert),
   so left as-is per the "fix karo" scope discussed — still open if you
   want it addressed (either drop it from `_risk_score()` too, or wire
   `checks["gap_filter"]` into a real factor list).
4. **`rsi_14` / `volume vs volume_sma_20` — reviewed, NOT duplicates.**
   Same source field, but genuinely different information: the
   strategy-file checks vote on a "healthy zone" (RSI 55-70 BUY /
   30-45 SELL), `_risk_score()` flags a different, extreme-tail
   condition (RSI>80 BUY / <20 SELL). Same design principle as Phase
   14's MFI trapezoid — left unchanged, not a bug.

## Fix applied

Removed the `volatility_state` and `market_regime` lines from
`strategy/buy_scoring.py`'s `_risk_score()`, and the `market_regime` line
from `strategy/sell_scoring.py`'s `_risk_score()` (no `volatility_state`
line existed there to remove). Each signal now has exactly ONE live vote
(Tier1's `market_trend` / `VOLATILITY_CORE`'s `volatility`) instead of
two independent ones silently stacking.

**REAL BEHAVIOR CHANGE — flagged explicitly, not buried:**
`_risk_score()`'s worst-case floor moved from **0 to 55 (BUY)** and
**0 to 60 (SELL)** — verified directly (see Testing). With only 3 real
remaining penalty inputs (`gap_down`/`gap_up`, extreme RSI, below-average
volume), the maximum possible deduction is 45 (BUY) / 40 (SELL), not 100.
**Practical consequence:** `BuyProbabilityEngine.risk_level()`/
`SellProbabilityEngine.risk_level()`'s `"EXTREME"` classification
(`score.risk < 50`) is now mathematically unreachable — not a bug, a
direct consequence of two of six inputs having been spurious duplicates
inflating apparent riskiness. **Not rescaled/recalibrated in this phase**
— that would mean inventing new penalty magnitudes without evidence,
the same caution applied in Phase 11's overextension recalibration (which
WAS backed by a concrete failing test; this isn't). Flagging for your
call: leave as-is (arguably more honest — the score no longer double-counts
itself into false extremity), or ask for a rescale to restore the full
0-100 range using the 3 remaining factors.

`expected_drawdown`/`reward_risk` (downstream of `result.risk`) do **not**
currently drive real position sizing — `risk/position_sizing.py` already
has `KELLY_CALIBRATED = False` from an earlier fix, so `kelly_fraction`
is a fixed constant, not scaled by this. This bug's reach is bounded to
the qualify-score/probability/diagnostic layer, not capital allocation.

---

## Wiring verification

Confirmed directly via `_risk_score()` calls (not inference):
- A row with `volatility_state="HIGH"` and `market_regime="BEAR"`/`"BULL"`
  set, everything else clean, now returns `100.0` (previously would have
  returned `60.0`/`50.0`) — proving these two fields no longer move
  `result.risk` on their own.
- A full worst-case row (all old conditions true) returns exactly `55.0`
  (BUY) / `60.0` (SELL), matching the new max-deduction arithmetic by
  hand.
- `checks["volatility"]`/`checks["market_trend"]` in
  `strategy/buy_strategy.py`/`sell_strategy.py` are untouched — each
  signal's one remaining vote is confirmed still live via the existing
  Phase-15 regression run (Tier1/VOLATILITY_CORE numbers unchanged from
  before this phase).

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_scoring.py`,
  `strategy/sell_scoring.py` — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Full regression re-run of all 6 prior phase test scripts — all pass.
- New targeted test (this phase): direct `_risk_score()` calls proving
  (a) `volatility_state`/`market_regime` alone no longer move the score,
  (b) the new worst-case floor is exactly 55 (BUY) / 60 (SELL), matching
  hand-calculated arithmetic.

## Files changed

- `strategy/buy_scoring.py`
- `strategy/sell_scoring.py`

## Still open (not fixed, flagged for your decision)

- `gap_down`/`gap_up`: dead in strategy files, heaviest single penalty in
  `_risk_score()` — inconsistent, not yet resolved either direction.
- Whether to rescale `_risk_score()`'s remaining 3 factors so `EXTREME`
  is reachable again, or leave the new 55/60 floor as-is.
