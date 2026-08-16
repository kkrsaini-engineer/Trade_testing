# PHASE 9 — Fix #4 (backtest contamination), #6 (forced 1-share), #7 (evidence-family duplicate vote)

Implemented on explicit request ("ab 4,6,and 7and fix kro (wired also check)"),
with fact-checking against actual code before implementing, per this
conversation's standing rule.

---

## Fix #4 — Backtest point-in-time contamination

**Confirmed real, but the scope is narrower than the pasted critique
claimed — corrected below before fixing anything.**

**Correction to the earlier VIX claim:** the pasted critique (my own
earlier text, quoted back to me) said VIX leaks into the backtest the same
way FII/DII does. Re-checked against the actual code before touching
anything: `execution/scanner.py`'s `_evaluate_market_context()` — the
method the backtester actually calls — **never calls `fetch_india_vix()`
at all**. Only `prepare_orders()` does, and the backtester doesn't use that
method. `risk/risk_manager.py` reads VIX via `market.get("vix", 20.0)` from
the `market_state` dict the *caller* builds, and
`analytics/backtest_engine.py`'s `market_state` dict never sets `"vix"` —
so backtest VIX is a fixed `20.0` for the entire run, not today's live
reading. **Not a leak. Nothing was fixed for VIX because there was nothing
to fix.**

**What genuinely leaks (confirmed):** `MarketScanner` lazy-fetches and
caches three things once per scanner *instance*, on the assumption that one
instance = one real live scan:

- `_get_fii_dii_data()` — FII/DII net institutional flow
- `_get_market_headlines()` — macro/sector news headlines (feeds
  `macro_bias`)
- `_get_delivery_data()` — NSE delivery-percentage bhavcopy

`BacktestEngine.__init__` builds **one** `MarketScanner` and reuses it
across the *entire* multi-year day-by-day replay loop in `run()`. Whichever
of these three fetches whatever was live on the day the backtest happened
to be run gets silently reused for every single simulated historical day —
e.g. a backtest of March 2020 would score every simulated day of that crash
against today's real FII/DII flow and today's real headlines, not March
2020's.

