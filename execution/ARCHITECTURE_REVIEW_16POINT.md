# 16-Point Architecture Critique — Fact-Checked Against Actual Code

No files modified. Every claim below was verified by reading the actual
repo code (not assumed), with exact file:line references. Verdict key:
**CONFIRMED** (verified true in current code), **ALREADY FIXED** (true
before, fixed earlier in this conversation), **PARTIALLY TRUE**, **NOT
QUITE** (claim is imprecise/wrong about impact).

---

## P0

### 1. Probability → Kelly contract
**Verdict: CONFIRMED — this is real, and it's serious.**

`risk/position_sizing.py:211-215`:
```python
win_rate = (decision.buy_probability if decision.action == "BUY"
            else decision.sell_probability) / 100.0
```
`decision.buy_probability` comes from `BuyProbabilityEngine._win_probability()`
(`strategy/buy_probability.py:91-103`):
```python
x = (score.overall - 50) / 10
probability = 100 / (1 + math.exp(-x))
```
This is a **sigmoid transform of a composite score**, not a calibrated
historical win-rate. It has never seen a single closed trade. Feeding it
straight into the Kelly formula (`risk/position_sizing.py:226`:
`kelly_fraction = ((win_rate*(reward_risk+1))-1)/reward_risk`) means
position size is being set by a number with no empirical backing.

**Advantage of the proposed fix**: correct, industry-standard separation.
Disabling Kelly until real calibration data exists is exactly the same
"don't fabricate data" discipline already used successfully in this
conversation (fundamental exclude-renormalize, no-news-neutral pattern) —
I'd recommend it without hesitation.

**Disadvantage / cost**: this is not a code refactor, it's a **new data
requirement** — calibration needs a table of closed trades with
(setup_score, outcome) pairs, binned, with enough samples per bin to be
statistically meaningful (dozens minimum per bin, really hundreds).
`analytics/learning_engine.py` and `analytics/backtest_engine.py` exist
but neither currently produces this calibration table. Until then, the
correct fallback (fixed risk % per trade) is a ~20-line change; the
*calibration* itself is weeks-to-months of trade accumulation, not a
coding task. Framing it as "convert to production solution now" oversells
how fast the real fix (the calibration part) can land.

---

### 2. SELL market-score direction
**Verdict: CONFIRMED — and worse than described. It's inconsistent, not just wrong.**

- `strategy/sell_strategy.py:839` (Tier 3 blend) — **already correctly
  inverts**: `inverted_market = 100.0 - min(max(market_score,0),100)`.
  This was fixed earlier in this conversation (Phase 3/4).
- `strategy/sell_strategy.py:729` (Tier 2 boolean check) — **NOT
  inverted**: `checks["market_score"] = market_score >= 60`, and the
  reason text literally says `"Overall market score favors bearish
  trades."` when market_score is HIGH (bullish). That's backwards.
- `strategy/sell_scoring.py:132` / `strategy/buy_scoring.py:172` — **NOT
  inverted**: both do `result.market = self._normalize(market_score)`,
  identical code, no SELL-side inversion. This value feeds
  `sell_probability.expected_return` and `_confidence()` directly.
- `execution/scanner.py:307` even has a comment acknowledging this:
  *"sell_strategy.py already inverts market_score for its own
  weighting"* — i.e. the inconsistency was already noticed and left
  unfixed elsewhere.

So: 1 of 4 touch points is correct, 3 are not. This is a real, current,
verified bug, independent of anything fixed earlier in this conversation.

