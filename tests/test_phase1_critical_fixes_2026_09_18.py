"""
Tests for the three Phase 1 CRITICAL bug fixes from BUG_AUDIT_2026-09-18.md
(all three verified/confirmed by direct code reading + reproduction before
being fixed):

1. portfolio/portfolio.py close_position() used to OVERWRITE
   pos.realized_pnl with just its own leg's P&L (0, when called as the
   final leg from partial_exit(), since quantity was already zeroed) --
   silently erasing every prior partial-exit leg's accumulated P&L.

2. portfolio/portfolio.py _current_equity() valued every open position as
   `quantity * current_price` regardless of direction -- correct for BUY,
   backwards for SELL/short (a rising price against a short is a real
   loss, but the old formula made it look like a gain).

3. risk/position_sizing.py's MIN_POSITION_VALUE floor could force a
   weak-setup's tiny risk-adjusted allocation up past MAX_CAPITAL_
   ALLOCATION (20%), bypassing the very risk budget the engine had just
   computed.
"""

import pandas as pd
import pytest

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationResult
from portfolio.portfolio import PortfolioEngine, PortfolioState, PortfolioPosition
from risk.position_sizing import PositionSizingEngine
from risk.risk_manager import RiskResult


def _engine(total_capital=100_000.0, available_capital=None):
    state = PortfolioState(
        total_capital=total_capital,
        available_capital=available_capital if available_capital is not None else total_capital,
    )
    return PortfolioEngine(state=state)


# ==========================================================
# BUG #1 — partial_exit() legs must ACCUMULATE, not get erased by the
# final close_position() call
# ==========================================================

def test_two_partial_exit_legs_followed_by_final_close_accumulate_realized_pnl():
    engine = _engine(100_000.0)
    engine.add_position("RELIANCE", quantity=100, entry_price=100.0, direction="BUY")

    # Leg 1: 50 shares @ 110 -> +500 profit.
    engine.partial_exit("RELIANCE", quantity=50, exit_price=110.0)
    assert engine.state.open_positions["RELIANCE"].realized_pnl == 500.0

    # Leg 2 (the LAST leg, quantity reaches 0 -> triggers close_position()
    # internally): 50 shares @ 120 -> +1000 more profit ((120-100)*50).
    # Total should be 500+1000=1500, not the old bug's "0" (close_
    # position overwriting instead of accumulating).
    engine.partial_exit("RELIANCE", quantity=50, exit_price=120.0)

    assert "RELIANCE" not in engine.state.open_positions
    closed = engine.state.closed_positions[-1]
    assert closed.symbol == "RELIANCE"
    assert closed.realized_pnl == 1500.0
    assert closed.status == "CLOSED"


def test_direct_full_close_position_with_no_prior_partial_exit_is_unchanged():
    # Backward-compatibility: a position that was NEVER partial-exited
    # must behave byte-for-byte as before (pos.realized_pnl starts at the
    # dataclass default 0.0, so `+=` and the old `=` are equivalent here).
    engine = _engine(100_000.0)
    engine.add_position("TCS", quantity=10, entry_price=3000.0, direction="BUY")

    closed = engine.close_position("TCS", exit_price=3100.0)

    assert closed.realized_pnl == 1000.0  # (3100-3000)*10


def test_sell_direction_partial_exits_also_accumulate_correctly():
    engine = _engine(300_000.0)
    engine.add_position("INFY", quantity=100, entry_price=1500.0, direction="SELL")

    # Leg 1: price drops to 1480 (a SELL profit of (1500-1480)*50 = 1000).
    engine.partial_exit("INFY", quantity=50, exit_price=1480.0)
    assert engine.state.open_positions["INFY"].realized_pnl == 1000.0

    # Leg 2 (final leg): price drops further to 1460 -> (1500-1460)*50 = 2000.
    engine.partial_exit("INFY", quantity=50, exit_price=1460.0)

    closed = engine.state.closed_positions[-1]
    assert closed.realized_pnl == 3000.0


def test_total_pnl_matches_sum_of_partial_legs_not_just_the_last_one():
    # This is the portfolio-level symptom the audit flagged: total_pnl
    # must reflect BOTH legs, and must not be corrupted by the final
    # close_position() call double-counting or zeroing anything.
    engine = _engine(100_000.0)
    engine.add_position("HDFC", quantity=100, entry_price=1000.0, direction="BUY")

    engine.partial_exit("HDFC", quantity=50, exit_price=1010.0)  # +500
    engine.partial_exit("HDFC", quantity=50, exit_price=1020.0)  # +1000

    assert engine.state.total_pnl == 1500.0


# ==========================================================
# BUG #2 — _current_equity() must be direction-aware
# ==========================================================

def test_equity_for_a_losing_short_position_reflects_the_real_loss_not_a_phantom_gain():
    engine = _engine(100_000.0, available_capital=90_000.0)
    engine.state.open_positions["ZOMATO"] = PortfolioPosition(
        symbol="ZOMATO", quantity=100, entry_price=100.0, current_price=150.0,
        direction="SELL",
    )
    # Real economics: sold 100 shares at Rs.100, now costs Rs.150 to buy
    # back -> a genuine Rs.5,000 loss. Correct equity = 90,000 + 5,000 =
    # 95,000. The old, direction-blind formula gave 90,000 + 100*150 =
    # 105,000 -- a short position LOSING money made to look like a GAIN.
    equity = engine._current_equity()
    assert equity == 95_000.0


