# Phase 26 — Point 11: Correlated Risk-Factor Double-Counting Redesign (DONE)

The original "Point 11" critique lost its exact wording when this
conversation's context was compacted. Re-derived the scope from a fresh
read of `risk/risk_manager.py`, `risk/portfolio_rules.py`,
`decision/validation_engine.py`, and the actual live/paper-trading
pipeline — findings below are grounded in that re-read, not memory.
Scope grew twice mid-phase as deeper findings surfaced; each expansion
was confirmed with you before implementing (see the three
clarifying-question rounds this phase — weight scheme, correlation
params, and the `generate_full_report.py` fake-portfolio bundle).

## What was actually wrong (confirmed via code, not assumption)

1. **`portfolio.get("correlation", 0.0)`** was read in three files
   (`risk_manager.py`, `validation_engine.py`, `portfolio_rules.py`) but
   NO producer anywhere in the codebase ever set this key — same class
   of bug as the `portfolio_drawdown`/`daily_loss` dead-input discovery
   in Phase 21. Always 0.0, everywhere, regardless of real portfolio
   correlation.
2. **The same current-state fact (sector_exposure / correlation) was
   independently evaluated in up to THREE engines** — a genuine
   double-count for `risk_manager.py`'s weighted score (re-scoring what
   `validation_engine.py` already hard-gates), and a genuine pure
   duplicate between `validation_engine.py` and `portfolio_rules.py` for
   correlation specifically. `portfolio_rules.py`'s sector_exposure
   check, on closer read, is NOT a duplicate — it's a `projected`
   (accounts for the specific new trade being sized) check the other
   two can't do; kept as-is.
3. **`risk_manager.py`'s ATR risk and Volatility risk are two
   independently-weighted buckets (combined weight 0.20) measuring the
   same underlying phenomenon** — how much a stock's price moves.
4. **A deeper, separate class of bug, found while investigating #1**:
   `risk_manager.py` read `portfolio.get("open_positions_count", 0)` /
   `portfolio.get("open_exposure", 0.0)` — keys that don't exist on the
   REAL portfolio dict (`PortfolioEngine.snapshot()` /
   `VirtualPortfolio.snapshot()` produce `"open_positions"` (a dict) and
   `"exposure"`). `portfolio_risk`/`sector_risk` were therefore
   structurally frozen near their minimum baseline in live trading,
   regardless of actual portfolio state — same class of bug as the
   circuit_breaker wrong-read-source fix in Phase 22.
5. **The biggest one, found while tracing where `RiskManager` actually
   runs live**: `scripts/generate_full_report.py` — the script
   `.github/workflows/daily_scan.yml` actually runs, which produces
   `reports/candidates_order.json`, which `scripts/morning_executor.py`
   reads to open REAL positions — passed a **completely fake, static,
   always-empty `portfolio` dict** (`open_positions={}`,
   `total_capital=100000.0`) into every single `scan_symbol()` call.
   `portfolio_allowed` (the gate deciding whether a candidate becomes an
   order) was therefore NEVER evaluated against the real portfolio, no
   matter how concentrated or correlated the real holdings were. This
   made #1-#4 above largely academic for the live candidate-generation
   path specifically — fixing the weighting math on top of fake input
   would have been "looks fixed, isn't real," the exact pattern this
   whole review keeps finding and correcting. You explicitly asked to
   bundle this into the same phase rather than defer it.

## What was fixed

### `risk/risk_manager.py`
- `open_positions_count`/`open_exposure` → now read the real keys
  (`len(portfolio.get("open_positions", {}))` / `portfolio.get("exposure",
  0.0)`).
- ATR risk + Volatility risk merged into one weighted component,
  `price_volatility = max(atr_risk, volatility_risk)` — max, not
  average, because this is a safety-facing risk score: if either lens
  flags high volatility, that's the more conservative signal to act on.
  `atr_risk`/`volatility_risk` are still individually computed and left
  in `diagnostics` exactly as before — only the WEIGHTED-SUM input
  changed.
- `sector_risk`/`correlation_risk` **removed from the weighted sum**
  (`validation_engine.py` is now the sole hard-gate for those two
  facts) but still individually computed and reported in `diagnostics`
  for audit visibility.
- New weights — proportionally rescaled from the original 11-factor
  scheme after removing sector(0.05)+correlation(0.10)'s 0.15 budget,
  so every surviving factor's RELATIVE weight vs. every other surviving
  factor is unchanged (pure math, not a re-judgment of importance),
  confirmed with you before implementing:

  | component | weight |
  |---|---|
  | price_volatility (merged ATR+BB) | 0.24 |
  | gap | 0.09 |
  | overnight | 0.09 |
  | news | 0.12 |
  | liquidity | 0.12 |
  | market | 0.14 |
  | portfolio | 0.14 |
  | capital | 0.06 |

  (sums to 1.00)

