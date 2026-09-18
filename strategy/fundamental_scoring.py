"""
FUNDAMENTAL SCORING (shared)

A single, weighted, bidirectional fundamental health score used across
buy_strategy / sell_strategy / buy_scoring / sell_scoring, instead of each
file duplicating its own all-or-nothing AND condition.

buy_fundamental_score(): 0-100, higher = more BUY-favorable (healthy company)
sell_fundamental_score(): 0-100, higher = more SELL-favorable (weak company)

sell_fundamental_score() is simply 100 - buy_fundamental_score() variant
tuned around the same metrics, so "strong fundamentals" and "weak
fundamentals" are always mirror images of each other rather than two
independently-tuned (and potentially inconsistent) rule sets.

MISSING DATA HANDLING — a real-data audit of the full watchlist (500
NSE symbols via yfinance) found roe/peg/operating_cashflow missing for
84.8% / 89.8% / 90.8% of symbols respectively (pe/pb/debt_to_equity are
much rarer gaps: 4.2% / 0% / 8.4%). The OLD version defaulted a missing
metric to a value (999.0 for the ratio metrics, 0.0 for the
growth/roe/cash metrics) that then fed the SAME quality thresholds a
real bad reading would — meaning "we don't know this company's ROE"
and "this company's ROE is genuinely 0" scored identically (and for
ROE specifically, defaulting to 0.0 meant a MISSING reading scored
WORSE than every other missing metric: literally 0/20 credit, not even
the ~30% partial credit the other defaults gave).

Each metric below is now computed ONLY from what's actually present
(`fundamentals.get(key) is not None`) — a missing metric is excluded
from both the weighted numerator AND the weight denominator, so its
weight is redistributed across whatever metrics ARE present, instead
of being defaulted to a value and penalized as if it were a real bad
reading. If literally nothing is present for a stock, the score is
0.0 — a genuinely unscored stock, not a fabricated neutral/bad number.

COVERAGE — the exclude-and-renormalize approach above avoids the worst
version of "missing = bad", but a plain 0-100 score alone still can't
tell a caller "72/100 built from 8/8 metrics" apart from "72/100 built
from 1/8 metrics" — the second is far less trustworthy even though the
number looks identical (found during an architecture review). Both
score AND coverage are now available via *_fundamental_evaluation()
(returns a FundamentalEvidence with score/coverage/available_metrics/
total_metrics) — buy_fundamental_score()/sell_fundamental_score() stay
exactly as before (unchanged behavior, unchanged callers: buy_strategy.
py, buy_scoring.py, risk/exit_engine.py, etc. all keep working with no
changes needed) and are now thin wrappers around the new evaluation
functions. Exposing coverage is deliberately NOT paired with any new
gating/rejection logic here — what a caller should DO with low coverage
(reject the check? downweight further? just log it?) is a policy
decision for whoever consumes fundamental_coverage, not something this
module decides unilaterally.

STRUCTURAL BUY BIAS FIX (2026-09-18) — sell_fundamental_score()'s
"100 - buy_score" mirror is mathematically a mirror around the
midpoint 50, but a real stock universe's average buy_fundamental_score
is NOT 50 — measured ~68/100 across the live NSE watchlist, because
most listed companies have "decent, not great" fundamentals (normal;
fundamentals update quarterly, they don't track daily price action).
That single fact meant the average stock's sell_fundamental_score sat
at ~32/100 — structurally ~18 points below QUALIFY_THRESHOLD's
midpoint-neutral assumption, suppressing SELL qualification regardless
of how much a stock's PRICE was actually falling. Confirmed in
production: 148 BUY vs only 8 SELL entries during a real one-sided
NSE decline (2026-09-11 to 2026-09-16), losing ~₹15,000 in that one
week alone.

buy_fundamental_relative_evaluation()/sell_fundamental_relative_evaluation()
fix this at the root: instead of mirroring around a fixed, wrong
midpoint of 50, they rank a stock's raw buy_fundamental_score against
the ACTUAL distribution of buy_fundamental_score() values across every
symbol scanned that same day (see execution/scanner.py's pre-pass,
which builds this list once per run and threads it through). A company
at the population's own median now scores ~50 on EITHER side, whatever
the population's raw average happens to be — removing the structural
skew while still correctly rewarding genuinely-above/below-average
fundamentals. Passing no distribution (universe_buy_scores=None, the
default) falls back to the plain absolute score unchanged, so every
existing caller/test that predates this fix keeps its exact old
behavior — this is purely additive, not a replacement of
buy_fundamental_score()/sell_fundamental_score() (which are unchanged
above) or buy_fundamental_evaluation()/sell_fundamental_evaluation().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _present_float(fundamentals: dict[str, Any], key: str) -> float | None:
    """The metric's float value, or None if genuinely absent. Unlike a
    default-substituting helper, this lets callers tell "missing" apart
    from "present and zero/negative" — the distinction the old
    _safe_float()-based version couldn't make."""
    value = fundamentals.get(key)
    return float(value) if value is not None else None


