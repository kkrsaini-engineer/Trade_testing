# Phase 1 Quick-Wins — Patch Notes

Apply with: `git apply phase1-quick-wins.patch` (from your repo root), then review the diff before committing.

## What changed

1. **VIX wired to a real live fetch** (`market/volatility.py`, new `fetch_india_vix()`)
   Every place that built a `market_state` dict (`orchestrator.py`, `execution/scanner.py`, `scripts/daily_scan.py`, `scripts/generate_full_report.py`) either hardcoded `"vix": 20` or omitted the key entirely, so `risk_manager.py`'s `vix >= 30` / `vix >= 35` risk-off checks could never fire. They now get a real India VIX close (`^INDIAVIX` via yfinance), with a logged fallback to 20.0 if the fetch fails.

2. **Delivery % wired to a real live fetch** (`data/delivery_data.py`, new file)
   `decision/validation_engine.py`'s `delivery_percentage` check had no producer anywhere in the codebase, so it always used its own `100.0` default and the `>= 20.0` gate always passed. `execution/scanner.py` now fetches the NSE daily bhavcopy once per scan run (cached across symbols) and sets the real per-symbol value when available. If the fetch fails, the column is simply left unset — `validation_engine.py`'s existing default behavior is unchanged, so this is a strict improvement with no new failure mode.

3. **Duplicate checks removed** (`strategy/buy_strategy.py`, `strategy/sell_strategy.py`)
   `vwap_confirmation` was byte-identical to `price_above_vwap`; `bull_market`/`bear_market` (Tier 2) was byte-identical to `market_trend` (Tier 1). Both were being counted as if they were independent confirmations. Removed the Tier 2 duplicates; Tier 1 keeps the canonical check.

4. **Exit priority reordered** (`risk/exit_strategy.py`)
   `final_exit`/`partial_exit` now checked *before* `trend_reversal`, so a position that already hit its profit target is recorded as a target-hit, not preempted by a same-bar noisy trend_reversal signal.

5. **Morning-executor macro-news check added** (`scripts/morning_executor.py`)
   `check_overnight_news()` previously only checked company-specific headlines, so a broad overnight shock (war, crisis) that didn't name the specific stock passed through silently. It now also checks `fetch_market_news()` (the same broad-headline source the evening scan already uses) and blocks the trade if either source is strongly against the trade direction.

## Important correction — please read

While implementing item 4, I traced which exit module is actually on the **live** path and found I'd fixed the wrong one initially. There are two separate, unrelated exit engines in this codebase:

- **`risk/exit_strategy.py`** (`ExitStrategyEngine`) — used only by `execution/tracker.py`, which is imported only by `orchestrator.py`, which is used only by `tests/test_pipeline.py`. **Not on the live production path.**
- **`risk/exit_engine.py`** (`ExitEngine`) — used by `paper_trading/paper_trading_engine.py`, which `scripts/run_paper_trading.py` runs, which is what `.github/workflows/paper_trading.yml` actually executes. **This is the live one.**

I fixed the priority-ordering bug in `exit_strategy.py` anyway (harmless, and it's still real code someone may read/extend), but I checked `exit_engine.py` for the equivalent problem and **it does not have it**: target-hit is already checked as a hard-priority trigger (alongside stop-loss) *before* the weighted "technical exit" score (which includes trend-reversal signals) is even consulted for the action decision. So no fix was needed there.

I also re-checked the "`emergency_exit`/`negative_news` flags are never set" finding from earlier in our conversation against the live file: those specific dead flags only exist in the non-live `exit_strategy.py`. The live `ExitEngine.evaluate()` computes its `news_exit` component fresh every call from real `news_score` — and that `news_score` **already includes macro/geopolitical bias** (`execution/scanner.py`'s `_evaluate_market_context()` merges `macro_intelligence.sector_bias(...)` into `news_score` before it reaches both the entry strategy AND `ExitEngine` for held-position monitoring). So held positions were less exposed to the "macro news blindness" gap than I'd described — that gap is real specifically in `morning_executor.py`'s pre-execution check for **new** candidates (fixed in this patch, item 5), not in ongoing position monitoring.

## What I could NOT verify live

This sandbox's outbound network blocks both `nseindia.com` and Yahoo Finance (`403` at the proxy level) — confirmed by direct test. I could not run a live end-to-end fetch of VIX or delivery data from here. What I verified instead:

- All modified modules import without error.
- `fetch_india_vix()` and `DeliveryDataProvider.fetch_latest()` both fail closed (graceful fallback, no crash/exception) when the network is unreachable — tested directly in this sandbox.
- `BuyStrategyEngine`/`SellStrategyEngine` run end-to-end on synthetic data post-dedup, producing correct BUY/SELL decisions with the duplicate keys genuinely gone from `technical_checks`.
- The exit-priority reorder was tested with a synthetic same-bar "target hit AND trend reversed" scenario — confirmed `"Final target achieved."` now wins.
- `morning_executor.py`'s new macro-news check was tested — it correctly fell back to the repo's own pre-existing cached macro-headlines file (`storage/reports/macro_headlines_cache.json`, last written by your real Aug 14 production run) when live fetch was blocked, proving the code path runs correctly end-to-end on real-shaped data, just not a live network call.
- All 4 pre-existing tests in `tests/` still pass.

**Please run this in your actual GitHub Actions environment (or anywhere with normal internet access) before trusting the live VIX/delivery fetches** — the network path itself is untested by me, only the code's correctness and its failure handling.

## Not included in this patch

Per our agreed Phase 1 scope: late-entry fix, PCR/OI module, FII/DII, probability-calibration — all deferred to later phases.
