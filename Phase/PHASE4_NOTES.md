# Phase 4 — Tier2 Factor-Score Restructuring (Trend/Momentum/Volume/Volatility)

## What changed

`strategy/buy_strategy.py` and `strategy/sell_strategy.py`'s Tier 2
("technical confirmation") score used to be a flat average of ~30
individual checks, all weighted equally. That let a category with many
near-duplicate checks (e.g. bollinger + donchian + pivot all essentially
voting "price above a support/reference line") silently drown out a
category with few checks, and gave zero credit for the fact that not
every technical signal matters equally in every market condition.

Tier 2 is now **4 non-overlapping category factor scores** — Trend,
Momentum, Volume, Volatility — each 0-100, combined with
**regime-conditional weights driven by the raw ADX-14 reading**:

| Regime | Trend | Momentum | Volume | Volatility | Trigger |
|---|---|---|---|---|---|
| TRENDING | 35% | 30% | 20% | 15% | ADX >= 25 |
| BASELINE | 30% | 25% | 25% | 20% | 20 <= ADX < 25 |
| RANGE_BOUND | 20% | 20% | 20% | 40% | ADX < 20 |

Rationale: when there IS a real trend (high ADX), trend + momentum
checks carry the real signal. When the market is range-bound (low ADX),
"is there a trend" is mostly noise — volatility/squeeze-timing checks
matter more, so that category's weight roughly doubles.

Each factor score is computed from a curated **"core" subset** of
non-redundant checks — not every check in that category. Every other
check is still computed and shown in `checks{}`/`reasons` (still feeds
derived checks like `confirmed_breakout`/`accumulation`/`squeeze_breakout`,
still visible for audits and `analytics/learning_engine.py`'s
agreement-rate scan) — it's just excluded from the factor-score math so
near-duplicate confirmations inside one category don't get counted twice.

### BUY categorization
- **Trend** (core): `price_above_ema20`, `supertrend`, `ichimoku`, `adx`, `ema_fresh_cross`, `pullback_entry`
  (diagnostic-only: `price_above_vwap`, `pivot`, `bollinger`, `donchian`, `pullback`)
- **Momentum** (core): `rsi`, `macd_cross`, `rsi_fresh_cross`
  (diagnostic-only: `roc`, `cci`, `stochastic`, `williams_r`)
- **Volume** (core): `volume_spike`, `obv`, `accumulation`
  (diagnostic-only: `volume`, `cmf`, `mfi`)
- **Volatility** (core): `squeeze_breakout`, `confirmed_breakout`, `atr_filter`, `volatility`
  (diagnostic-only: `breakout`, `gap_filter`, `gap_day`)

### SELL categorization (mirrored)
Same structure with `price_below_ema20`, `distribution`, `breakdown`,
`confirmed_breakdown` in place of their BUY equivalents.

