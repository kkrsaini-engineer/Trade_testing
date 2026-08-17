"""
Tests for risk/portfolio_limits.py — the shared drawdown-band /
daily-loss-stage module that replaces 3 previously independent, drifted
hardcoded thresholds (risk/portfolio_rules.py, risk/risk_manager.py,
decision/validation_engine.py — see PHASE22_NOTES.md).
"""

from risk import portfolio_limits


# ==========================================================
# DRAWDOWN BANDS
# ==========================================================

def test_drawdown_band_normal_below_5_percent():
    assert portfolio_limits.drawdown_band_label(0.03) == "normal"
    assert portfolio_limits.drawdown_multiplier(0.03) == 1.0


def test_drawdown_band_reduced_between_5_and_10_percent():
    assert portfolio_limits.drawdown_band_label(0.07) == "reduced"
    assert portfolio_limits.drawdown_multiplier(0.07) == 0.75


def test_drawdown_band_heavily_reduced_between_10_and_15_percent():
    assert portfolio_limits.drawdown_band_label(0.12) == "heavily_reduced"
    assert portfolio_limits.drawdown_multiplier(0.12) == 0.50


def test_drawdown_band_halt_above_15_percent():
    assert portfolio_limits.drawdown_band_label(0.16) == "halt"
    assert portfolio_limits.drawdown_multiplier(0.16) == 0.0


def test_drawdown_band_boundaries_are_exclusive_on_the_lower_edge():
    # Exactly at a boundary belongs to the LOWER (less severe) band —
    # `>` comparisons, not `>=`.
    assert portfolio_limits.drawdown_band_label(0.05) == "normal"
    assert portfolio_limits.drawdown_band_label(0.10) == "reduced"
    assert portfolio_limits.drawdown_band_label(0.15) == "heavily_reduced"


def test_drawdown_band_none_treated_as_normal():
    assert portfolio_limits.drawdown_band_label(None) == "normal"


# ==========================================================
# DAILY-LOSS STAGES
# ==========================================================

def test_daily_loss_stage_normal_below_warning():
    assert portfolio_limits.daily_loss_stage(0.01) == "normal"


def test_daily_loss_stage_warning_at_2_percent():
    assert portfolio_limits.daily_loss_stage(0.02) == "warning"


def test_daily_loss_stage_risk_reduction_at_3_percent():
    assert portfolio_limits.daily_loss_stage(0.03) == "risk_reduction"
    assert portfolio_limits.daily_loss_multiplier(0.03) == 0.50


def test_daily_loss_stage_trading_halt_at_4_percent():
    assert portfolio_limits.daily_loss_stage(0.04) == "trading_halt"
    assert portfolio_limits.daily_loss_multiplier(0.04) == 1.0  # not scaled, rejected outright


def test_daily_loss_stage_emergency_at_5_percent():
    assert portfolio_limits.daily_loss_stage(0.05) == "emergency"


def test_daily_loss_stage_none_treated_as_normal():
    assert portfolio_limits.daily_loss_stage(None) == "normal"


# ==========================================================
# EMERGENCY CONDITION — the real trigger source for emergency_stop
# ==========================================================

def test_emergency_condition_false_when_both_normal():
    assert portfolio_limits.is_emergency_condition(0.02, 0.01) is False


def test_emergency_condition_true_on_drawdown_halt_alone():
    assert portfolio_limits.is_emergency_condition(0.20, 0.0) is True


def test_emergency_condition_true_on_daily_loss_emergency_alone():
    assert portfolio_limits.is_emergency_condition(0.0, 0.06) is True


def test_emergency_condition_false_at_trading_halt_not_emergency():
    # trading_halt (4%) is severe but NOT the emergency stage (5%+) —
    # emergency_stop should not fire yet at this stage.
    assert portfolio_limits.is_emergency_condition(0.0, 0.04) is False
