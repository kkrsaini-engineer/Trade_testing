# Phase 3a — FII/DII Institutional Flow (low-risk half of Phase 3)

## Files changed
- `data/fii_dii_data.py` (new)
- `execution/scanner.py` (modified)

## What it does
Fetches NSE's daily FII/DII net cash-market activity (`https://www.nseindia.com/api/fiidiiTradeReact`), combines FII net + DII net (they often partly offset each other — combined is what actually moved the market that day), and converts it into a signed bias in [-1, +1]. Rs 5,000 crore combined net is the saturation point (researched — most days are a few hundred crore either side; Rs 5,000cr+ is already "notably significant", Rs 10,000cr+ is genuinely extreme, so bigger prints don't need a bigger nudge past that point).

## How it's wired in (mirrors BUY/SELL automatically — no strategy file changes needed)
The bias nudges `market_score` in `execution/scanner.py`, right after it's computed from the per-stock regime (BULL=75/SIDEWAYS=50/BEAR=25). `market_score` is a single shared value already passed into **both** `buy_strat.evaluate()` and `sell_strat.evaluate()` — and `sell_strategy.py` already inverts it for its own tier3 calc (`inverted_market = 100 - market_score`). So heavy institutional buying nudges BUY confidence up and SELL confidence down automatically, and heavy selling does the reverse — same mechanism as the existing `macro_bias` → `news_score` nudge, just applied to the other shared score. Weight is 10.0 (vs macro_bias's 20.0 on news_score) — deliberately smaller since market_score already carries a strong per-stock trend signal; FII/DII is secondary context, not the primary driver.

Fetched once per scan run (cached, like delivery data and market headlines) — not once per symbol.

## Fails closed, not fabricated
If the live fetch fails (network, NSE format change, missing FII or DII record in the response), `fetch_latest()` returns `None`, not a fake "neutral 0.0" bias — the scanner then skips the nudge entirely and `market_score` stays exactly at its regime baseline, same as today. Falls back to a disk cache (`storage/reports/fii_dii_cache.json`) if there's a same-day cached read from earlier in the run.

## Testing done
- Unit-tested `_parse_response()` directly: normal case, extreme-value saturation (confirmed clips at bias=1.0), and a malformed/missing-record case (confirmed returns `None`, not a crash).
- Unit-tested the scanner's caching wrapper (`_get_fii_dii_data()`) with a mocked provider — confirmed it fetches once and reuses the cached result across multiple calls, and confirmed the exact nudge arithmetic (`75.0 + 0.3*10 = 78.0`).
- Confirmed the failure path (`fetch_latest()` returns `None`) correctly skips the nudge.
- Full existing `pytest tests/` suite — 4/4 still pass, no regressions.
- `flake8` against the repo's `.flake8` config — exit code 0 on both files.

## What I could NOT verify live
Same limitation as Phase 1's VIX/delivery fetches: this sandbox's network blocks `nseindia.com` (confirmed — same 403-at-proxy error as before). The actual HTTP fetch, NSE's exact current JSON shape, and whether `www.nseindia.com`'s bot-protection accepts these headers can only be confirmed in your real GitHub Actions run. Everything upstream of the network call (parsing, math, caching, wiring, failure handling) is tested against realistic sample data.

## Not included in this round (per your call — PCR next, only after this verifies)
Put-Call Ratio (option-chain based) — higher-risk endpoint (NSE has changed its option-chain API path multiple times), per-symbol (not all scanned stocks have F&O), deferred until this FII/DII piece is confirmed working live.
