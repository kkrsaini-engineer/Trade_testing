"""
Tests for decision/validation_engine.py's Phase 22 changes (see
PHASE22_NOTES.md):
  - `circuit` check now reads from `market_state` (a real dict callers
    populate), not `latest` (a price-dataframe row that never had a
    "circuit_breaker" column produced anywhere in the data pipeline).
  - `daily_loss` / `drawdown` checks now use the shared
    risk/portfolio_limits.py stages/bands instead of this file's own
    third, independently-hardcoded threshold copies.

Only these 3 checks are asserted directly via `result.checks` /
`result.diagnostics` — every check in validate() runs unconditionally
and populates `checks`/`diagnostics` regardless of whether an EARLIER
check already set `rejection_reason`, so a fixture doesn't need to pass
every other unrelated validation layer to exercise these three.
"""

import pandas as pd

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationEngine


def _decision():
    return FinalDecision(
        action="BUY", confidence=85.0, ranking=70.0, buy_score=80.0, sell_score=20.0,
        buy_probability=75.0, sell_probability=15.0, expected_return=5.0,
        expected_drawdown=2.0, expected_hold_days=5,
    )


def _dataframe():
    # 2026-08-17 is a Monday — fixed, not datetime.now()-dependent, so
    # this test doesn't flake depending on what day it's actually run.
    return pd.DataFrame([{"timestamp": "2026-08-17", "close": 100.0}])


def _market_state(circuit_breaker=False):
    return {"market_open": True, "holiday": False, "circuit_breaker": circuit_breaker}


ENGINE = ValidationEngine()


def _validate(portfolio=None, circuit_breaker=False):
    return ENGINE.validate(
        decision=_decision(),
        dataframe=_dataframe(),
        portfolio=portfolio or {},
        broker_status={"connected": True, "order_allowed": True},
        market_state=_market_state(circuit_breaker=circuit_breaker),
    )


# ==========================================================
# CIRCUIT BREAKER — now reads market_state, not the dataframe
# ==========================================================

def test_circuit_check_passes_when_market_state_flag_is_false():
    result = _validate(circuit_breaker=False)
    assert result.checks["circuit"] is True


def test_circuit_check_fails_when_market_state_flag_is_true():
    result = _validate(circuit_breaker=True)
    assert result.checks["circuit"] is False
    assert result.rejection_reason == "Circuit breaker active."


# ==========================================================
# DAILY LOSS — shared stage function, not a lone 0.03 constant
# ==========================================================

def test_daily_loss_check_passes_below_trading_halt_stage():
    result = _validate(portfolio={"daily_loss": 0.035})  # risk_reduction stage
    assert result.checks["daily_loss"] is True


def test_daily_loss_check_fails_at_trading_halt_stage():
    result = _validate(portfolio={"daily_loss": 0.04})
    # rejection_reason isn't asserted here — an unrelated earlier check
    # (insufficient historical candles, from this minimal 1-row fixture)
    # sets it first. `checks["daily_loss"]` is computed unconditionally
    # regardless, which is the thing this test is actually verifying.
    assert result.checks["daily_loss"] is False


# ==========================================================
# DRAWDOWN — shared band function, not a lone 0.20 constant
# ==========================================================

def test_drawdown_check_passes_below_halt_band():
    result = _validate(portfolio={"max_drawdown": 0.12})  # heavily_reduced, not halt
    assert result.checks["drawdown"] is True


def test_drawdown_check_fails_at_halt_band():
    result = _validate(portfolio={"max_drawdown": 0.18})
    # See note in test_daily_loss_check_fails_at_trading_halt_stage above
    # — rejection_reason isn't asserted for the same reason.
    assert result.checks["drawdown"] is False
