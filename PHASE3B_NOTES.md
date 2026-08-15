# Phase 3b — VIX Monitoring Bug Fix + FII/DII wired into Exit Engine

## Files changed
- `paper_trading/paper_trading_engine.py`
- `risk/exit_engine.py`

## 1. VIX bug fix (`paper_trading/paper_trading_engine.py`)
`run_cycle()`'s own `market_state` dict (used for the daily monitoring loop that checks every open position — the 10-15 day hold period) never had a `"vix"` key. Every `RiskManager.evaluate()` call made during that daily monitoring therefore fell back to `risk_manager.py`'s hardcoded default (`vix=20.0`), regardless of the real market — so the `vix>=30`/`vix>=35` hard-risk exit trigger could never fire on a held position for the entire hold, no matter what VIX actually did. Fixed by fetching real VIX once per cycle (same pattern as the other once-per-run fetches).

Note: this bug was mine — I fixed VIX in 4 other files in Phase 1 (`orchestrator.py`, `execution/scanner.py`, `scripts/daily_scan.py`, `scripts/generate_full_report.py`) but missed this one, which is specifically the file that runs during the hold, not just at entry. Found it only when you asked directly whether this data actually works during the 10-15 day hold.

## 2. FII/DII wired into the actual exit decision (`risk/exit_engine.py`)
Previously FII/DII (Phase 3a) refreshed daily during monitoring but only fed the diary's informational buy/sell confidence numbers — it never reached `ExitEngine`, so it couldn't actually influence HOLD/EXIT. Now it's a 5th weighted component, mirroring the existing `news_exit` pattern exactly:

- New `institutional_exit` component, bidirectional: strong institutional buying reduces exit pressure on a held BUY / increases it on a held SELL, and strong selling does the reverse. No data = neutral (contributes 0), never fabricated — same philosophy as every other data source in this codebase (VIX fallback aside, which is intentionally different since VIX always has *some* value).
- Weights rebalanced to fit a 5th component: `TECHNICAL 0.35->0.30`, `RISK 0.30->0.25`, `FUNDAMENTAL`/`NEWS` unchanged (0.20/0.15), new `INSTITUTIONAL 0.10`. Took the share from the two largest existing weights rather than diluting the smaller ones.
- Still cannot override a genuine hard-risk trigger (stop-loss breach, target hit, `risk_safe=False`, max-hold-days) — those still force EXIT immediately regardless of institutional flow, unchanged from before.
- `paper_trading_engine.py` now passes `fii_dii_bias=result.diagnostics.get("fii_dii_bias")` into `exit_engine.evaluate()` — the exact same bias value `_evaluate_market_context()` already recomputes fresh every monitoring cycle.

## Testing done
- 6 scenarios directly against `ExitEngine.evaluate()`: no-data neutral case, BUY-held + strong buying (exit pressure down), BUY-held + strong selling (exit pressure up), SELL-held mirror of both, and confirmed a stop-loss hard-risk trigger still forces EXIT regardless of institutional bias. All pass.
- Full existing `pytest tests/` — 4/4 still pass, no regressions.
- `flake8` against the repo's `.flake8` config — exit code 0 on both files.
- Confirmed `execution/tracker.py`'s exit call is a different, unrelated class (`ExitStrategyEngine` from `risk/exit_strategy.py`, the non-live one) — not affected by this change.

## What I could NOT verify live
Same as before — this sandbox can't reach `nseindia.com`/yfinance, so the real VIX/FII-DII network fetch during an actual daily monitoring cycle can only be confirmed in your GitHub Actions run. Everything upstream (parsing, weighting, BUY/SELL mirroring, hard-risk precedence) is tested against realistic data.