# Per-metric weight (out of 100 when everything is present) and quality
# function (maps the metric's real value to a 0.0-1.0 "how healthy is
# this reading" fraction). Quality thresholds are UNCHANGED from the
# original version — only how a MISSING metric is handled has changed.
_REVENUE_WEIGHT = 15
_EARNINGS_WEIGHT = 15
_ROE_WEIGHT = 20
_PE_WEIGHT = 10
_PB_WEIGHT = 10
_PEG_WEIGHT = 10
_DEBT_WEIGHT = 10
_CASH_WEIGHT = 10

_METRICS: list[tuple[str, int, Any]] = [
    ("revenue_growth", _REVENUE_WEIGHT, lambda v: 1.0 if v > 0 else 0.3),
    ("earnings_growth", _EARNINGS_WEIGHT, lambda v: 1.0 if v > 0 else 0.3),
    ("roe", _ROE_WEIGHT, lambda v: min(max(v / 20.0, 0.0), 1.0)),
    ("pe", _PE_WEIGHT, lambda v: 1.0 if 0 < v < 30 else 0.3),
    ("pb", _PB_WEIGHT, lambda v: 1.0 if 0 < v < 5 else 0.3),
    ("peg", _PEG_WEIGHT, lambda v: 1.0 if 0 < v < 2 else 0.3),
    ("debt_to_equity", _DEBT_WEIGHT, lambda v: 1.0 if 0 <= v < 1.5 else 0.2),
    ("operating_cashflow", _CASH_WEIGHT, lambda v: 1.0 if v > 0 else 0.2),
]

_TOTAL_WEIGHT = sum(weight for _, weight, _ in _METRICS)  # 100, all metrics present


@dataclass(slots=True, frozen=True)
class FundamentalEvidence:
    """score: same 0-100 number buy_fundamental_score()/
    sell_fundamental_score() return. coverage: 0.0-1.0, the fraction of
    the total possible weight actually backed by present data (1.0 =
    all 8 metrics present, 0.0 = none). available_metrics/total_metrics:
    the same thing as a raw count, for human-readable logging."""

    score: float
    coverage: float
    available_metrics: int
    total_metrics: int


def _evaluate(fundamentals: dict[str, Any]) -> FundamentalEvidence:
    weighted_sum = 0.0
    weight_present = 0.0
    available_metrics = 0

    for key, weight, quality_fn in _METRICS:
        value = _present_float(fundamentals, key)
        if value is None:
            continue
        weighted_sum += weight * quality_fn(value)
        weight_present += weight
        available_metrics += 1

    if weight_present == 0.0:
        score = 0.0
    else:
        score = round(min(max((weighted_sum / weight_present) * 100.0, 0.0), 100.0), 2)

    coverage = round(weight_present / _TOTAL_WEIGHT, 4)

    return FundamentalEvidence(
        score=score,
        coverage=coverage,
        available_metrics=available_metrics,
        total_metrics=len(_METRICS),
    )


def buy_fundamental_evaluation(fundamentals: dict[str, Any]) -> FundamentalEvidence:
    """Full evidence (score + coverage) for a BUY-direction read. Use
    this instead of buy_fundamental_score() when the caller can act on
    (or at least log/report) how much data the score is actually based
    on, not just the blended number itself."""
    return _evaluate(fundamentals)


def buy_fundamental_score(fundamentals: dict[str, Any]) -> float:
    """0-100 weighted fundamental health score, computed only from the
    metrics actually present for this stock (see module docstring for
    why). Returns 0.0 if nothing at all is present.

    Unchanged behavior from before FundamentalEvidence/coverage was
    added — existing callers that only need the score (buy_strategy.py,
    buy_scoring.py, risk/exit_engine.py) need no changes."""
    return buy_fundamental_evaluation(fundamentals).score


def sell_fundamental_evaluation(fundamentals: dict[str, Any]) -> FundamentalEvidence:
    """Mirror of buy_fundamental_evaluation(): score is 100-buy_score
    (see sell_fundamental_score()'s docstring for the fully-missing-data
    caveat). coverage/available_metrics/total_metrics are IDENTICAL
    either direction — same metrics, same presence/absence; direction
    only changes how a PRESENT value is judged, not which values are
    present, so there's nothing to invert about coverage itself."""
    buy = buy_fundamental_evaluation(fundamentals)
    return FundamentalEvidence(
        score=round(100.0 - buy.score, 2),
        coverage=buy.coverage,
        available_metrics=buy.available_metrics,
        total_metrics=buy.total_metrics,
    )


