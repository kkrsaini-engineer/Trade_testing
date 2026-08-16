# PHASE 11 — Fix #12 (ATR-normalized overextension), #15 (state narrative)

Implemented on explicit request ("now 12,15 and check wired afte fix"),
with fact-checking against actual code before implementing.

---

## Fix #12 — overextension cap, ATR-normalized instead of a flat percentage

**Verdict confirmed, exact match:** `OVEREXTENSION_CAP_PERCENT = 8.0` was a
single fixed value applied identically to every stock regardless of its
own volatility — a low-volatility stock 8% above EMA20 and a high-
volatility stock 8% above EMA20 are not equally extended, but the old
check couldn't tell them apart.

**Fix applied:** the cap is now `atr_percent (ATR as % of price) *
OVEREXTENSION_ATR_MULTIPLE`, floored and ceilinged to a sane range so
neither a near-zero-ATR stock (unreasonably tight cap) nor a very-high-
ATR/penny/junk stock (unreasonably loose cap) breaks the check:

```python
OVEREXTENSION_ATR_MULTIPLE = 4.0
OVEREXTENSION_CAP_FLOOR_PERCENT = 5.0
OVEREXTENSION_CAP_CEILING_PERCENT = 20.0

atr_percent = (atr_14 / close) * 100
overextension_cap_percent = min(max(atr_percent * 4.0, 5.0), 20.0)
checks["not_overextended"] = extension_percent <= overextension_cap_percent
```

**Calibration, not an arbitrary number:** the multiplier (4.0) was chosen
so a "typical" NSE swing-trade candidate (roughly 2% ATR-as-percent-of-
price) lands close to the OLD flat 8% cap (2% × 4.0 = 8%) — this is a
genuine ATR-normalization, not a blanket tightening. A caught-and-corrected
mistake during implementation: an initial multiplier of 2.5 (floor 4%,
ceiling 15%) silently tightened the cap for typical-ATR stocks well below
the old 8% baseline — caught by a real regression-suite failure
(`test_phase2_buy.py`'s "Scenario5 normal BUY" started failing, a
previously-passing 5%-above-EMA20 setup at ~1.9% ATR got rejected) rather
than by inspection alone. Recalibrated before delivery so a stock at
roughly-typical ATR sees close to no change, a lower-than-typical-ATR
stock now correctly gets a tighter cap than 8%, and a higher-than-typical-
ATR stock correctly gets a wider one.

**Verified numerically, not just "doesn't crash":** two synthetic rows
with the identical 6% extension-from-EMA20 but very different ATR (~0.47%
vs ~5.7%) produce opposite `not_overextended` outcomes (low-ATR → rejected,
high-ATR → passes) — confirms the cap genuinely varies with volatility,
not a no-op.

**SCOPE NOTE (explicit, matches the critique's own framing):** the fuller
"Entry Distance Model" the critique describes also wants breakout-age
(days-since-signal) and volume-decay-after-breakout tracking — both need
NEW persisted per-symbol state across scan days, which doesn't exist
anywhere in this codebase today (confirmed via search) and is a materially
bigger, separate scope item — the same class of work as fix #5 (late-entry
architecture). This fix is deliberately scoped to the ATR-normalization
part only, which needs no new state (`atr_14` is already computed per
row) — **not** attempted here, not silently dropped.

**Advantage:** the hard-reject filter now reflects each stock's own
volatility instead of one number for the whole watchlist — a genuinely
better filter, not just a cosmetic change. **Disadvantage:** the
multiplier/floor/ceiling are still a judgment call (no historical
calibration data was available in this sandbox to backtest-optimize them);
they're documented and easy to retune later if paper-trading data suggests
otherwise.

---

## Fix #15 — state-based narrative (presentation layer, not a replacement)

**Verdict confirmed as scoped:** the pasted critique's own verdict draws
exactly the line this fix follows — a readable narrative built ON TOP of
existing checks is low-risk; REPLACING scoring with hard state-transition
rules is the same scope as fix #5. This fix implements only the former.

**Fix applied:** `BuyDecision`/`SellDecision` gained a `state_narrative:
str` field, computed from checks/scores already calculated by
`evaluate()` — no new inputs, no new state, nothing persisted across
scans:

