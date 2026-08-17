# Phase 25 — Real Per-Stock Circuit-Lock Detection (DONE)

Follow-up to PHASE24_NOTES.md's Part 3 ("real per-stock circuit data —
blocked, no broker API"). You pushed back on that conclusion — asked
whether bhavcopy actually carries this data, and proposed your own
heuristic (stock hits its daily high -> price can't move further ->
that's a circuit lock). Research confirmed your instinct was directionally
right, even though the specific data source (bhavcopy) doesn't carry it.
This phase implements it.

## What the research found (correcting my earlier claim)

My Phase 23/24 claim was "no free NSE data source has circuit info,
needs a broker API." That was too pessimistic — re-checked properly:

- **Bhavcopy (both current UDiFF format and legacy `sec_bhavdata_full.csv`)
  genuinely has NO circuit-limit column at all.** That part of the
  earlier claim was correct.
- **BUT NSE separately publishes a free, public file**:
  `https://nsearchives.nseindia.com/content/equities/sec_list.csv`
  ("Complete List of Price Bands") — columns `Symbol, Series, Security
  Name, Band, Remarks`, where `Band` is the symbol's assigned circuit
  percentage (2/5/10/20). No login, no API key, no broker account
  needed — same free-download pattern already used for delivery data
  and FII/DII in this codebase.
- This file is a **coarse, occasionally-updated snapshot list**, NOT a
  daily circuit-hit report — it tells you a stock's assigned BAND, not
  whether it actually got locked today.
- **Your heuristic is real and community-validated**: closing at the
  session's high (for an up-move) or low (for a down-move), combined
  with the day's % move landing near a known circuit-band percentage,
  is the exact logic a public TradingView "Circuit Marker" indicator
  uses. It's inference from price shape, not ground truth — but it's a
  legitimate, well-known technique, not something invented for this
  codebase.
- F&O-active stocks don't have a fixed round-number band at all
  (dynamic bands: 10% initial, widening in stages up to 30%) — the
  heuristic is less reliable for those, flagged in the code, not
  silently ignored.

## What was built

### `market/circuit_bands.py` (new)

- `CircuitBandsProvider.fetch_bands()` — fetches `sec_list.csv`,
  returns `{SYMBOL: band_percent}`. Same pattern as
  `data/fii_dii_data.py`: `requests.Session()` + NSE warm-up GET,
  2-attempt retry, disk-cache fallback (`storage/reports/
  circuit_bands_cache.json`) on total failure, **empty dict — never a
  fabricated default band — if genuinely nothing is available.**
- `detect_circuit_lock(open, high, low, close, prev_close, band_percent,
  tolerance_percent)` — the pure heuristic function (your proposed
  logic): flags `circuit_likely=True` when (a) the day's % move is
  within `tolerance_percent` (default 0.75) of a known band, AND (b)
  the close sits at that day's high (up-move) or low (down-move). Also
  reports `full_day_freeze` (true textbook lock:
  open==high==low==close) vs. a partial/intraday touch, `direction`,
  `move_percent`, and which band matched. Never raises, never guesses
  on missing input — returns the same "not detected" result a
  genuinely-normal day would produce.
- Uses the symbol's real assigned band from `sec_list.csv` when known;
  falls back to checking against all 4 standard bands (2/5/10/20) when
  the symbol isn't in that list (e.g. cache/fetch failed, or a newly
  listed stock).

### `execution/scanner.py` (modified)

- `_get_circuit_bands()` — lazy-fetch-once-per-scan-run, same shape as
  the existing `_get_fii_dii_data()`/`_get_delivery_data()` caches.
  Respects `_disable_live_market_context` (short-circuits to `{}` during
  backtesting — never leaks a live-fetched-today list into a
  historical replay).
- `_detect_circuit_lock(symbol, dataframe)` — pulls the last two OHLC
  rows, looks up this symbol's band, calls the pure function above.
- Both are now called from `_evaluate_market_context()` — the single
  shared method both `scan_symbol()` (new entries) and
  `evaluate_position()` (monitoring existing positions) already funnel
  through — so entry and monitoring see the identical circuit
  diagnosis for a symbol on a given run, not two independent
  computations that could disagree. Result surfaced into `diagnostics`
  as `circuit_likely` / `circuit_direction` / `circuit_full_day_freeze`
  / `circuit_move_percent` / `circuit_band_used`, for report/audit
  visibility.
