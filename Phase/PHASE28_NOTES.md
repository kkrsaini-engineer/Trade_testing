# Phase 28 — Point 16: 30-Day Flat Time-Exit → Thesis-Decay-Based (DONE)

Follow-up to PHASE26_NOTES.md / PHASE27_NOTES.md's "Still open" list.
You said "16 suru kro (make sure all wired)" — start Point 16, with an
explicit emphasis on genuine live wiring, not another dead-input
situation.

## What was wrong

`risk/exit_strategy.py`'s TIME EXIT check was a flat rule:

```python
time_exit = holding_days >= self.MAX_HOLD_DAYS  # 30
```

Any position held 30 days got force-PARTIAL_EXIT'd (50%) regardless of
whether the original trade thesis was still valid — a genuinely strong,
still-working trade gets cut for no reason other than the calendar, and
a thesis that decayed on day 10 keeps riding until day 30 regardless.

## The blocker this ran into (and the workaround)

The obvious fix — "compare today's confidence to the confidence at
entry" — hits a dead-input wall: `scripts/morning_executor.py` (the
REAL live entry path — confirmed in Phase 26 that this, not
`daily_scan.py`, is what actually opens positions) hardcodes
`open_trade()`'s `buy_probability`/`buy_confidence` to `0.0`. Its own
docstring says why: it deliberately skips a full re-scan at entry
("the full RiskManager isn't called here: it needs ATR/volume/spread/
market-regime/VIX inputs that don't genuinely exist yet this morning
without a fresh scan"). Using that field as a decay baseline would be
exactly the "fabricated neutral default" bug this whole review keeps
catching — every position would show 100% decay from day one.

**Workaround** (confirmed with you): capture the position's
held-direction confidence on its FIRST monitoring cycle instead — this
value genuinely IS freshly computed by `execution/scanner.py`'s real
scan every cycle (`result.diagnostics["buy_decision_confidence"]` /
`["sell_decision_confidence"]`, already flowing into the diary's
`daily_log` before this phase). That first real value becomes the
permanent baseline for that trade.

## Confirmed parameters (all explicit, all yours)

- **MAX_HOLD_DAYS = 30** (unchanged) — decay-checking only STARTS here;
  a big confidence drop on day 10 still doesn't force an early exit.
- **THESIS_DECAY_THRESHOLD = 20** percentage points — once past day 30,
  an exit only forces if held-direction confidence has dropped 20+
  points from the baseline.
- **HARD_CEILING_DAYS = 150** — absolute safety net, forces the exit
  regardless of thesis strength once a position has been held this
  long (so a thesis that never meaningfully decays can't hold a
  position open forever).
- **Backward compatibility (my implementation decision — this specific
  question got dropped by an AskUserQuestion validation error earlier
  and never got re-asked)**: a position with NO captured baseline
  (predates this fix, or its first monitoring cycle hasn't run yet)
  falls back to the OLD flat `holding_days >= MAX_HOLD_DAYS` rule —
  running a thesis-strength check with no thesis data to check against
  would be meaningless, and silently never forcing an exit would leave
  a stale pre-existing position open forever.

## What was fixed — exact wiring

**`storage/trades/trade_diary.py`** — new `entry_thesis_confidence`
field (always `None` at `open_trade()` time — see the blocker above)
and a new method:

```python
def capture_thesis_baseline(self, trade_id, confidence):
    if confidence is None:
        return
    record = self._read(trade_id)
    if record is None:
        return
    if record.get("entry_thesis_confidence") is not None:
        return
    record["entry_thesis_confidence"] = confidence
    ...
```

Idempotent by design: safe to call on EVERY monitoring cycle (no
separate "is this day 1?" tracking needed elsewhere) — it only ever
writes once, on the first cycle a real (non-`None`) value happens to be
available, and a transient gap doesn't permanently block capture on a
later cycle.

**`paper_trading/paper_trading_engine.py`** — in the monitoring loop,
right after `trade_id`/`holding_days` are resolved:

```python
held_thesis_confidence = (
    result.diagnostics.get("buy_decision_confidence")
    if pos.direction == "BUY"
    else result.diagnostics.get("sell_decision_confidence")
)
if trade_id is not None:
    self.diary.capture_thesis_baseline(trade_id, held_thesis_confidence)
    diary_record = self.diary.get_diary(trade_id)
entry_thesis_confidence = diary_record.get("entry_thesis_confidence") if diary_record else None
```

Both values are then injected into `position_input` (the dict passed
to `ExitStrategyEngine.evaluate()`) as `entry_thesis_confidence` /
`held_thesis_confidence`. Correctly mirrors BUY and SELL by reading
whichever diagnostics field matches `pos.direction` — same
held-direction precedent as `held_decision` a few lines below it.

**`risk/exit_strategy.py`** — new class constants
`THESIS_DECAY_THRESHOLD = 20.0`, `HARD_CEILING_DAYS = 150`
(`MAX_HOLD_DAYS = 30` unchanged). The TIME EXIT section now reads:

```python
entry_thesis_confidence = position.get("entry_thesis_confidence")
held_thesis_confidence = position.get("held_thesis_confidence")

has_baseline = entry_thesis_confidence is not None and held_thesis_confidence is not None
thesis_decay_points = (entry_thesis_confidence - held_thesis_confidence) if has_baseline else None
thesis_decayed = has_baseline and thesis_decay_points >= self.THESIS_DECAY_THRESHOLD
hard_ceiling_reached = holding_days >= self.HARD_CEILING_DAYS

if has_baseline:
    time_exit = (holding_days >= self.MAX_HOLD_DAYS and thesis_decayed) or hard_ceiling_reached
else:
    time_exit = holding_days >= self.MAX_HOLD_DAYS or hard_ceiling_reached
```

New diagnostics for audit trail: `thesis_decay_points`,
`thesis_decayed`, `hard_ceiling_reached` (`time_exit` itself is
unchanged as a key, so `_classify_exit_trigger()`'s existing
"Time-Based Exit" label logic — which reads the `time_exit` diagnostics
flag directly, not the reason string — needed NO changes). The
`exit_reason` text now distinguishes all three cases: "Maximum holding
period." (old flat fallback), "Thesis decayed beyond threshold.", and
"Hard ceiling reached (150 days held)." — same distinction is threaded
into `reasons[]` for the diary's `daily_log[].notes`.

This engine logic itself is fully direction-agnostic (works identically
for BUY/SELL positions); the BUY/SELL-specific part is
`paper_trading_engine.py` correctly picking `buy_decision_confidence`
vs. `sell_decision_confidence` based on `pos.direction`, tested both
ways below.

## Still-open related gap (flagged, NOT fixed — separate from this phase)

`scripts/generate_full_report.py`'s `candidates_order.json` carries a
real `overall_score`/`ranking` per candidate, but
`scripts/morning_executor.py` doesn't currently read/propagate either
into the diary at entry. That's a separate gap from the one this phase
fixes (it's about score-at-entry bookkeeping, not the decay-comparison
mechanism) — noted here for visibility, not bundled in, since you
didn't ask for it and it wasn't part of Point 16's scope.

## Files changed

- **`storage/trades/trade_diary.py`** — `entry_thesis_confidence` field
  on `open_trade()`'s record, new `capture_thesis_baseline()` method.
- **`paper_trading/paper_trading_engine.py`** — captures the baseline
  each cycle (idempotent), wires both baseline + today's confidence
  into `position_input`.
- **`risk/exit_strategy.py`** — `THESIS_DECAY_THRESHOLD`,
  `HARD_CEILING_DAYS` constants; decay-aware TIME EXIT logic in both
  the diagnostics-computation section and the EXIT PRIORITY ENGINE
  cascade's `time_exit` branch.
- **`tests/test_trade_diary.py`** (new, 6 tests) — `capture_thesis_
  baseline()` in isolation: first-write, idempotency, `None`-skip,
  unknown trade_id, BUY/SELL.
- **`tests/test_exit_strategy.py`** (+8 tests) — pure-engine coverage:
  no-baseline fallback (BUY+SELL), decay-below-threshold holds past day
  30, decay-at-threshold forces exit (BUY+SELL), decay ignored before
  day 30, hard ceiling forces exit even with zero decay (BUY), hard
  ceiling forces exit even with no baseline (SELL).
- **`tests/test_paper_trading_exit_wiring.py`** (+5 tests) — real
  end-to-end wiring through `PaperTradingEngine.run_cycle()`: baseline
  captured on cycle 1 (BUY+SELL), NOT overwritten by a later cycle's
  lower confidence, a genuine 31-cycle run where decay past day 30
  forces a real `PARTIAL_EXIT` (confirmed via the diary's own
  `daily_log[-1].notes`), and the mirror case where decay stays under
  threshold and the position correctly stays open.

## Verification

```
python3 -m pytest tests/ -q                    # 187 passed (168 prior + 19 new)
python3 -m flake8 --config=.flake8 .            # whole repo, clean
```

## Still open (unchanged from PHASE26/27_NOTES.md)

- Point 15 (explicit `position_state` state machine) — recommended to
  stay its own isolated phase.
- `execution/broker.py`'s ~400 lines of dead simulation code — blocked
  on your revive-vs-delete decision.
- `orchestrator.py`'s hardcoded `risk_snapshot` — blocked on either a
  real volatility/liquidity_risk data source or your confirmation to
  leave it hardcoded/documented.
- (New, this phase) `scripts/morning_executor.py` not propagating
  `candidates_order.json`'s real `overall_score`/`ranking` into the
  diary at entry — flagged above, not scoped/fixed yet.
