"""
Tests for execution/broker.py's Phase 24 fix (see PHASE24_NOTES.md) —
the circuit_breaker/spread/volatility rejection checks used to sit AFTER
an unconditional `return result`, making them 100% unreachable regardless
of market_state content. Moved earlier so they actually run.

Mirrored across BOTH BUY and SELL per the standing rule — these checks
are direction-agnostic by design, this confirms they stay that way.
"""

import random

from execution.broker import BrokerEngine, OrderRequest, REJECTED


def _engine():
    engine = BrokerEngine()
    # Deterministic, instant — no simulated network latency in tests.
    engine.execution_latency_ms = (0, 0)
    return engine


def _order(action):
    return OrderRequest(symbol="RELIANCE", action=action, quantity=10)


def _market_state(**overrides):
    base = {"market_open": True, "volatility": 0.01, "spread": 0.001}
    base.update(overrides)
    return base


# ==========================================================
# CIRCUIT BREAKER
# ==========================================================

def test_buy_rejected_when_circuit_breaker_active():
    engine = _engine()
    result = engine.place_order(
        order=_order("BUY"), market_price=100.0,
        market_state=_market_state(circuit_breaker=True),
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Circuit breaker active"
    assert result.filled_quantity == 0


def test_sell_rejected_when_circuit_breaker_active():
    engine = _engine()
    result = engine.place_order(
        order=_order("SELL"), market_price=100.0,
        market_state=_market_state(circuit_breaker=True),
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Circuit breaker active"


# ==========================================================
# SPREAD TOO HIGH
# ==========================================================

def test_buy_rejected_when_spread_too_high():
    engine = _engine()
    result = engine.place_order(
        order=_order("BUY"), market_price=100.0,
        market_state=_market_state(spread=0.06),  # > 0.05
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Spread too high"


def test_sell_rejected_when_spread_too_high():
    engine = _engine()
    result = engine.place_order(
        order=_order("SELL"), market_price=100.0,
        market_state=_market_state(spread=0.06),
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Spread too high"


# ==========================================================
# EXTREME VOLATILITY
# ==========================================================

def test_buy_rejected_when_volatility_extreme():
    engine = _engine()
    result = engine.place_order(
        order=_order("BUY"), market_price=100.0,
        market_state=_market_state(volatility=0.25),  # > 0.2
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Extreme volatility"


def test_sell_rejected_when_volatility_extreme():
    engine = _engine()
    result = engine.place_order(
        order=_order("SELL"), market_price=100.0,
        market_state=_market_state(volatility=0.25),
    )
    assert result.status == REJECTED
    assert result.diagnostics["reason"] == "Extreme volatility"


# ==========================================================
# NORMAL CONDITIONS — none of the 3 checks fire, order proceeds to the
# (still-live) fill-probability simulation instead of being rejected here.
# ==========================================================

def test_buy_not_rejected_by_these_checks_under_normal_conditions():
    random.seed(0)
    engine = _engine()
    result = engine.place_order(
        order=_order("BUY"), market_price=100.0,
        market_state=_market_state(),
    )
    assert result.diagnostics.get("reason") not in (
        "Circuit breaker active", "Spread too high", "Extreme volatility",
    )


def test_sell_not_rejected_by_these_checks_under_normal_conditions():
    random.seed(0)
    engine = _engine()
    result = engine.place_order(
        order=_order("SELL"), market_price=100.0,
        market_state=_market_state(),
    )
    assert result.diagnostics.get("reason") not in (
        "Circuit breaker active", "Spread too high", "Extreme volatility",
    )