**Advantage of the proposed `MarketContext(directional_bias)` fix**:
centralizes the inversion in ONE place instead of 4 independently
maintained copies — exactly the kind of drift this file has already
demonstrated (one got fixed, three didn't, silently).

**Disadvantage**: touches 4 files simultaneously across two engines that
currently have different internal representations (bool checks vs raw
floats vs weighted blends) — needs care to not change BUY-side behavior
while fixing SELL-side, and needs full regression coverage on both
directions (same rigor as the Phase 4/5 work already delivered).

---

### 3. Missing fundamentals ≠ weak fundamentals
**Verdict: PARTIALLY TRUE — the worst version of this bug is already fixed; the remaining gap is real but narrower than described.**

`strategy/fundamental_scoring.py` (rewritten in this conversation,
Phase 3) already does exclude-and-renormalize: a missing metric is
excluded from both numerator and denominator, so partial coverage (e.g.
3/8 metrics present) is judged only on what's present, not defaulted to
a bad value. Real audit data (500 symbols) showed this matters a lot
(roe/peg/operating_cashflow missing 85-91% of the time).

What's **still missing**, and where the claim is accurate: the function
returns a single float — no `coverage` / `available_metrics` is exposed
to the caller. So `BuyDecision`/`SellDecision`/`decision_engine.py`
cannot currently distinguish "72/100 from 8/8 metrics" from "72/100 from
1/8 metrics" — there's no way to gate on low-confidence coverage today.

**Advantage**: cheap, additive change — `buy_fundamental_score()` already
computes `weight_present` internally, it's just not returned. Exposing
`coverage = weight_present/100` and threading it through to
`BuyDecision`/`SellDecision` is a small, low-risk addition, not a rewrite.

**Disadvantage**: deciding what to DO with low coverage (reject? downweight
the whole fundamental component further? just log it?) is a policy
decision, not just plumbing — needs the same kind of "what should happen"
clarification this conversation already went through once for the
missing-data question.

---

### 4. Backtest contamination (point-in-time leakage)
**Verdict: CONFIRMED, and it's worse than the framing suggests — it directly implicates my own Phase 3 work (VIX + FII/DII).**

`analytics/backtest_engine.py:127-129` — the code's OWN docstring already
admits this: *"fundamentals: optional {symbol: dict} — static snapshot
used for every day (... historical point-in-time fundamentals aren't
available from this pipeline)."* So fundamentals leakage is a known,
documented limitation already.

**What I found that's NOT yet documented**: `backtest_engine.py` has
**zero VIX or FII/DII wiring** at all. But `execution/scanner.py`'s
`_evaluate_market_context()` — which the backtester calls, since it
reuses the real production `MarketScanner` — calls `fetch_india_vix()`
and `FiiDiiDataProvider.fetch_latest()` unconditionally, every symbol,
every simulated day. Both are **live-only fetchers with no historical
mode**. So a backtest of, say, March 2020 would use TODAY's real VIX and
FII/DII reading for every single simulated day of that crash — not
March 2020's actual values. This is exactly the leakage the critique
describes, and it's specifically in code delivered earlier in this
session (Phase 3), which I hadn't flagged until checking this now.

News is handled more safely: `news=[]` is hardcoded in every backtest
step (`backtest_engine.py:194`) — so news doesn't leak, but it also means
news impact can never be validated in a backtest at all (silently
disabled, not simulated).

**Advantage of the proposed fix**: point-in-time contracts are the
correct, industry-standard way to make a backtest trustworthy. Given
VIX/FII-DII genuinely leak today, this isn't optional polish — a
backtest run through the current pipeline is not measuring what it
claims to measure.

**Disadvantage — this is the big one**: this needs an actual **historical
data source** for VIX and FII/DII (a real per-day series, not a live
snapshot), and point-in-time fundamentals (as-reported-on-date, not
"today's yfinance value"). This sandbox has already established, repeatedly,
that it cannot reach nseindia.com or Yahoo Finance at all — so I can't
even verify what historical data sources would be available for this
without your GitHub Actions environment being tested against a real
vendor. This is likely the single most expensive item on this whole list
to do properly (a new historical dataset + storage format, not a
refactor), and "bina naye fancy modules" doesn't really apply here — a
point-in-time data store IS a new module, there's no way around it.

**Cheapest honest interim fix, if full point-in-time isn't feasible
soon**: exclude VIX/FII-DII from the backtest's market-context call
entirely (force them to the "no data" neutral path, which both already
support) rather than leaking today's live value — that's a small change
and immediately removes the specific bug found above, even before a real
historical feed exists.

---

### 5. Late-entry architecture (Trend / Setup / Entry separation)
**Verdict: PARTIALLY already addressed — the checks exist, the architecture doesn't.**

Phase 2 of this conversation ("Early-Entry Engine") already added exactly
this kind of signal: `ema_fresh_cross`, `rsi_fresh_cross`,
`squeeze_breakout`, `pullback_entry`, and a hard-reject
`not_overextended` cap — these ARE trying to answer "is this the right
MOMENT to enter" rather than just "is this bullish." So the raw
ingredients for Setup/Entry-quality thinking already exist.

What's missing, and where the critique is accurate: they're flattened
into the same 40-key `checks{}` dict as everything else (Phase 4 grouped
them into Trend/Momentum/Volume/Volatility factor buckets, but that's
still a scoring grouping, not a state machine). There's no explicit
`TrendState` / `SetupState` / `EntryState` object, and critically, no
rule like *"TREND=bullish + SETUP=old breakout + ENTRY=extended → hard
NO_TRADE even if score=92"* — today an old, extended breakout can still
mathematically qualify if enough OTHER checks compensate, because
everything gets blended into one weighted number.

**Advantage**: an explicit state machine is much more explainable
(exactly what critique #15 argues too) and makes "reject old/extended
setups regardless of score" a hard rule instead of hoping the weights
work out that way.

**Disadvantage**: this is a genuine architecture change (new dataclasses,
new decision layer, re-deriving `qualified` from state combinations
instead of a threshold) — bigger than anything done in Phases 2-5 of
this conversation, all of which stayed within the existing checks-dict
shape. Real risk of behavior change across the whole qualify/reject
surface, needs the heaviest testing of anything proposed here.

---

### 6. Position sizing cannot force 1 share
**Verdict: CONFIRMED, exact match to the code.**

`risk/position_sizing.py:97`: `MIN_QUANTITY = 1`. Then twice —
`position_sizing.py:454-457` and `:525-528` — `quantity =
max(quantity, self.MIN_QUANTITY)` and `executable_quantity =
max(executable_quantity, self.MIN_QUANTITY)`. If the ATR-based
risk-budget quantity (`atr_quantity = risk_per_trade / stop_distance`,
line 500) computes to 0 — meaning even 1 share's stop-loss distance
would risk more than the allowed per-trade risk — the code overrides
that to 1 anyway. This is a real, live bug: it can take a position that
violates the very risk budget it just calculated.

**Advantage of the proposed fix**: correct and genuinely important — this
is a risk-control bug, not a style preference. `PORTFOLIO_REJECT` with a
clear reason when `final_quantity < 1` is the right behavior.

**Disadvantage**: essentially none — this is a small, contained,
low-risk fix (a few lines, one new rejection path). Of everything on
this list, this is one of the cheapest to fix correctly and probably
should be prioritized regardless of what else gets done.

---

## P1

### 7. MACD duplicate as "evidence family," not just deletion
**Verdict: LARGELY ALREADY ADDRESSED, differently than proposed.**

The specific `macd_cross` + `macd_histogram` duplicate was removed (not
patched) from `buy_strategy.py`/`sell_strategy.py` earlier in this
conversation. Phase 4 then grouped momentum checks
(`rsi`, `macd_cross`, `rsi_fresh_cross`) into one weighted "Momentum"
factor score, which is conceptually similar to an "evidence family" —
though without the specific "internal caps" mechanism described (it's a
flat average within the category, not a capped point system).

Separately, Phase 5 (just now, this session) **removed the entire
independent duplicate-implementation** in `buy_scoring.py`/
`sell_scoring.py`, which still had this exact same macd duplicate — so
that copy of the bug no longer exists anywhere in the live decision path.

**What's NOT done**: a formal, reusable "evidence family" abstraction
(so a future indicator addition is forced to declare its family and
respects a cap) doesn't exist — today avoiding double-counting still
relies on a human noticing at each new check, not a structural
guarantee.

**Advantage**: a formal family abstraction prevents *future* drift, not
just today's known duplicates.
**Disadvantage**: given the concrete instances are already gone, this is
now more of a "guard-rail for the future" than an urgent fix — lower
priority than it would have been before Phases 4/5.

---

### 8. Sector / breadth placeholders
**Verdict: CONFIRMED — already flagged, not yet fixed.**

`execution/scanner.py:360` / `:366`: `sector_score = 50.0` and
`dataframe["breadth"] = 50.0` are hardcoded for every real scan (the
code's own NOTE comments say cross-symbol sector-rotation and
market-breadth data aren't wired into the per-symbol pipeline yet). This
was found and documented in this conversation's Phase 4 delivery notes
already — not new, but the critique's framing ("never fabricate 50 as
if neutral, mark it unavailable") is the right fix and hasn't been done.

**Advantage**: an explicit `status="UNAVAILABLE"` is strictly more honest
than a silent 50.0 that downstream code can't distinguish from "measured
and genuinely neutral."
**Disadvantage**: every consumer of `sector_score`/`breadth` (there are
several — Tier 2 legacy checks, Tier 3 blends in both engines, the
scoring engines) needs to handle an "unavailable" sentinel gracefully
instead of just doing arithmetic on a float — more touch points than it
first looks like, though each individual change is small.

---

### 9. Relative Strength naming
**Verdict: CONFIRMED, and it's dead code on top of being mislabeled.**

`features/indicators/breakout.py:74`:
```python
df["relative_strength"] = df["close"] / df["close"].rolling(20).mean()
```
This is literally price vs. its own 20-day mean — not relative to a
benchmark or sector index. **Bonus finding**: it's also never consumed
by any check in `buy_strategy.py`/`sell_strategy.py`/scoring — it only
flows into `diagnostics["relative_strength"]` for the CSV report. So
today it's a mislabeled, unused diagnostic column, not something
influencing any decision.

**Advantage**: renaming costs nothing and removes a misleading label from
reports immediately.
**Disadvantage**: building genuine relative strength (`stock_return_N /
benchmark_return_N`) needs a NIFTY/sector-index return series available
alongside every symbol's own series — not currently wired into
`DataEngine`/`MarketScanner` at all (same underlying gap noted in #8 —
no cross-symbol/index data pipeline exists yet in this architecture).

---

### 10 / 16. Volume-pressure model
**Verdict: reasonable proposal, current state is "individual signals exist, no composite."**

`volume`, `volume_spike`, `obv`, `cmf`, `mfi` all exist today as
separate checks (grouped into Phase 4's "Volume" factor — core:
`volume_spike`, `obv`, `accumulation`; diagnostic: `volume`, `cmf`,
`mfi`). Delivery % and "price-location-in-candle" do NOT exist anywhere
in the codebase (verified — no delivery-percentage field, no candle-close-
position-within-range calculation found).

**Advantage**: delivery % (NSE actually publishes this) is a genuinely
different signal from OBV/CMF (which are volume*price-direction proxies,
not settlement data) — real incremental information, not another
same-idea duplicate.
**Disadvantage**: this again needs a new data source
(`data/delivery_data.py` already exists in this repo per earlier work in
this conversation — worth checking whether it's already fetching
delivery % before assuming it needs to be built from scratch). The
critique's own caution — "don't pretend CMF = institutional buying, real
order-flow needs tick data you don't have" — is correct and should be
taken seriously: this composite would still be a same-timeframe-OHLCV
proxy, not real order flow, regardless of how it's packaged.

---

### 11. No-news confidence
**Verdict: MIXED — one implementation already correct, another still has the bug.**

`strategy/buy_strategy.py`/`sell_strategy.py`'s Tier 3 blend already does
this correctly: when `has_news` is False, news gets **zero weight**, not
a neutral-50 dilution — its weight redistributes to fundamentals+market
(`tier3_score = fundamental*(0.55/0.70) + market*(0.15/0.70)`, no news
term at all). This was explicitly built this way in this conversation.

`strategy/buy_scoring.py`'s `_confidence()` (and `sell_scoring.py`'s
mirror) is where the critique is still accurate:
```python
values = np.array([result.technical, result.fundamental, result.news, ...])
mean = values.mean()
consistency = 100 - values.std()
```
`result.news = 0.0` when there's no news (confirmed: `buy_scoring.py`
sets `result.news = self._normalize(news_score) if has_news else 0.0`).
This raw 0.0 goes straight into the mean/std calculation **without
exclusion** — even though `result.overall`'s WEIGHTED SUM correctly
zeroes news's weight, the separate `_confidence()` calculation still
gets dragged down by a fabricated 0.0 sitting in an 8-value array. So
"no news" does penalize *confidence* today, even though it doesn't
penalize the *score* — a real, narrower bug than the original framing,
but real.

**Advantage**: fixing `_confidence()` to exclude unavailable components
(same has_news-style exclusion already used for `result.overall`) is
small and consistent with a pattern already proven to work in this
codebase.
**Disadvantage**: essentially none — this is a contained, low-risk fix,
one of the cheaper items on this list.

---

### 12. Overextension (crude % distance)
**Verdict: CONFIRMED, exact match.**

`strategy/buy_strategy.py`: `OVEREXTENSION_CAP_PERCENT = 8.0`,
`extension_percent = (close-ema20)/ema20*100`, hard cap at 8% — a single
fixed percentage, the same for every stock regardless of its own
volatility. No ATR-normalization, no breakout-age tracking, no
volume-decay-after-breakout tracking exist anywhere in the codebase
(verified via search).

**Advantage**: an ATR-normalized, multi-factor "Entry Distance Model" is
strictly better trading logic — a low-volatility stock 8% from EMA20 and
a high-volatility stock 8% from EMA20 are not equally extended, and a
fixed percentage can't tell them apart.
**Disadvantage**: needs `breakout_age` (days-since-signal tracking,
doesn't exist today — would need to persist state across scan days per
symbol) and volume-decay tracking (needs a short lookback window, doable
but new). Moderate scope — bigger than #6/#11, smaller than #1/#4/#5.

---

## P2

### 13. Multi-timeframe — "don't add yet"
**Verdict: agree with the caution, and found something worse than "not built yet."**

There already IS a `features/multi_timeframe.py` /
`MultiTimeframeEngine` in the codebase — but it's not real MTF. It
computes `mtf_sma_20/50/200` as **rolling means of the SAME daily
close series** (`features/multi_timeframe.py:44-46`) — mathematically
near-identical to the existing `sma_20/50/200` already used in Tier 1,
just relabeled "mtf_". No weekly/4H resampling happens anywhere. And
verified via search: **`mtf_sma_*`/`mtf_trend` are computed every scan
and consumed by nothing** — completely dead output.

This actually strengthens the "don't add yet" recommendation: the
existing attempt at MTF is both fake (same timeframe, different label)
and pointless (unused). A real MTF implementation would need to replace
this, not extend it.

**Advantage of leaving it alone for now**: correct — real MTF needs
weekly/4H OHLCV resampling wired through the whole `DataEngine` →
`FeatureEngineeringEngine` chain, a bigger lift than anything else on
this list except #1/#4/#5.
**Disadvantage of the CURRENT state**: the fake `MultiTimeframeEngine`
running every scan for zero benefit is wasted computation and a
misleading module name sitting in the codebase — worth deleting or
clearly marking as unused, independent of whether real MTF ever gets
built.

---

### 15. State-based structure over raw check counts
**Verdict: valid explainability critique, not yet implemented.**

Current reason strings genuinely do look like
`"Technical confirmation: 32/40"` / `"Weighted score: 86.55/100"` (exact
strings from `buy_strategy.py`, confirmed) — accurate description of
today's output. Phase 4 added factor-level breakdown
(`"Tier2 factors [TRENDING]: Trend=83 Momentum=67 Volume=100
Volatility=50"`) which is a step in this direction, but still numeric,
not the proposed `MarketState=BULL / TrendState=UPTREND /
SetupState=PULLBACK / EntryState=TRIGGERED` typed-state narrative.

**Advantage**: genuinely more readable for a human deciding whether to
trust a signal, especially useful in exactly the GitHub Actions logs
this conversation has been optimizing for (Phase 3's exit-engine
logging, Phase 4's factor logging) — this is a natural next step in that
same direction.
**Disadvantage**: mostly a presentation-layer change if built on TOP of
the existing checks (low risk), but if it's meant to REPLACE scoring
with hard state-transition rules (as implied by point #5's "NO TRADE
even if score=92" example), then it's the same scope/risk as #5, not a
separate small task.

---

## Overall assessment

**What's already handled by this conversation's prior work** (Phases
2-5): the specific MACD duplicate (#7), the missing-fundamentals
scoring (#3, partially), the sell-side market inversion (#2, partially —
1 of 4 places), early-entry/overextension checks existing at all (#5,
partially), the sector/breadth placeholder problem being documented
(#8, found not fixed).

**Genuinely new findings from this fact-check, not previously known**:
the Kelly/win-probability contract violation (#1), the VIX/FII-DII
live-data leakage into backtests (#4 — this is on code from THIS
conversation, Phase 3), the forced-1-share risk override (#6), the
`_confidence()` no-news penalty (#11, narrower than described but real),
and the fake/unused MTF module (#13, worse than "not built").

**Honest pushback on framing**: "convert to mature production solution
without new fancy modules" doesn't quite hold for #1 (needs a trade-
outcome calibration store), #4 (needs a real historical VIX/FII-DII/
fundamentals data source — and this environment has already proven it
can't reach the vendors needed to build one), #5 (needs new state
dataclasses and a new decision layer), and #10 (needs delivery-%/candle-
position data). Those four are legitimately new infrastructure, not
refactors — worth sequencing separately from the smaller, contained
fixes (#6, #9's rename, #11, #12, #13's cleanup) which could land
quickly with the same rigor as Phases 2-5.

**Cheapest, highest-value fixes if prioritizing by effort vs. risk**:
#6 (position sizing 1-share override — small, pure risk-control win),
#11 (`_confidence()` no-news exclusion — small, consistent with existing
pattern), #2's remaining 3 inconsistent spots (medium, but the pattern
to copy already exists in sell_strategy.py's tier3), #9's rename +
dead-code note, #13's dead-module cleanup.