### `risk/portfolio_rules.py`
- `open_positions_count` → same real-key fix as risk_manager.py.
- Correlation hard-reject **removed** (pure duplicate of
  `validation_engine.py`'s gate) — `portfolio_correlation` is still
  computed/reported, and the `>0.70` soft warning (not a rejection)
  stays.
- Sector exposure's **projected** check (`sector_exposure +
  new_position_exposure > MAX_SECTOR_EXPOSURE`) — kept, unchanged. Not
  a duplicate of `validation_engine.py`'s cruder current-state-only
  check (that one runs before position sizing, so it structurally
  can't account for the specific trade being sized).

### `portfolio/correlation.py` (new)
- `compute_portfolio_correlation(closes, lookback_days=20,
  min_positions=2, min_overlapping_days=10)` — average pairwise Pearson
  correlation of daily returns across open positions, over the last 20
  trading days (both numbers confirmed with you). Returns `None` — never
  a fabricated `0.0` — when correlation can't be meaningfully computed
  (fewer than 2 positions, or insufficient overlapping history). A
  negative result (genuine diversification) is reported as-is, not
  clipped.
- `fetch_correlation_inputs(symbols, market_provider, period="2mo")` —
  fetches each open position's recent closes via
  `data/market_data.py`'s `MarketDataProvider` (OHLCV-only, deliberately
  NOT the full fundamentals+news bundle) — skips (doesn't fabricate)
  symbols whose fetch fails.

### `paper_trading/paper_trading_engine.py`
- Real correlation computed ONCE per cycle (before the monitoring
  loop) — same "fetch once per run" reasoning as VIX/FII-DII/circuit-bands
  elsewhere in this codebase.
- `portfolio_dict` inside the monitoring loop switched from
  `self.portfolio.engine.snapshot()` (bare `PortfolioEngine`, which
  doesn't track sectors at all) to `self.portfolio.snapshot()` (the
  `VirtualPortfolio` wrapper, which does, via the existing
  `register_sector()` calls) — its `"sector_exposure"` dict is renamed
  to `"sector_exposure_by_sector"` to match the contract
  `execution/scanner.py`'s per-symbol injection expects (feeding the
  raw dict into a spot that expects a scalar ratio would crash
  downstream `float(...)` calls).
- `_top_risk_factors()`'s notification helper — `labels` dict updated to
  match the new `risk_components`/`risk_weights` keys (merged
  `price_volatility`; `sector`/`correlation` naturally drop out of the
  weighted breakdown instead of showing a misleading "0% contribution"
  line).

### `execution/scanner.py`
- New `_sector_exposure_ratio(sector, portfolio)` static method —
  computes the SCALAR ratio for one symbol's sector from
  `portfolio["sector_exposure_by_sector"]` ({sector: $value}) and
  `portfolio["portfolio_value"]`. Callers that don't supply these two
  keys (backtests, diagnose scripts) get 0.0 — the same fallback that
  was already always produced before this phase, not a new regression.
- Wired into BOTH `scan_symbol()` (entry path — computed right after
  `_evaluate_market_context()` returns `diagnostics["sector"]`, same
  per-symbol-copy pattern as Phase 25's `circuit_breaker`) and
  `evaluate_position()` (monitoring path).

### `scripts/generate_full_report.py`
- Fake static `portfolio` dict replaced with a REAL, read-only
  `VirtualPortfolio()` snapshot (same real state paper trading uses —
  no `.save()`/mutation here, this script only proposes candidates).
  `"sector_exposure"` renamed to `"sector_exposure_by_sector"` (same
  contract as above). Real portfolio correlation computed once for the
  whole scan run, same as paper_trading_engine.py.
- **`scripts/daily_scan.py`** — confirmed (by reading
  `.github/workflows/daily_scan.yml` directly) that this file is NOT
  what any workflow actually runs (`generate_full_report.py` is,
  despite `daily_scan.py`'s own docstring claiming otherwise — that
  claim is corrected in the file). Has the exact same fake-portfolio
  bug; NOT fixed, since it's genuinely dead code — flagged with a
  comment instead, same "why bother, nothing runs it" reasoning applied
  to `orchestrator.py`'s `risk_snapshot` in PHASE24_NOTES.md.

## Verification

```
python3 -m pytest tests/ -q                    # 161 passed (135 prior + 26 new)
python3 -m flake8 --config=.flake8 .            # whole repo, clean
python3 -c "import scripts.generate_full_report, paper_trading.paper_trading_engine, \
  execution.scanner, portfolio.correlation, risk.risk_manager, risk.portfolio_rules, \
  decision.validation_engine"                   # all import cleanly, no circular imports
```

New test files: `tests/test_portfolio_correlation.py` (11 tests — pure
correlation math, mocked-fetch skip-on-failure behavior),
`tests/test_scanner_sector_exposure.py` (7 tests — per-symbol ratio
computation, missing-data fallbacks). Extended `tests/test_risk.py` (8
new tests — hand-computed weighted total_risk against the new scheme,
ATR/Volatility merge via `max()`, sector/correlation reported-but-not-weighted,
real open_positions/exposure key reads) and `tests/test_portfolio_rules.py`
(5 new tests — real open_positions gating, correlation dedup). Mirrored
BUY/SELL throughout per the standing rule.

## Files changed

`risk/risk_manager.py`, `risk/portfolio_rules.py`,
`portfolio/correlation.py` (new), `paper_trading/paper_trading_engine.py`,
`execution/scanner.py`, `scripts/generate_full_report.py`,
`scripts/daily_scan.py` (comment only), `tests/test_risk.py`,
`tests/test_portfolio_rules.py`, `tests/test_portfolio_correlation.py`
(new), `tests/test_scanner_sector_exposure.py` (new).

## Still open (unchanged from PHASE25_NOTES.md, not touched this phase)

- Point 15 (explicit `position_state` state machine)
- Point 16 (30-day flat time-exit → thesis-decay-based)
- `execution/broker.py`'s remaining ~400 lines of dead simulation code
  (revive vs. delete — a separate product decision, still yours to make)
- `orchestrator.py`'s hardcoded `risk_snapshot` (`WiredOrchestrator` has
  no live caller — still needs your sign-off before spending effort on
  it)
- `orchestrator.py`'s hardcoded `market_open=True`/`holiday=False`
  (same "no live caller" situation, noted in passing during this
  phase's investigation — not touched)
