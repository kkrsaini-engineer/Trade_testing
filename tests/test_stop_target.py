"""
Tests for risk/stop_target.py — the shared stop-loss/target formula that
replaces 3 previously-hand-duplicated copies (execution/scanner.py,
risk/exit_strategy.py, risk/position_sizing.py — see PHASE20_NOTES.md).

Covers, mirrored across BOTH BUY and SELL:
  - Correct stop/target levels under normal (non-floored) ATR.
  - The MIN_STOP_PERCENT floor kicking in for very low ATR, applied
    consistently to stop AND both targets (the R-multiple invariant
    holds even when the floor — not raw ATR — is what's driving the
    distance).
  - target1/target2 are ALWAYS exactly TARGET1_R_MULTIPLE/
    TARGET2_R_MULTIPLE times the stop distance, regardless of inputs —
    this is the core "Risk:Reward can never vary" finding being fixed.
  - Zero/missing atr or close_price returns (0, 0, 0), never fabricated
    levels.
"""

import pytest

from risk import stop_target


def test_buy_normal_atr_levels():
    stop_loss, target1, target2 = stop_target.compute_stop_loss_targets(
        direction="BUY", close_price=100.0, atr=2.0,
    )
    assert stop_loss == 96.0    # 100 - 2*2
    assert target1 == 104.0     # 100 + 2*2 (1R)
    assert target2 == 107.0     # 100 + 3.5*2 (1.75R)


def test_sell_normal_atr_levels():
    stop_loss, target1, target2 = stop_target.compute_stop_loss_targets(
        direction="SELL", close_price=100.0, atr=2.0,
    )
    assert stop_loss == 104.0
    assert target1 == 96.0
    assert target2 == 93.0


def test_buy_floor_kicks_in_for_very_low_atr():
    # atr*2 = 0.02*2 = 0.04, floor = 100*0.01 = 1.0 -> floor wins.
    stop_loss, target1, target2 = stop_target.compute_stop_loss_targets(
        direction="BUY", close_price=100.0, atr=0.02,
    )
    dist = stop_target.stop_distance(close_price=100.0, atr=0.02)
    assert dist == 1.0
    assert stop_loss == 99.0
    # Targets scale off the SAME floored distance, at the fixed
    # R-multiples — the invariant holds even when the floor is active.
    assert target1 == round(100.0 + 1.0 * stop_target.TARGET1_R_MULTIPLE, 2)
    assert target2 == round(100.0 + 1.0 * stop_target.TARGET2_R_MULTIPLE, 2)


def test_sell_floor_kicks_in_for_very_low_atr():
    stop_loss, target1, target2 = stop_target.compute_stop_loss_targets(
        direction="SELL", close_price=100.0, atr=0.02,
    )
    dist = stop_target.stop_distance(close_price=100.0, atr=0.02)
    assert stop_loss == round(100.0 + dist, 2)
    assert target1 == round(100.0 - dist * stop_target.TARGET1_R_MULTIPLE, 2)
    assert target2 == round(100.0 - dist * stop_target.TARGET2_R_MULTIPLE, 2)


def test_r_multiple_invariant_holds_across_many_inputs():
    # The whole point of this module: target distance / stop distance
    # must be (up to ordinary paisa-rounding noise at 2 decimal places)
    # the fixed R-multiple, for every combination of inputs, floored or
    # not, BUY or SELL. A small tolerance is used because each of
    # stop/target1/target2 is independently rounded to 2 decimals for
    # display (a real traded price can't show fractional paise) — at
    # very small absolute distances that rounding can shift the ratio
    # by a percent or two; that's an acceptable presentation artifact,
    # not formula drift (the pre-rounding math is exact).
    cases = [
        ("BUY", 50.0, 1.0), ("BUY", 500.0, 20.0), ("BUY", 1000.0, 0.5),
        ("SELL", 50.0, 1.0), ("SELL", 500.0, 20.0), ("SELL", 1000.0, 0.5),
    ]
    for direction, close_price, atr in cases:
        stop_loss, target1, target2 = stop_target.compute_stop_loss_targets(
            direction=direction, close_price=close_price, atr=atr,
        )
        stop_dist = abs(close_price - stop_loss)
        target1_dist = abs(target1 - close_price)
        target2_dist = abs(target2 - close_price)
        assert target1_dist / stop_dist == pytest.approx(stop_target.TARGET1_R_MULTIPLE, abs=0.02)
        assert target2_dist / stop_dist == pytest.approx(stop_target.TARGET2_R_MULTIPLE, abs=0.02)


def test_zero_atr_or_close_returns_all_zero():
    assert stop_target.compute_stop_loss_targets(direction="BUY", close_price=0.0, atr=2.0) == (0.0, 0.0, 0.0)
    assert stop_target.compute_stop_loss_targets(direction="BUY", close_price=100.0, atr=0.0) == (0.0, 0.0, 0.0)


def test_r_multiple_constants_are_the_documented_values():
    assert stop_target.TARGET1_R_MULTIPLE == 1.0
    assert stop_target.TARGET2_R_MULTIPLE == 1.75
