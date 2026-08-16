# PHASE 18 — Real liquidity proxy from already-fetched bhavcopy data +
rolling 20-day history

Implemented on explicit request ("pehle ye sab implement or wired kro")
covering the full liquidity thread: extending `data/delivery_data.py`
to stop discarding `NO_OF_TRADES`/`TURNOVER_LACS`/etc., building a
genuine rolling multi-day history (not just a single day's snapshot),
and rebuilding `_liquidity_score()` to use it.

---

## Part 1: `data/delivery_data.py` — stop discarding columns

The bhavcopy CSV NSE already sends every day (`sec_bhavdata_full_*.csv`)
was only ever parsed for `SYMBOL`, `SERIES`, `DELIV_PER`. The same row
also carries `TTL_TRD_QNTY`, `NO_OF_TRADES`, `TURNOVER_LACS`,
`CLOSE_PRICE`, `PREV_CLOSE` — these were fetched and thrown away.

**No new network call, no new data source, no cost.** `fetch_latest()`
now returns `{symbol: {field: value}}` instead of `{symbol: float}`.
Each field is independently optional (missing/`-`/unparsable →
omitted, never fabricated) — a row with `DELIV_PER` present but
`NO_OF_TRADES` missing still returns `delivery_percent` normally.

Cache file (`storage/reports/delivery_data_cache.json`) format changed
accordingly. Backward compatibility: `_read_cache()` detects an
old-format cache (bare floats, from before this phase) and wraps each
value into `{"delivery_percent": value}` instead of crashing or
silently dropping it.

---

## Part 2: `data/liquidity_history.py` — new module, rolling window

Answers the question asked directly: **"ye ek din k data se rolling
average kaise niklega?"**

Mechanism — exactly as described before implementing, now real code:

1. Every scan run, today's bhavcopy fields for a symbol get appended to
   `storage/reports/liquidity_history.json` (`append_and_prune()`).
2. Entries older than `WINDOW_DAYS` (20, matches `volume_sma_20`/OBV/CMF's
   own window — not a new arbitrary number) are pruned.
3. `rolling_liquidity_stats()` computes the window average of
   `avg_trade_size` (`ttl_trd_qnty / no_of_trades`) and Amihud (2002)
   illiquidity (`|pct_change| / turnover_lacs`).
4. Persisted back to the repo by `daily_scan.yml`'s existing
   `git add -A -- ... storage/reports/` commit step — no workflow
   change needed, that mechanism already existed for
   `delivery_data_cache.json`.

**Accuracy — stated plainly, not hidden:**

- **Cold start**: `rolling_liquidity_stats()` returns `None` (not a
  guess) below `MIN_HISTORY_DAYS = 5` entries. Below that, the two new
  scoring components are simply absent — see Part 3.
- **Newly-added symbols** restart cold — same guard applies per-symbol,
  independent of how long the feature has existed overall.
- **Missed days** (holiday/fetch failure) are never gap-filled or
  carry-forwarded — that day just doesn't get an entry, same
  "omit, don't fabricate" convention as everywhere else in this review.
- **Re-run safety**: appending twice for the same date replaces, not
  duplicates, that date's entry (verified in the targeted test).
- **Repo growth**: the history file is capped at `WINDOW_DAYS` entries
  per symbol (old entries pruned every run), so its size stays bounded
  — it does not grow unboundedly with the repo's git history the way an
  uncapped log would.

---

## Part 3: `_liquidity_score()` rebuild (buy_scoring.py + sell_scoring.py)

Old: a single volume-vs-own-20d-average step function (35-100).

New: a 3-component blend —

```
volume_component       (50% weight)  — UNCHANGED step function
trade_quality_component (30% weight) — today's avg_trade_size vs its
                                        own 20d rolling average
amihud_component        (20% weight) — today's Amihud illiquidity vs
                                        its own 20d rolling average
```

Both new components use the same shared interpolation: ratio ≤ 1.2 →
100 (no anomaly), ratio ≥ 2.0 → floor of 40 (not 0 — a single ratio
heuristic without cross-sectional confirmation shouldn't zero a stock
out entirely), linear in between.

**Exclusion + reweight, not a fabricated neutral default.** If a
symbol has no rolling history yet, the two new components are absent
entirely and `volume_component` alone carries 100% of the weight —
i.e. the score is **mathematically identical to the pre-Phase-18
behavior** until real rolling data exists for that symbol. This
mirrors `result.overall`'s existing `has_news`/`has_sector` exclusion
pattern rather than the naive alternative (defaulting a missing
component to 50.0), which would have silently pulled every cold-start
score toward the middle instead of leaving the already-validated
volume signal untouched.

**Honest side effect, flagged rather than hidden:** once rolling data
exists, an ordinary/well-behaved trading day (ratio ≈ 1.0 on both new
components) scores full marks (100) on each — same "start-at-max,
subtract only for anomalies" idiom `_risk_score()` already uses
elsewhere in this codebase — which measurably raises the blended score
above the old volume-only number even with nothing wrong (e.g. a
volume-ratio-1.0 day that used to score exactly 70 can score 85 once
confirmed normal). This is intentional — it reflects genuinely new
positive evidence (confirmed not block-deal-driven, confirmed not an
abnormal price-impact day) — but it means average liquidity scores will
trend upward over the following ~20 trading days as history fills in
for well-behaved stocks, not stay flat. Flagging this now so it isn't
mistaken for a bug later.

**BUY/SELL mirroring:** liquidity/illiquidity is not a directional
concept — a thin, block-deal-driven market is equally bad information
for an entry as for an exit. `buy_scoring.py` and `sell_scoring.py`'s
versions are identical (verified: same row → same score both sides),
same as they were before this phase.

---

## Wiring — `execution/scanner.py`

`_evaluate_market_context()`'s existing delivery-percentage section (2a)
now reads the new dict-of-dicts shape (`delivery_percentage` wiring
itself unchanged). A new section 2b:

1. Appends today's bhavcopy fields for this symbol to the in-memory
   history (loaded once per scan run via `_get_liquidity_history()`,
   same lazy-cache pattern as `_get_delivery_data()`/`_get_fii_dii_data()`).
2. Saves the history file after every symbol (not just once at the end
   of the run) — so a mid-run crash on symbol 150 of 500 doesn't lose
   the already-processed 149 symbols' updates.
3. Computes today's own trade-quality/Amihud values plus the rolling
   window average, and sets them on the dataframe's latest row
   (`avg_trade_size_today`, `avg_trade_size_20d`, `amihud_today`,
   `avg_amihud_20d`, `liquidity_window_days`) — exactly where
   `_liquidity_score(row)` reads them from.
4. Respects the existing `disable_live_market_context` flag (backtester
   opt-out) — same as delivery data and FII/DII, no new leak surface.

## Wiring verification

- `_liquidity_score()` on a cold-start row (no rolling columns) returns
  bit-for-bit the same value the old pure step function would have
  returned — proven directly in the targeted test, not just asserted.
- A block-deal-simulated row (trade-size ratio 3.0) scores measurably
  BELOW the no-data baseline; an abnormal-price-impact row (Amihud
  ratio 4.0) also scores below baseline — both components verified to
  actually move the number in the correct direction.
- NaN-safety verified: a rolling column present but `NaN` (e.g. a
  partially-populated pandas row) is treated as "missing", weight
  reweighted to the remaining components — no crash, no silent 0.
- `data/liquidity_history.py` mechanics verified directly: `None`
  returned below `MIN_HISTORY_DAYS`, window caps at exactly
  `WINDOW_DAYS` after over-appending, same-day re-run does not
  duplicate an entry.
- `DeliveryDataProvider._parse_csv()` verified against a realistic
  sample row matching the exact bhavcopy column layout shared earlier
  in this review — all 6 fields parse correctly; a row with every
  optional field as `-` is correctly excluded entirely, while a row
  with only SOME fields as `-` correctly keeps whatever did parse.
- Full regression suite re-run — all pass, no behavior outside the
  liquidity path changed.

## Testing performed

- `flake8 --config=.flake8` on all 5 changed/new files — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed.
- Full regression re-run of all 6 prior phase test scripts — all pass.
- New targeted test (this phase, 10 cases): cold-start parity, normal-day
  upward shift, block-deal penalty, Amihud penalty, BUY/SELL parity,
  NaN-safe exclusion, `liquidity_history.py`'s cold-start/window-cap/
  re-run-safety mechanics, and a realistic bhavcopy-row parse test.

## Files changed / added

- `data/delivery_data.py` (extended)
- `data/liquidity_history.py` (new)
- `execution/scanner.py` (wiring)
- `strategy/buy_scoring.py` (`_liquidity_score()` rebuilt)
- `strategy/sell_scoring.py` (`_liquidity_score()` rebuilt, mirrored)

---

---

## Part 4 (added on explicit request): one-time historical backfill

"pehle 20 days ka bhavcopy backup k liye log do jo 20 days ka data
collection kre? liquidity k liye??" — implemented as
`scripts/backfill_liquidity_history.py`, a manual/one-off script (NOT
part of `daily_scan.yml`'s cron).

**Mechanism:** `DeliveryDataProvider` gets a new `fetch_range()` method
— same NSE bhavcopy URL/session/parsing `fetch_latest()` already uses,
just called once per trading day instead of once total, walking
backwards from **yesterday** (never today — today stays
`fetch_latest()`'s job, so a backfill run can never race or double up
with the live daily scan). It skips 404s (weekends/holidays) and keeps
walking back until either `WINDOW_DAYS` (20) successful trading-day
fetches are collected, or a `max_calendar_days_back` safety cap (45) is
hit — the cap exists so a long outage/gap can't turn into an unbounded
loop. A small delay (1.5s) between requests avoids hammering NSE with
~20-45 requests in quick succession (`fetch_latest()`'s daily path only
ever makes 1).

The script itself: loads `storage/watchlist/nifty500.json`, calls
`fetch_range()`, filters each day's whole-market bhavcopy down to just
watchlist symbols (bhavcopy covers thousands of symbols; keeping only
the ~500 actually scanned keeps the persisted history file the size it
would naturally reach through normal daily scanning, not the entire
market), and appends+saves each day via the same
`liquidity_history.append_and_prune()`/`save_history()` the daily scan
path uses — saved after every day, not just once at the end, so a
crash partway through doesn't lose already-backfilled days.

**How to run it — GitHub Actions, not your laptop:** this repo's entire
runtime is GitHub Actions (see daily_scan.yml) — this script needs to
write `storage/reports/liquidity_history.json` back into the SAME repo
daily_scan.yml reads it from, so it has to run there too, not locally.
A new workflow, `.github/workflows/backfill_liquidity.yml`, is included
in this zip — manual-trigger only (`workflow_dispatch`, no schedule),
mirrors `daily_scan.yml`'s exact Python-setup/commit-push pattern
(including the same `repo-write-lock` concurrency group, so it can
never race the nightly scan's own commit).

After uploading this zip: GitHub repo → **Actions** tab → **Backfill
Liquidity History (one-time)** (left sidebar) → **Run workflow** button
→ Run workflow. Takes a few minutes (20+ NSE requests with a polite
delay between each). No local Python/laptop run needed — this is a
button click, same as everything else in this review.

Re-running it later (if ever needed) is harmless (same-date entries are
replaced, not duplicated) — it just re-spends NSE requests for no
reason.

**What this changes for cold-start:** without this script, a symbol's
rolling window takes ~20 daily scan runs (roughly a calendar month) to
fill. After running this once, the window is full from the very first
scan afterward — the accuracy caveat in Part 3 about "average liquidity
scores drift up over the following ~20 days" no longer applies once
this has been run.

**Honest limitation:** this only backfills symbols currently in the
watchlist at the time it's run. A symbol added to the watchlist later
still starts cold (same as before) unless this script is re-run.

## Wiring verification (Part 4)

- `fetch_range()` verified with a mocked NSE response simulating
  holidays (every 3rd day empty): collects exactly the requested number
  of trading days, in chronological order, never includes today's date.
- Verified it gives up cleanly (empty result, no infinite loop) when
  `max_calendar_days_back` is exhausted with zero successful fetches.
- `backfill_liquidity_history.py`'s `main()` verified end-to-end
  (mocked, no live network call): correctly filters a mixed
  in-watchlist/out-of-watchlist bhavcopy day down to only watchlist
  symbols, and correctly accumulates multiple days into history.
- `flake8`/`pytest`/full regression suite re-run after this part too —
  all still clean.

## Files changed / added (Part 4)

- `data/delivery_data.py` (added `fetch_range()`)
- `scripts/backfill_liquidity_history.py` (new)
- `.github/workflows/backfill_liquidity.yml` (new — manual-trigger-only
  GitHub Action to actually run the script; validated as syntactically
  valid YAML)

## Still open (unchanged by this phase)

The delivery%'s non-directional (BUY/SELL symmetric) credit in
`volume_factor_score` — discussed earlier, 3 options presented, none
chosen — is untouched by this phase. This phase only concerns
`_liquidity_score()` (the 5%-weight liquidity component), not
`volume_factor_score`.

---

## PHASE 18c — real bug found from actual production data (2026-08-16)

User ran the Actions backfill workflow and uploaded the resulting
`storage/reports/liquidity_history.json` for a sanity check. Analysis
of the real output found a genuine data-corruption bug — not a guess,
directly confirmed from the file:

**The bug:** NSE's bhavcopy archive endpoint returns **HTTP 200 with
the previous trading day's file** — not a 404 — for a date that has no
bhavcopy of its own. Confirmed pattern: every Saturday URL correctly
404'd (skipped, as designed), but every **Sunday** URL silently
returned the prior Friday's exact numbers under a 200 status. Since
`_fetch_for_date()` only ever checked for a 404 and otherwise trusted
whatever came back, this stale data got recorded as a brand-new
"Sunday" entry, byte-for-byte identical to the real Friday entry.

**Confirmed blast radius (from the uploaded file, before this fix):**
all 499 backfilled symbols, all 3 Sundays in the 20-day window
(2026-07-26, 2026-08-02, 2026-08-09) — 1,497 phantom entries out of
9,980 total (15% of the file). Net effect: each of the 3 Fridays in
that window was double-counted in every symbol's rolling average,
biasing `avg_trade_size_20d`/`avg_amihud_20d` toward Friday's trading
pattern instead of a true 17-distinct-day (this backfill only reached
17 genuine trading days, not the intended 20 — see below) average.

**Root-cause fix:** `_parse_csv()` now takes an `expected_date` and
verifies the bhavcopy's own `DATE1` column actually matches the date
that was requested, before accepting any of the file's data. A
mismatch is treated exactly like a 404 (empty result, caller tries an
earlier date) — never silently accepted. This fixes BOTH
`fetch_latest()` (daily scan path) and `fetch_range()` (backfill path)
at once, since both funnel through the same `_fetch_for_date()`.

**This means the 20-day backfill script will now actually need to walk
back further than 20 calendar days to find 20 REAL trading days** (since
Sundays that used to falsely "count" as successful fetches will now be
correctly rejected) — already handled by the existing
`max_calendar_days_back=45` safety margin, no code change needed there.

**Cleanup of the already-corrupted file:** the 1,497 phantom entries
were identified precisely (an entry byte-identical to the
chronologically preceding entry for the same symbol — verified as a
100%-precise detector for this specific corruption, zero false
positives checked against all 499 symbols) and removed. The cleaned
file (`liquidity_history_CLEANED.json` in this delivery) has exactly 17
genuine trading days per symbol, zero remaining duplicates. **Upload
this cleaned file to replace `storage/reports/liquidity_history.json`
in the repo** (or delete the corrupted one and commit the cleaned one
in its place), then re-run the Actions backfill workflow (now fixed) to
top up the remaining ~3 days and reach a genuine 20-day window.

## Testing performed (Phase 18c)

- `flake8 --config=.flake8` — clean.
- `pytest tests/ -q` — 4 passed.
- Full regression suite (all prior phase scripts) — all pass.
- New targeted test: reproduces the EXACT bug (a Friday's real CSV
  served under a Sunday's requested date) and proves it's now rejected;
  proves a matching date still parses normally; proves the
  `expected_date=None` backward-compatible path still works; proves the
  `DATE1` parser handles case-insensitivity and garbage input safely.
- Cleanup script verified against the real uploaded file: confirmed
  1,497 phantom entries removed, re-scanned the cleaned output and
  confirmed zero duplicate-adjacent entries remain, confirmed all 17
  remaining dates per symbol are genuine trading days (no weekends).

## Files changed (Phase 18c)

- `data/delivery_data.py` (`_parse_csv()` now validates `DATE1`; new
  `_parse_bhavcopy_date()` helper)

---

## Part 5 — a second bug, found by the user's follow-up question (2026-08-16)

User's question: "daily_scan.py chalne se pehle uske paas updated
bhavcopy honi chahiye taaki accuracy rahe?" — answering it directly
required re-checking exactly WHEN and HOW `fetch_latest()` gets its
"today" label, and that check found a real bug.

**Answer to the actual question:** bhavcopy is fetched live, DURING the
scan run itself (lazily, on the first symbol processed, then cached for
the rest of that run) — there is no separate pre-fetch step, and none
is needed for that specific concern. `daily_scan.yml` runs at 8:30 PM
IST, which is normally after NSE publishes end-of-day bhavcopy.

**The real risk, and the bug it exposed:** `fetch_latest()` already had
a walk-back fallback for when today's bhavcopy genuinely isn't
published yet (NSE can run late) — it silently uses the most recent
available day instead. That part was fine. But `fetch_latest()` never
told its caller WHICH day it actually returned — and
`execution/scanner.py`'s liquidity-history wiring (added in Part 3)
unconditionally labeled every entry with `date.today()`, regardless of
which day the data actually came from. On any day NSE's bhavcopy is
delayed, this would have silently created a phantom "today" entry
containing an EARLIER day's numbers — the exact same corruption
pattern Phase 18c's DATE1 validation was built to catch, just
introduced by this module's own caller instead of by NSE.

**Fix:** `fetch_latest()` now returns `(data, as_of_date)` instead of
just `data` — `as_of_date` is the real trading day the data is for.
`execution/scanner.py` stores this (`_delivery_data_as_of`) and:
- Surfaces it to the report: `diagnostics["bhavcopy_as_of"]` and
  `diagnostics["bhavcopy_stale"]` (true when the data isn't from
  today) — so a delayed-bhavcopy day is now VISIBLE in the output,
  not silent.
- Uses it (not `date.today()`) as the liquidity-history entry's date
  label — a delayed day now correctly REPLACES that earlier date's
  existing entry (same-date-safe, per `append_and_prune()`) instead of
  manufacturing a phantom "today" entry with old numbers.
- Only appends to liquidity history when `bhavcopy_as_of` is actually
  known (skips entirely on total fetch failure — no data, no guess).

## Testing performed (Phase 18d)

- `flake8`/`pytest`/full regression suite — all still clean.
- New targeted test (4 cases): `fetch_latest()` returns `as_of == today`
  when today's file is available; returns the REAL earlier date when
  today's is delayed (not today); returns the cache's own recorded
  date on total failure (not today); and an end-to-end scanner-wiring
  case proving a stale-bhavcopy day now labels the liquidity-history
  entry with the real date, not today.

## Files changed (Phase 18d)

- `data/delivery_data.py` (`fetch_latest()`/`_read_cache()` now return
  `(data, as_of_date)`)
- `execution/scanner.py` (stores + uses `_delivery_data_as_of`, fixes
  the `date.today()` mislabeling bug, surfaces `bhavcopy_as_of`/
  `bhavcopy_stale` in diagnostics)

---

## Part 6 — Telegram notification for bhavcopy fetch failures (user request)

User asked: notify on Telegram when bhavcopy fetch fails for the whole
day (so it's visible which day's delivery%/liquidity data wasn't
counted), plus show the last 4 days' counted/not-counted status
alongside it.

**New module: `data/bhavcopy_status_log.py`** — a tiny, purpose-built
log, separate from `liquidity_history.py` (which stores per-symbol
fetched values). This records ONE entry per scan day: whether the fetch
was `OK` (today's real bhavcopy used), `STALE` (an earlier day's
bhavcopy used — the walk-back fallback from Part 5), or `FAILED` (no
data at all — live fetch + cache both failed), plus the actual `as_of`
date and how many symbols matched. Persisted to
`storage/reports/bhavcopy_status_log.json`, same git-commit-back
mechanism as every other file in `storage/reports/`. Capped at 30
entries (re-run-safe: same-day re-runs replace, not duplicate).

**Wiring — `scripts/generate_full_report.py`** (the actual script
`daily_scan.yml` runs): right after the scan loop finishes, reads
`scanner._delivery_data`/`scanner._delivery_data_as_of` (already set
once per run by Part 5's fix — no new fetch, no extra NSE request),
classifies today as OK/STALE/FAILED, records it to the status log, and
— **only when something's actually wrong** (STALE or FAILED — a
healthy day stays silent, same as every other `notify()` call in this
file) — sends ONE Telegram message containing:
1. What happened today (FAILED: no data, fell back to volume-only
   everywhere; STALE: which earlier day's data got used instead).
2. A "Last 4 scan days" trail — OK/STALE/FAILED, the as_of date, and
   symbol-match count for each of the last 4 recorded scan days —
   directly answering "pichhle 4 din me liquidity/delivery% count
   hua ya nahi."

**Example notifications** (generated by the targeted test below, not
hand-written):

```
❌ Bhavcopy Fetch Failed — No Delivery%/Liquidity Data Today
Date: 2026-08-13
NSE bhavcopy could not be fetched (live fetch + cache both failed).
Delivery% and liquidity (trade-size/Amihud) scoring fell back to
volume-only for EVERY symbol in today's scan — not a crash, just
less-informed scoring for today.

Last 4 scan days:
✅ 2026-08-10: OK (data as of 2026-08-10, 499 symbols matched)
✅ 2026-08-11: OK (data as of 2026-08-11, 500 symbols matched)
🟡 2026-08-12: STALE (data as of 2026-08-11, 500 symbols matched)
❌ 2026-08-13: FAILED (data as of —, 0 symbols matched)
```

```
🟡 Bhavcopy Data Stale — Using an Earlier Trading Day
Date: 2026-08-12
Today's bhavcopy wasn't published yet — used 2026-08-11's data
instead for delivery%/liquidity scoring.

Last 4 scan days:
✅ 2026-08-10: OK (data as of 2026-08-10, 499 symbols matched)
✅ 2026-08-11: OK (data as of 2026-08-11, 500 symbols matched)
🟡 2026-08-12: STALE (data as of 2026-08-11, 500 symbols matched)
❌ 2026-08-13: FAILED (data as of —, 0 symbols matched)
```

Uses the codebase's existing shared `core/notifications.py` `notify()`
helper — same Telegram bot/chat already configured for
`daily_scan_started`/`daily_scan_completed`, no new setup needed.
Severity: `SEVERITY_HIGH` for FAILED, `SEVERITY_MEDIUM` for STALE.

## Testing performed (Phase 18e)

- `flake8`/`pytest`/full regression suite — all still clean.
- New targeted test (isolated from the real status-log file): confirms
  `record_status()`/`recent_entries()` mechanics, same-day re-run
  replaces (not duplicates), log pruning caps at `MAX_LOG_ENTRIES`, and
  generates the two example notifications shown above from the actual
  formatting code (not hand-typed examples).

## Files changed / added (Phase 18e)

- `data/bhavcopy_status_log.py` (new)
- `scripts/generate_full_report.py` (records status + sends the
  notification after the scan loop; imports `SEVERITY_HIGH`/
  `SEVERITY_MEDIUM` from `core/notifications.py`)
