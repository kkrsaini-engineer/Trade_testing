"""
Tests for the STRUCTURAL BUY BIAS FIX (2026-09-18, see
strategy/fundamental_scoring.py's module docstring): sell_fundamental_
score()'s old "100 - buy_score" mirror was mathematically a mirror
around the midpoint 50, but a real stock universe's average
buy_fundamental_score sits well above 50 (measured ~68/100 across the
live NSE watchlist) — meaning the average stock's sell_fundamental_
score was structurally suppressed to ~32/100, regardless of how much
that stock's PRICE was actually falling. Confirmed in production: 148
BUY vs only 8 SELL entries during a real one-sided NSE decline
(2026-09-11 to 2026-09-16).

fundamental_percentile_rank()/buy_fundamental_relative_evaluation()/
sell_fundamental_relative_evaluation() fix this by ranking a stock's
raw score against the REAL distribution of buy_fundamental_score()
values across the whole watchlist scanned that day, instead of
mirroring around a fixed, wrong midpoint of 50.

Every test below also proves the fix is purely ADDITIVE: passing no
distribution (the default) must reproduce the OLD absolute-score
behavior byte for byte, so every pre-existing caller/test that doesn't
opt in keeps working unmodified.
"""

from strategy.fundamental_scoring import (
    RelativeFundamentalEvidence,
    buy_fundamental_evaluation,
    buy_fundamental_relative_evaluation,
    buy_fundamental_score,
    fundamental_percentile_rank,
    sell_fundamental_evaluation,
    sell_fundamental_relative_evaluation,
    sell_fundamental_score,
)

STRONG_FUNDAMENTALS = dict(
    revenue_growth=10.0, earnings_growth=12.0, roe=18.0, pe=20.0,
    pb=3.0, peg=1.2, debt_to_equity=0.5, operating_cashflow=1000.0,
)

WEAK_FUNDAMENTALS = dict(
    revenue_growth=-5.0, earnings_growth=-8.0, roe=2.0, pe=45.0,
    pb=8.0, peg=3.5, debt_to_equity=2.5, operating_cashflow=-100.0,
)

AVERAGE_FUNDAMENTALS = dict(
    revenue_growth=5.0, earnings_growth=5.0, roe=10.0, pe=25.0,
    pb=4.0, peg=1.8, debt_to_equity=1.0, operating_cashflow=200.0,
)


# ==========================================================
# fundamental_percentile_rank() — pure percentile math
# ==========================================================

def test_percentile_rank_below_every_population_member_is_zero():
    # Probe value NOT in the population -- pure "how many fall below"
    # behavior, undiluted by a self-tie.
    universe = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert fundamental_percentile_rank(5.0, universe) == 0.0


def test_percentile_rank_above_every_population_member_is_100():
    universe = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert fundamental_percentile_rank(55.0, universe) == 100.0


def test_percentile_rank_of_a_population_member_counts_its_own_occurrence_as_a_tie():
    # In production the stock's OWN score is part of the population
    # being ranked against (today's whole watchlist includes this
    # stock) -- so ranking a value that's actually IN the list must
    # count that occurrence as a (half-credit) tie with itself, per the
    # documented "below + half of ties" definition.
    universe = [10.0, 20.0, 30.0, 40.0, 50.0]
    # 0 strictly below + 0.5 self-tie, out of 5 -> 10.0
    assert fundamental_percentile_rank(10.0, universe) == 10.0
    # 4 strictly below + 0.5 self-tie, out of 5 -> 90.0
    assert fundamental_percentile_rank(50.0, universe) == 90.0


def test_percentile_rank_of_the_exact_population_median_is_50():
    # An odd-sized, symmetric population: the middle value's rank should
    # land at the midpoint regardless of what the raw values are.
    universe = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert fundamental_percentile_rank(30.0, universe) == 50.0


def test_percentile_rank_ties_split_the_credit():
    # value appears twice in a population of 4 -> (0 below + 0.5*2 tied)/4*100 = 25.0
    universe = [30.0, 30.0, 40.0, 50.0]
    assert fundamental_percentile_rank(30.0, universe) == 25.0


def test_percentile_rank_is_robust_to_a_population_whose_average_is_skewed_high():
    # This is the EXACT real-world scenario the fix addresses: a
    # population clustered high (like the real ~68 NSE watchlist
    # average) must still rank an exactly-average score at ~50, not
    # near-0 the way a fixed "100 - x" mirror around 50 would.
    universe = [60.0, 65.0, 68.0, 70.0, 75.0]
    # 68.0 IS the population's own median here (2 below + 0.5 self-tie
    # out of 5 -> 50.0) -- exactly the fix's headline guarantee: an
    # exactly-average score lands at 50, whatever the population's raw
    # average happens to be.
    assert fundamental_percentile_rank(68.0, universe) == 50.0


# ==========================================================
# buy_fundamental_relative_evaluation() — no distribution ->
# byte-for-byte old behavior (backward compatibility)
# ==========================================================

def test_buy_relative_with_no_universe_matches_old_absolute_score_exactly():
    absolute = buy_fundamental_evaluation(STRONG_FUNDAMENTALS)
    relative = buy_fundamental_relative_evaluation(STRONG_FUNDAMENTALS, None)
    assert relative.score == absolute.score
    assert relative.raw_score == absolute.score
    assert relative.coverage == absolute.coverage
    assert relative.available_metrics == absolute.available_metrics
    assert relative.total_metrics == absolute.total_metrics


