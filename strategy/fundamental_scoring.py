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
"""

from __future__ import annotations

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


def buy_fundamental_score(fundamentals: dict[str, Any]) -> float:
    """0-100 weighted fundamental health score, computed only from the
    metrics actually present for this stock (see module docstring for
    why). Returns 0.0 if nothing at all is present."""

    weighted_sum = 0.0
    weight_present = 0.0

    for key, weight, quality_fn in _METRICS:
        value = _present_float(fundamentals, key)
        if value is None:
            continue
        weighted_sum += weight * quality_fn(value)
        weight_present += weight

    if weight_present == 0.0:
        return 0.0

    score = (weighted_sum / weight_present) * 100.0
    return round(min(max(score, 0.0), 100.0), 2)


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
    """
    return round(100.0 - buy_fundamental_score(fundamentals), 2)