- **Wiring choice**: `market_state` (the dict passed into
  `validation.validate()` / `risk.evaluate()`) is the SAME shared
  object reused across every symbol in a scan run — it can't carry a
  per-symbol fact directly without corrupting every other symbol's
  view. Both `scan_symbol()` and `evaluate_position()` now build a
  **per-symbol shallow copy** (`market_state_for_symbol = dict
  (market_state); market_state_for_symbol["circuit_breaker"] =
  diagnostics["circuit_likely"]`) right before those two calls, instead
  of touching the shared dict. `risk_manager.py`'s and
  `validation_engine.py`'s read side (`market.get("circuit_breaker")` /
  `market_state.get("circuit_breaker")`) needed NO changes — Phase 22
  already fixed them to read from this dict; they simply had no real
  producer until now. Updated the stale Phase-22 comments in both
  files that said "no producer currently sets this" — that's no longer
  true.

## What this does NOT do (scope, honestly stated)

- **Not real-time / not ground truth.** This is a lagging, end-of-bar
  heuristic computed from the same daily OHLCV already fetched via
  `yfinance` — it cannot tell you a stock is circuit-locked *right now*
  intraday, only "yesterday's/today's completed bar looks like it
  probably was."
- **Under-detects touch-and-bounce.** A stock that hit the upper
  circuit intraday and traded back off before the close won't be
  flagged (correctly, per the heuristic's own logic — it only claims
  detection when the close is AT the extreme).
- **Less reliable for F&O-active stocks** (dynamic bands, not the fixed
  2/5/10/20 list) — `sec_list.csv`'s `Band` column is understood to
  only be meaningful for non-F&O names; an F&O stock simply won't be in
  the returned band lookup in most cases, so the heuristic falls back
  to standard-band matching for it, which is weaker.
- **`sec_list.csv` itself is a coarse/occasionally-updated snapshot** —
  band reassignments (NSE does periodically re-tier stocks) may lag
  what's actually in effect on a given day.

None of this was hidden or silently smoothed over — it's the same
"heuristic, not guaranteed" framing the code's own docstrings/comments
carry, so anyone reading a `circuit_likely=True` in a report knows
exactly how much to trust it.

## Files changed

- **`market/circuit_bands.py`** (new) — `CircuitBandsProvider`,
  `detect_circuit_lock()`.
- **`execution/scanner.py`** — import, constructor state,
  `_get_circuit_bands()`, `_detect_circuit_lock()`, called from
  `_evaluate_market_context()`; per-symbol `market_state` copy wired
  into both `scan_symbol()` and `evaluate_position()`.
- **`risk/risk_manager.py`**, **`decision/validation_engine.py`** —
  comment-only update (their Phase-22 "no producer sets this yet"
  comment corrected; read logic itself is unchanged, it was already
  correct).
- **`tests/test_circuit_bands.py`** (new, 24 tests) — pure-function
  coverage of `detect_circuit_lock()` (upper/lower circuit, full-day
  freeze vs. partial touch, no-match cases, `band_percent=None`
  fallback vs. an explicit band, missing/invalid inputs, zero-division
  guard) plus `CircuitBandsProvider._parse_csv()` (valid rows,
  malformed header, malformed rows, empty input) and the disk-cache
  round-trip / network-failure-falls-back-to-cache path (network
  itself mocked — this sandbox has no outbound network access at all,
  confirmed separately, not NSE-specific).
- **`tests/test_scanner_circuit_lock.py`** (new, 9 tests) — scanner-level
  wiring: `_get_circuit_bands()` lazy-caches and respects
  `_disable_live_market_context`; `_detect_circuit_lock()` correctly
  strips the `.NS` suffix for the band lookup, mirrors BUY-side
  (upper-circuit up-move) and SELL-side (lower-circuit down-move),
  falls back to standard bands for an unknown symbol, returns the safe
  default in backtest mode and with insufficient history.

## Verification

```
python3 -m pytest tests/ -q                    # 130 passed (97 prior + 33 new)
python3 -m flake8 --config=.flake8 .            # whole repo, clean
```

## Still open (unchanged from PHASE24_NOTES.md, not touched this phase)

- Point 11 (correlated risk-factor double-counting redesign)
- Point 15 (explicit `position_state` state machine)
- Point 16 (30-day flat time-exit -> thesis-decay-based)
- `execution/broker.py`'s remaining ~400 lines of dead simulation code
  (revive vs. delete — a separate product decision)
- `orchestrator.py`'s hardcoded `risk_snapshot` (`WiredOrchestrator` has
  no live caller — still needs your sign-off per PHASE24_NOTES.md before
  spending effort on it)