- **MarketState** — `row["market_regime"]` (`BULL`/`BEAR`/`SIDEWAYS`),
  already computed.
- **TrendState** — `UPTREND`/`DOWNTREND`/`RANGE`, derived from
  `adx_regime` (already computed by fix #4's Tier2 restructuring) and
  the `price_above_ema20`/`price_below_ema20` check.
- **SetupState** — `SQUEEZE_BREAKOUT`/`BREAKOUT`/`PULLBACK`/`NONE` (SELL:
  `SQUEEZE_BREAKDOWN`/`BREAKDOWN`), derived from which specific entry-type
  check fired, in priority order.
- **EntryState** — `REJECTED_TREND` (Tier 1 failed) /
  `REJECTED_OVEREXTENDED` (fix #12's hard-reject fired) / `TRIGGERED`
  (qualified, action is BUY/SELL) / `WAITING` (Tier 1 passed, not
  overextended, but `overall_score` below `QUALIFY_THRESHOLD`).

Example: `"MarketState=BULL / TrendState=UPTREND / SetupState=PULLBACK /
EntryState=WAITING"`.

**Does NOT change `qualified`/`action`/`overall_score`/any scoring —**
those are computed exactly as before, from the same checks this narrative
just re-describes in words. Verified this is true by construction (the
narrative is computed strictly after `qualified`/`action` are already
final) and by the full regression suite passing unchanged (aside from the
expected fix #12 numeric shifts) on top of this addition.

**Wired where it's actually seen:** appended to `reasons` (visible in
per-symbol audit trails), included in the existing `logger.info(...)` line
(visible in GitHub Actions run logs — the exact place this conversation's
Phase 3/4 logging improvements have been optimizing for), and exposed via
`execution/scanner.py`'s diagnostics dict as `buy_state_narrative`/
`sell_state_narrative`. **Not** added as a new CSV column in
`generate_full_report.py` — that `FIELDNAMES` list is explicitly described
in-repo as "exact column order the person asked for," and this fix doesn't
touch it.

**Advantage:** a human scanning GitHub Actions logs or diagnostics gets an
immediate, readable summary ("why didn't this trigger?") instead of only
`"32/40"` / `"86.55/100"` — genuinely more explainable, in exactly the
place this conversation's earlier logging work has been aimed at.
**Disadvantage:** purely additive — no real downside, but also no
functional effect on trading decisions; value is explainability only, as
scoped.

---

## Wiring verification (both)

- **#12**: the ATR-normalized cap lives directly inside
  `BuyStrategyEngine.evaluate()`/`SellStrategyEngine.evaluate()`'s
  `checks["not_overextended"]` computation — the same hard-reject gate
  already wired into `qualified` (`tier1_passed and overall_score >=
  QUALIFY_THRESHOLD and checks["not_overextended"]`), which
  `execution/scanner.py:447/454` calls on every real scan. Confirmed via
  a live `evaluate()` call showing the SAME 6% extension producing
  opposite pass/reject outcomes purely based on ATR — not a unit-level
  check on isolated math.
- **#15**: `state_narrative` is set on the `BuyDecision`/`SellDecision`
  object returned by `evaluate()`, and `execution/scanner.py:483/494`
  (`diagnostics["buy_state_narrative"] = buy_decision.state_narrative` /
  `sell_state_narrative` mirror) reads it directly off that same
  live-path return value — confirmed via source grep showing both
  diagnostics lines present and reading the correct attribute.

## Testing performed

- `flake8 --config=.flake8` on `strategy/buy_strategy.py`,
  `strategy/sell_strategy.py`, `execution/scanner.py` — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check — clean.
- Full regression re-run of all 6 prior phase test scripts — all pass. One
  genuine regression surfaced and was fixed during this phase (see fix
  #12's calibration section above) — not silently worked around.
- New targeted smoke tests (this phase): same-extension-percent,
  different-ATR scenario producing opposite `not_overextended` outcomes
  (confirms the ATR-normalization is real, not a no-op); `state_narrative`
  presence/structure check confirming all 4 components are populated on a
  live `evaluate()` return value.

## Files changed

- `strategy/buy_strategy.py` (#12, #15)
- `strategy/sell_strategy.py` (#12, #15)
- `execution/scanner.py` (#15 diagnostics wiring)