**One real asymmetry, pre-existing, not introduced by this change**:
SELL's breakout-engine section tracks `breakdown` + `failed_breakout`
where BUY tracks `breakout` + `pullback` — these are not literal 1:1
mirrors of each other (BUY has no "failed breakout" concept, SELL never
built an `is_pullback`-based check). `failed_breakout` is grouped into
Volatility-diagnostic (same bucket as `breakdown`/BUY's `breakout`)
since it's a breakout-pattern concept, not a trend-support one. Flagging
this explicitly since it's a judgment call, not a hard mirror.

## `breadth` / `sector` moved into Tier 3

Per the agreed plan, `breadth` and `sector` are no longer part of the
Tier 2 technical-factor math at all — they're market-wide/cross-symbol
context, not stock-level technical signal. They now blend into Tier 3's
existing "market" 15% weight slot:

```
market_context_score = market_score * 0.50 + breadth_value * 0.25 + sector_value * 0.25
```

(SELL mirrors this with inverted market_score and breadth preference,
same as the existing `inverted_market` pattern already in the file.)

**Real-data finding surfaced while wiring this (important — flagging
transparently, not silently)**: `execution/scanner.py` currently
hardcodes `sector_score = 50.0` and `dataframe["breadth"] = 50.0` (a
**float**, not a `"STRONG"/"NEUTRAL"/"WEAK"` string) for every real scan,
because neither cross-symbol sector-rotation data nor market-wide
advance/decline breadth data is wired into the per-symbol pipeline yet
(see the existing NOTEs at that exact spot in `scanner.py`). That means:

- In the OLD code, `checks["breadth"]` and `checks["sector"]` were
  **already always False** in production — comparing a float 50.0 to
  the string `"STRONG"`/`"WEAK"`, and `50.0 >= 70` is False. This was
  true before this restructuring too; not something introduced here.
- In the NEW code, the market-context blend gracefully falls back to a
  neutral 50.0 for both terms (the `.get(..., 50.0)` default), so this
  restructuring is safe and forward-compatible — it just means, as of
  today, moving breadth/sector to Tier 3 doesn't change real production
  behavior at all (they contribute a flat, inert 50 either way) until
  `market/market_breadth.py` and `market/sector_rotation.py` are
  actually wired into `scanner.py`'s per-symbol dataframe. That wiring
  is a separate, real infrastructure gap — not attempted here, out of
  scope for this restructuring.

## `not_overextended` — one additional cleanup beyond the agreed scope

The old code counted `not_overextended` in the flat Tier 2 average
**as well as** using it as a hard reject (`qualified = tier1_passed and
overall_score >= threshold and checks["not_overextended"]`) — a genuine
double-count that wasn't explicitly called out in the earlier
categorization discussion. Since it's fundamentally a chase-risk gate
("too extended, don't chase"), not a graded measurement of technical
health, it's now excluded from all 4 factor scores and used **only** as
the hard reject it already was. Flagging this as a small extra cleanup
made along the way, not a silent scope change — it only removes a
double-count, it doesn't change what triggers the hard reject itself.

## New diagnostics (for live-production verification via logs)

`BuyDecision`/`SellDecision` gained 5 new fields: `trend_factor_score`,
`momentum_factor_score`, `volume_factor_score`, `volatility_factor_score`,
`adx_regime` ("TRENDING"/"BASELINE"/"RANGE_BOUND"). The `logger.info(...)`
line and a new `reasons[]` entry now show the full factor breakdown, e.g.:

```
BUY Strategy | Action=BUY | Confidence=86.55 | Passed=32/40 | ADXRegime=TRENDING |
Trend=83.3 Momentum=66.7 Volume=100.0 Volatility=50.0 | Tier2=76.67 Tier3=94.64
```

so this is verifiable directly from GitHub Actions logs without
re-simulating anything, same pattern as the exit-engine logging added
in Phase 3.

## What did NOT change

- `overall_score = tier2_score*0.45 + tier3_score*0.55` and
  `QUALIFY_THRESHOLD = 58.0` — untouched, out of approved scope.
- Tier 1 (core trend, 2-of-3 majority) — untouched.
- Fundamental/news scoring — untouched (Phase 3 work).
- No existing check was removed from `checks{}`/`reasons` — everything
  that was visible before is still visible; only what feeds the Tier 2
  *score* changed.

## Testing

- `pytest tests/` — 4/4 pass (no dependency on these files' internals).
- `flake8` (repo's exact `.flake8` config) — clean on both files.
- All previously-passing Phase 2 scenario diagnostics (7 BUY + 7 SELL
  scenarios covering fresh-cross/pullback/squeeze/overextension logic)
  still pass unchanged — no regression from the restructuring.
- All previously-passing fundamental-scoring and exit-engine FII/DII
  tests still pass unchanged.
- **New**: `test_tier2_factor_restructure.py` — 8 targeted regression
  tests, BUY + SELL mirrored: ADX-regime switching at exact boundary
  values (19.99/20.0/24.99/25.0), the weighted-formula math itself,
  confirmation that `breadth`/`sector` no longer move `tier2_score` but
  still move `tier3_score`, and confirmation that `not_overextended` is
  now a pure hard-reject that never moves `tier2_score`.

## What was explicitly NOT attempted here (per earlier agreement)

- Deflated Sharpe Ratio, regression-learned weights, agreement-rate
  auto-dedup, sideways/range-dedicated strategy branch — all remain
  deferred pending real closed-trade data, as previously discussed.