**Fix applied:** added an opt-in `MarketScanner(disable_live_market_context:
bool = False)` constructor flag. When `True`, all three fetch helpers
short-circuit to the *same* "no live data" fallback each already supports
for a real fetch failure (`[]` headlines → `macro_bias` stays `0.0`; `{}`
delivery data → column left unset; `None` FII/DII → nudge skipped) —
**no new fallback behavior was invented**, this just routes to the
already-existing one. `analytics/backtest_engine.py`'s `BacktestEngine.
__init__` now does `self.scanner = scanner or MarketScanner
(disable_live_market_context=True)` when no scanner is explicitly passed
in.

**Default is `False`** — every other `MarketScanner()` construction site
(`daily_scan.py`, `orchestrator.py`, `paper_trading_engine.py`,
`prepare_orders()`, diagnostics scripts) is completely unaffected; only
`BacktestEngine`'s default construction opts in.

**Still NOT fixed (explicitly out of scope, flagged honestly):**
`fundamentals` remains a static snapshot scored against every simulated
day — real point-in-time fundamentals need an actual as-reported-on-date
data source/store, which is new data infrastructure, not a code change.
This sandbox has no path to nseindia.com/a fundamentals vendor to build or
verify that against, so it stays a documented, known limitation
(docstring updated to say so explicitly) rather than a fix.

**Advantage:** backtest results for FII/DII/macro-news/delivery-driven
score components now reflect "no data available" for historical days
instead of silently leaking today's real market state — a backtest of a
past period is measurably closer to what it claims to measure.
**Disadvantage:** none for the live/paper-trading path (zero behavior
change, flag defaults off). For backtests specifically, the honest
trade-off is that FII/DII/macro/delivery nudges now contribute nothing
during a backtest (rather than a wrong value) — slightly less signal in
backtested scores, but a wrong signal is worse than no signal for
something meant to validate strategy quality.

---

## Fix #6 — Position sizing could not size to 0 shares

**Verdict: confirmed, exact match to the code, and cheaper to fix than the
critique suggested.**

`risk/position_sizing.py` had `MIN_QUANTITY = 1` force-applied via
`max(quantity, MIN_QUANTITY)` in two places: once on the capital-allocation
-based `quantity`, and again on the final `executable_quantity = min
(atr_quantity, capital_quantity)`. If the ATR-based risk-budget calculation
(`atr_quantity = risk_per_trade / stop_distance`) genuinely computed `0` —
meaning even 1 share's stop-loss distance would risk more than
`MAX_RISK_PER_TRADE` (2%) allows — the code overrode that to `1` anyway,
taking a position that violates the risk budget it had just calculated.
Traced through to `paper_trading_engine.py`'s "🟢 New Virtual Trade Opened"
path in an earlier phase of this review — a real, live-reachable bug.

**Fix applied:** removed both `max(..., MIN_QUANTITY)` floors.
`executable_quantity` (and the intermediate capital-based `quantity`) can
now legitimately be `0`.

**Correction to the critique's proposed scope:** the critique suggested
adding a new `PORTFOLIO_REJECT` rejection path for this. That
infrastructure **already exists** —
`risk/portfolio_rules.py:134`: `if sizing.quantity <= 0: rejection_reason =
"Invalid position size."`. A 0-quantity `PositionSizingResult` was already
a supported, correctly-handled input to `PortfolioRulesEngine`; this engine
was just never allowed to produce one. No new rejection logic was needed —
this was cheaper to fix correctly than expected.

**Verified end-to-end (not just unit-level):** a synthetic extreme-ATR
scenario (huge stop distance relative to the risk budget) now produces
`atr_quantity=0`, `final quantity=0`, and feeding that result into the real
`PortfolioRulesEngine.evaluate()` correctly returns `allowed=False,
rejection_reason="Invalid position size."` — the existing downstream
rejection path picks it up with zero additional code.

**Advantage:** closes a real risk-control gap — the engine can no longer
silently take a trade that violates its own risk budget. **Disadvantage:**
essentially none; this was a small, contained, two-line change.

---

## Fix #7 — MACD-style duplicate vote reintroduced in Phase 4's factor grouping

**Verdict: the formal "evidence family" abstraction proposed in the
critique is still not built (correctly assessed as low-priority,
guard-rail-for-the-future scope) — but re-checking the actual `*_CORE`
lists before writing that off surfaced a genuine, concrete instance of
exactly the bug #7 warns about, reintroduced by Phase 4's own factor-score
grouping in this same conversation.**

`buy_strategy.py`'s `VOLUME_CORE = ["volume_spike", "obv", "accumulation"]`
— but `checks["accumulation"] = checks["obv"] and checks["cmf"]` (a few
lines above, right next to a comment about the *macd_cross/macd_histogram*
duplicate-vote fix that removed the exact same pattern). `"obv"` was being
averaged into `volume_factor_score` **both directly and again folded into
"accumulation"** — inflating the volume factor whenever OBV was positive,
without adding independent evidence. `sell_strategy.py`'s mirror
(`VOLUME_CORE` + `"distribution"` = `obv and cmf`) had the identical issue.

**Fix applied:** removed `"obv"` from both `VOLUME_CORE` lists —
`accumulation`/`distribution` already carry the OBV signal (combined with
CMF), so nothing is lost, only the double-count.

**Also added:** a small structural guard — an `assert` that no check key
appears in more than one `*_CORE` list, run every time `evaluate()` builds
the factor scores. **Scoped honestly, not oversold:** this only catches a
*literal* duplicate key across lists (the original macd_cross/
macd_histogram-style mistake). It does **not** catch a future composite
check silently re-using another check's underlying signal the way
`accumulation` did with `obv` — that would need real dependency tracking
through derived checks, which is a bigger structural project left for a
future request if wanted, not built here.

**What's still not done, staying consistent with the earlier assessment:**
a formal, reusable "evidence family" abstraction that forces every new
indicator to declare its family and respects a cap. Given the concrete
instances found (macd, and now obv/accumulation) are both fixed, this
remains lower priority — a guard-rail for future drift, not an active bug.

**Advantage:** removes a live, currently-active double-count in
`tier2_score`'s volume factor (both BUY and SELL) that had gone unnoticed
despite the identical pattern being fixed for MACD one factor-group over.
**Disadvantage:** none identified for the concrete fix. The added
assertion has a real, stated limitation (doesn't catch composite-check
reuse) — flagged rather than presented as a complete solution.

---

## Wiring verification (all three, live production path)

- **#6**: `execution/scanner.py:104` `self.sizer = PositionSizingEngine()`,
  called at `scanner.py:~586` inside the live `scan_symbol()` path;
  `diagnostics["quantity"]` flows to `ScanResult.position_size`, which
  `paper_trading_engine.py` uses to open real paper positions. Confirmed
  via a live `PositionSizingEngine.calculate()` → `PortfolioRulesEngine.
  evaluate()` call chain, not just unit math.
- **#4**: `analytics/backtest_engine.py`'s `BacktestEngine()` (no scanner
  arg) confirmed to construct `MarketScanner(disable_live_market_context=
  True)` — verified via direct attribute check
  (`bt_engine.scanner._disable_live_market_context is True`). Confirmed
  every other `MarketScanner()` call site keeps the default `False`
  (`MarketScanner()._disable_live_market_context is False`) — zero
  behavior change to live/paper-trading scans.
- **#7**: the edited `*_CORE` lists live inside `BuyStrategyEngine.
  evaluate()`/`SellStrategyEngine.evaluate()` — the exact methods
  `execution/scanner.py:428/435` (`self.buy_strat.evaluate(...)`,
  `self.sell_strat.evaluate(...)`) call on every real scan. Confirmed via
  the full regression suite (`test_tier2_factor_restructure.py`,
  `test_scoring_unification.py`) exercising this exact code path — both
  pass cleanly (no assertion errors, tier2_score/volume_factor_score
  values sane) after the change.

## Testing performed

- `flake8 --config=.flake8` on all 5 changed files — clean (exit 0).
- `python3 -m pytest tests/ -q` — 4 passed, no regressions.
- Import sanity check on `execution.scanner`, `risk.exit_engine`,
  `strategy.buy_scoring`, `strategy.sell_scoring`,
  `analytics.backtest_engine`, `risk.position_sizing` — all clean.
- Full regression re-run of all 6 prior phase test scripts — all pass, no
  regressions.
- New targeted smoke tests (this phase): #6 end-to-end (extreme-ATR
  scenario → `quantity=0` → `PortfolioRulesEngine` correctly rejects with
  "Invalid position size."); #4 (`disable_live_market_context` flag
  short-circuits all 3 fetchers with zero network calls when `True`,
  default scanner unaffected; `BacktestEngine()` auto-opts-in).

## Files changed

- `risk/position_sizing.py` (#6)
- `execution/scanner.py` (#4)
- `analytics/backtest_engine.py` (#4)
- `strategy/buy_strategy.py` (#7)
- `strategy/sell_strategy.py` (#7)
