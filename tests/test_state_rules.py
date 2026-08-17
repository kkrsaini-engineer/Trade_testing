"""
Tests for decision/state_rules.py's evaluate_entry_state() — the shared,
ordered, first-match-wins entry-state rule table (Point 15,
PHASE29_NOTES.md) both strategy/buy_strategy.py and
strategy/sell_strategy.py now call to decide `qualified`/`entry_state`
together, instead of computing them independently and risking silent
disagreement.

Covers every rule in priority order, the WAITING/TRIGGERED boundary, and
that the SAME function correctly serves both directions via its
direction-specific parameters (mirrors BUY/SELL per the standing rule —
this module itself is direction-agnostic, so "mirroring" here means
proving both parameterizations work, not duplicating test logic).
"""

from decision.state_rules import (
    REJECTED_OVEREXTENDED,
    REJECTED_STALE_ENTRY,
    REJECTED_TREND,
    TRIGGERED,
    WAITING,
    evaluate_entry_state,
)


def _evaluate(**overrides):
    kwargs = dict(
        trend_state="UPTREND",
        unfavorable_trend_state="DOWNTREND",
        tier1_passed=True,
        setup_state="NONE",
        stale_setup_state="STALE_BREAKOUT",
        stale_reason="stale reason",
        not_overextended=True,
        overextended_reason="overextended reason",
        overall_score=80.0,
        qualify_threshold=58.0,
    )
    kwargs.update(overrides)
    return evaluate_entry_state(**kwargs)


def test_everything_favorable_triggers():
    result = _evaluate()
    assert result.entry_state == TRIGGERED
    assert result.qualified is True
    assert result.reject_reason is None


def test_below_threshold_waits_not_a_hard_reject():
    result = _evaluate(overall_score=50.0)
    assert result.entry_state == WAITING
    assert result.qualified is False
    assert result.reject_reason is None


def test_score_exactly_at_threshold_triggers():
    result = _evaluate(overall_score=58.0, qualify_threshold=58.0)
    assert result.entry_state == TRIGGERED
    assert result.qualified is True


def test_unfavorable_trend_state_hard_rejects_even_with_high_score():
    # tier1_passed=True and score=95 -- the trend-state contradiction
    # this rule specifically closes (Point 15's core finding).
    result = _evaluate(trend_state="DOWNTREND", overall_score=95.0)
    assert result.entry_state == REJECTED_TREND
    assert result.qualified is False
    assert "TrendState=DOWNTREND" in result.reject_reason
    assert "regardless of score" in result.reject_reason


def test_tier1_failed_hard_rejects_even_with_favorable_trend_state():
    result = _evaluate(tier1_passed=False, overall_score=95.0)
    assert result.entry_state == REJECTED_TREND
    assert result.qualified is False
    assert result.reject_reason == "Rejected: core trend not confirmed (Tier 1), regardless of score."


def test_stale_setup_hard_rejects_even_with_high_score():
    result = _evaluate(setup_state="STALE_BREAKOUT", overall_score=95.0)
    assert result.entry_state == REJECTED_STALE_ENTRY
    assert result.qualified is False
    assert result.reject_reason == "stale reason"


def test_overextended_hard_rejects_even_with_high_score():
    result = _evaluate(not_overextended=False, overall_score=95.0)
    assert result.entry_state == REJECTED_OVEREXTENDED
    assert result.qualified is False
    assert result.reject_reason == "overextended reason"


def test_priority_order_unfavorable_trend_beats_everything_else():
    # ALL other hard-reject conditions are also true here -- the
    # unfavorable-trend rule must still win (it's checked first).
    result = _evaluate(
        trend_state="DOWNTREND", tier1_passed=False,
        setup_state="STALE_BREAKOUT", not_overextended=False,
    )
    assert result.entry_state == REJECTED_TREND


def test_priority_order_stale_beats_overextended():
    result = _evaluate(setup_state="STALE_BREAKOUT", not_overextended=False)
    assert result.entry_state == REJECTED_STALE_ENTRY


def test_priority_order_overextended_beats_score_threshold():
    result = _evaluate(not_overextended=False, overall_score=10.0)
    assert result.entry_state == REJECTED_OVEREXTENDED


# ==========================================================
# SAME function correctly serves BOTH directions via its parameters —
# BUY passes unfavorable_trend_state="DOWNTREND"/stale="STALE_BREAKOUT",
# SELL passes the exact opposite. Neither direction is hardcoded inside
# state_rules.py itself.
# ==========================================================

def test_buy_direction_parameterization_rejects_downtrend():
    result = evaluate_entry_state(
        trend_state="DOWNTREND", unfavorable_trend_state="DOWNTREND",
        tier1_passed=True, setup_state="NONE", stale_setup_state="STALE_BREAKOUT",
        stale_reason="stale", not_overextended=True, overextended_reason="ext",
        overall_score=90.0, qualify_threshold=58.0,
    )
    assert result.entry_state == REJECTED_TREND


def test_sell_direction_parameterization_rejects_uptrend():
    result = evaluate_entry_state(
        trend_state="UPTREND", unfavorable_trend_state="UPTREND",
        tier1_passed=True, setup_state="NONE", stale_setup_state="STALE_BREAKDOWN",
        stale_reason="stale", not_overextended=True, overextended_reason="ext",
        overall_score=90.0, qualify_threshold=58.0,
    )
    assert result.entry_state == REJECTED_TREND


def test_sell_direction_parameterization_triggers_on_downtrend():
    # DOWNTREND is FAVORABLE for a SELL (unfavorable_trend_state=UPTREND
    # here) -- must not be rejected by the trend rule.
    result = evaluate_entry_state(
        trend_state="DOWNTREND", unfavorable_trend_state="UPTREND",
        tier1_passed=True, setup_state="NONE", stale_setup_state="STALE_BREAKDOWN",
        stale_reason="stale", not_overextended=True, overextended_reason="ext",
        overall_score=90.0, qualify_threshold=58.0,
    )
    assert result.entry_state == TRIGGERED
    assert result.qualified is True