def test_equity_for_a_winning_short_position_reflects_the_real_gain():
    engine = _engine(100_000.0, available_capital=90_000.0)
    engine.state.open_positions["ZOMATO"] = PortfolioPosition(
        symbol="ZOMATO", quantity=100, entry_price=100.0, current_price=70.0,
        direction="SELL",
    )
    # Price fell (good for a short): real gain = (100-70)*100 = 3,000.
    equity = engine._current_equity()
    assert equity == 90_000.0 + 100.0 * 100.0 + 3_000.0  # 103,000


def test_equity_for_a_long_buy_position_is_unchanged_by_the_fix():
    # Byte-for-byte backward compatibility for the BUY case (this is the
    # exact scenario test_portfolio_equity_tracking.py's
    # test_open_position_market_value_counts_toward_equity already covers
    # end-to-end via update_equity_tracking(); this test hits
    # _current_equity() directly).
    engine = _engine(100_000.0, available_capital=50_000.0)
    engine.state.open_positions["RELIANCE"] = PortfolioPosition(
        symbol="RELIANCE", quantity=100, entry_price=500.0, current_price=520.0,
        direction="BUY",
    )
    assert engine._current_equity() == 50_000.0 + 100.0 * 520.0  # 102,000


# ==========================================================
# BUG #3 — MIN_POSITION_VALUE floor must never breach MAX_CAPITAL_ALLOCATION
# ==========================================================

def _sizing_engine():
    return PositionSizingEngine()


def _weak_setup_dataframe():
    # High ATR% (>5 -> volatility_adjustment floors at 0.40) and low
    # volume (<500k -> liquidity_adjustment floors at 0.40), so combined
    # with a low-confidence decision and a high-risk RiskResult below,
    # the adjustment_factor bottoms out and allocation_percent collapses
    # to MIN_CAPITAL_ALLOCATION (2%) -- exactly the "weak setup" scenario
    # the audit's example describes.
    return pd.DataFrame([{"close": 100.0, "atr_14": 10.0, "volume_sma_20": 100_000.0}])


def _weak_decision():
    return FinalDecision(
        action="BUY",
        confidence=10.0,  # -> confidence_adjustment floors at 0.50
        ranking=10.0,
        buy_score=10.0,
        sell_score=0.0,
        buy_probability=10.0,
        sell_probability=0.0,
        expected_return=5.0,
        expected_drawdown=2.0,
        expected_hold_days=5,
    )


def _weak_validation():
    return ValidationResult(
        passed=True,
        action="BUY",
        confidence=10.0,
        rejection_reason=None,
    )


def _weak_risk():
    return RiskResult(
        safe=True,
        total_risk=90.0,  # -> risk_adjustment floors at 0.20
        risk_grade="HIGH",
        atr_risk=0.0,
        gap_risk=0.0,
        overnight_risk=0.0,
        news_risk=0.0,
        liquidity_risk=0.0,
        volatility_risk=0.0,
        portfolio_risk=0.0,
        sector_risk=0.0,
        correlation_risk=0.0,
        capital_risk=0.0,
    )


def test_min_position_value_floor_never_breaches_max_capital_allocation():
    engine = _sizing_engine()
    # A weak setup on a small (Rs.20,000) account: the risk-adjusted
    # capital_to_use collapses to MIN_CAPITAL_ALLOCATION (2% -> Rs.400),
    # well below MIN_POSITION_VALUE (Rs.5,000) -- the exact scenario the
    # audit's example describes.
    result = engine.calculate(
        decision=_weak_decision(),
        validation=_weak_validation(),
        risk=_weak_risk(),
        dataframe=_weak_setup_dataframe(),
        portfolio={"available_cash": 20_000.0, "max_quantity": 10_000},
    )
    assert result.diagnostics["final_allocation_percent"] == pytest.approx(
        engine.MIN_CAPITAL_ALLOCATION
    )
    assert result.capital_to_use == pytest.approx(400.0)

    max_allowed = 20_000.0 * engine.MAX_CAPITAL_ALLOCATION  # Rs.4,000
    # The bug lived specifically in the capital-allocation-based
    # `position_value` (used to derive `capital_quantity`, before it is
    # combined with the separate ATR-risk-based quantity via min()) --
    # this is the precise point the audit's example describes, and where
    # the old code forced it to the unconditional Rs.5,000 floor,
    # breaching the 20% allocation cap (Rs.4,000).
    assert result.diagnostics["position_value"] == pytest.approx(max_allowed)
    assert result.diagnostics["capital_quantity"] == 40  # floor(4,000 / 100)

    # End-to-end: the final blended result (min of ATR-quantity and
    # capital-quantity) can only ever be tighter than this cap, never
    # looser, so it too must never exceed it.
    assert result.position_value <= max_allowed + 1e-6


def test_min_position_value_floor_still_applies_when_it_does_not_breach_the_cap():
    # On an account where the Rs.5,000 floor is still comfortably under
    # 20% of capital (here: 20% of Rs.100,000 == Rs.20,000), the floor
    # should behave exactly as before -- this proves the fix only CAPS
    # the floor, it doesn't remove it.
    engine = _sizing_engine()
    result = engine.calculate(
        decision=_weak_decision(),
        validation=_weak_validation(),
        risk=_weak_risk(),
        dataframe=_weak_setup_dataframe(),
        portfolio={"available_cash": 100_000.0, "max_quantity": 10_000},
    )
    assert result.capital_to_use == pytest.approx(2_000.0)  # 2% of 100,000
    assert result.position_value == pytest.approx(engine.MIN_POSITION_VALUE)