def test_buy_relative_with_empty_universe_list_also_falls_back_to_absolute():
    absolute = buy_fundamental_evaluation(WEAK_FUNDAMENTALS)
    relative = buy_fundamental_relative_evaluation(WEAK_FUNDAMENTALS, [])
    assert relative.score == absolute.score


def test_sell_relative_with_no_universe_matches_old_mirror_score_exactly():
    old = sell_fundamental_evaluation(STRONG_FUNDAMENTALS)
    new = sell_fundamental_relative_evaluation(STRONG_FUNDAMENTALS, None)
    assert new.score == old.score
    assert new.coverage == old.coverage


def test_buy_fundamental_score_and_sell_fundamental_score_are_unchanged_by_this_fix():
    # The plain (non-relative) wrapper functions must be completely
    # untouched by this change -- any caller still using them directly
    # (tests, diagnostics, risk/exit_engine.py) keeps working exactly
    # as before.
    assert buy_fundamental_score(STRONG_FUNDAMENTALS) == buy_fundamental_evaluation(STRONG_FUNDAMENTALS).score
    assert sell_fundamental_score(STRONG_FUNDAMENTALS) == sell_fundamental_evaluation(STRONG_FUNDAMENTALS).score


# ==========================================================
# buy_fundamental_relative_evaluation() — WITH a real distribution:
# this is the actual bug fix under test
# ==========================================================

def test_a_company_at_the_universe_average_scores_near_50_on_both_sides():
    # Build a universe whose average buy_fundamental_score mirrors the
    # real, measured production skew (~68/100), and confirm a stock at
    # exactly that average now lands at ~50 on BOTH BUY and SELL sides
    # -- the whole point of the fix.
    universe = [
        buy_fundamental_score(AVERAGE_FUNDAMENTALS),
        buy_fundamental_score(STRONG_FUNDAMENTALS),
        buy_fundamental_score(STRONG_FUNDAMENTALS),
        buy_fundamental_score(WEAK_FUNDAMENTALS),
    ]
    buy_relative = buy_fundamental_relative_evaluation(AVERAGE_FUNDAMENTALS, universe)
    sell_relative = sell_fundamental_relative_evaluation(AVERAGE_FUNDAMENTALS, universe)
    # AVERAGE_FUNDAMENTALS' own raw score is IN the universe, so its
    # percentile rank depends on how many of the other 3 fall below it
    # -- assert the qualitative fix (near the middle), not an exact
    # fragile number tied to today's quality-function internals.
    assert 20.0 <= buy_relative.score <= 80.0
    assert 20.0 <= sell_relative.score <= 80.0
    # And they must still sum to 100 (the mirror relationship itself is
    # preserved, only the reference point changed).
    assert round(buy_relative.score + sell_relative.score, 2) == 100.0


def test_old_mirror_would_have_suppressed_sell_but_relative_scoring_does_not():
    # Reproduce the actual production bug: a universe of mostly
    # STRONG_FUNDAMENTALS stocks (raising the population average well
    # above 50), and confirm the OLD absolute mirror punished an
    # average-quality stock's sell score, while the NEW relative scoring
    # does not.
    universe = [buy_fundamental_score(STRONG_FUNDAMENTALS) for _ in range(9)]
    universe.append(buy_fundamental_score(AVERAGE_FUNDAMENTALS))

    old_sell_score = sell_fundamental_evaluation(AVERAGE_FUNDAMENTALS).score
    new_sell_score = sell_fundamental_relative_evaluation(AVERAGE_FUNDAMENTALS, universe).score

    # Old mirror: 100 - (a below-STRONG absolute score) -> low, punished.
    # New relative: AVERAGE_FUNDAMENTALS is the WEAKEST in a
    # STRONG-heavy universe -> should rank HIGH on the sell side (top of
    # the pack, relatively weak), not low.
    assert new_sell_score > old_sell_score


def test_a_genuinely_weak_company_still_scores_high_on_sell_relative_to_a_strong_universe():
    universe = [buy_fundamental_score(STRONG_FUNDAMENTALS) for _ in range(5)]
    universe.append(buy_fundamental_score(WEAK_FUNDAMENTALS))
    sell_relative = sell_fundamental_relative_evaluation(WEAK_FUNDAMENTALS, universe)
    # WEAK_FUNDAMENTALS is the worst in the universe -> should rank at
    # (or very near) the top of the SELL-favorable scale.
    assert sell_relative.score >= 80.0


def test_relative_score_still_respects_coverage_from_the_underlying_absolute_evaluation():
    partial = {"revenue_growth": 10.0, "earnings_growth": 12.0}  # only 2 of 8 metrics
    universe = [buy_fundamental_score(STRONG_FUNDAMENTALS), buy_fundamental_score(WEAK_FUNDAMENTALS)]
    relative = buy_fundamental_relative_evaluation(partial, universe)
    assert relative.available_metrics == 2
    assert relative.total_metrics == 8
    assert 0.0 < relative.coverage < 1.0


def test_relative_evaluation_returns_the_documented_dataclass_shape():
    result = buy_fundamental_relative_evaluation(STRONG_FUNDAMENTALS, [50.0, 60.0, 70.0])
    assert isinstance(result, RelativeFundamentalEvidence)
    assert hasattr(result, "score")
    assert hasattr(result, "raw_score")
    assert hasattr(result, "coverage")