def sell_fundamental_score(fundamentals: dict[str, Any]) -> float:
    """0-100, higher = weaker company = more SELL-favorable. Mirror of
    buy_fundamental_score() rather than an independently tuned rule set.

    Note on the fully-missing case: buy_fundamental_score() returning
    0.0 for "nothing present" mirrors here to 100.0 ("maximally
    SELL-favorable"). That is a real, deliberate side-effect of keeping
    this a strict mirror rather than an independently-tuned rule set —
    "we have zero data on this company" is being treated as maximally
    bearish, not neutral, on the SELL side. Flagging this explicitly
    since it wasn't separately specified; if that reads as too strong
    once seen in practice, sell_fundamental_score() would need its own
    explicit zero-data case rather than inheriting the mirror.

    Unchanged behavior from before FundamentalEvidence/coverage was
    added — existing callers that only need the score need no changes.
    """
    return sell_fundamental_evaluation(fundamentals).score


def fundamental_percentile_rank(score: float, universe_scores: list[float]) -> float:
    """0-100 percentile rank of `score` within `universe_scores` (a real
    population of buy_fundamental_score() values — typically every
    symbol scanned in the same run), using "fraction strictly below,
    plus half of exact ties" so a score sitting at the population's own
    median lands at ~50 regardless of where the population's raw scores
    happen to cluster.

    `universe_scores` must be non-empty — callers with no population to
    rank against should use the absolute score directly instead of
    calling this (see buy_fundamental_relative_evaluation()'s
    universe_buy_scores=None fallback, which does exactly that)."""
    n = len(universe_scores)
    below = sum(1 for s in universe_scores if s < score)
    tied = sum(1 for s in universe_scores if s == score)
    return round((below + 0.5 * tied) / n * 100.0, 2)


@dataclass(slots=True, frozen=True)
class RelativeFundamentalEvidence:
    """Like FundamentalEvidence, but `score` is a PERCENTILE RANK against
    a real universe distribution (0-100; 50 = exactly average for
    today's scanned watchlist) instead of a fixed absolute reading.
    `raw_score` keeps the original absolute 0-100 score (what
    FundamentalEvidence.score would have been) purely for
    diagnostics/reporting — no live check should gate on raw_score,
    only on score."""

    score: float
    raw_score: float
    coverage: float
    available_metrics: int
    total_metrics: int


def buy_fundamental_relative_evaluation(
    fundamentals: dict[str, Any],
    universe_buy_scores: list[float] | None = None,
) -> RelativeFundamentalEvidence:
    """buy_fundamental_evaluation(), re-expressed as this stock's
    percentile rank within `universe_buy_scores` — see this module's
    STRUCTURAL BUY BIAS FIX docstring note for why: ranking against the
    population's own actual distribution (instead of an assumed, wrong
    midpoint of 50) removes the structural bias that comes from most
    real companies having above-midpoint fundamentals.

    universe_buy_scores=None or [] (the default) means no population is
    available — e.g. a single-symbol diagnostic call, or any existing
    test written before this fix that only cares about one stock in
    isolation. In that case this returns the plain ABSOLUTE score
    unchanged (raw_score == score), so every caller that doesn't pass a
    distribution keeps its exact prior behavior, unmodified."""
    absolute = buy_fundamental_evaluation(fundamentals)
    if not universe_buy_scores:
        percentile = absolute.score
    else:
        percentile = fundamental_percentile_rank(absolute.score, universe_buy_scores)
    return RelativeFundamentalEvidence(
        score=percentile,
        raw_score=absolute.score,
        coverage=absolute.coverage,
        available_metrics=absolute.available_metrics,
        total_metrics=absolute.total_metrics,
    )


def sell_fundamental_relative_evaluation(
    fundamentals: dict[str, Any],
    universe_buy_scores: list[float] | None = None,
) -> RelativeFundamentalEvidence:
    """Mirror of buy_fundamental_relative_evaluation(): score is
    100-percentile, so a company sitting at the population's own median
    lands at ~50 on the SELL side too (not the ~32 the old 100-x-on-a
    ~68-average mirror produced). universe_buy_scores is the SAME
    BUY-direction distribution passed to the BUY side — SELL doesn't
    need its own separate population; a stock's relative fundamental
    standing is one fact, BUY/SELL only decide which way "good"
    points."""
    buy_relative = buy_fundamental_relative_evaluation(fundamentals, universe_buy_scores)
    return RelativeFundamentalEvidence(
        score=round(100.0 - buy_relative.score, 2),
        raw_score=round(100.0 - buy_relative.raw_score, 2),
        coverage=buy_relative.coverage,
        available_metrics=buy_relative.available_metrics,
        total_metrics=buy_relative.total_metrics,
    )
