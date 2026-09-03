"""
Tests for risk/portfolio_rules.py's Phase 22 graduated drawdown/daily-loss
throttling — replaces the old binary >20% drawdown / 3% daily-loss reject
gates with risk/portfolio_limits.py's graduated bands/stages (see
PHASE21_NOTES.md's "9 + 10" section and PHASE22_NOTES.md).
"""

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationResult
from risk.risk_manager import RiskResult
from risk.position_sizing import PositionSizingResult
from risk.portfolio_rules import PortfolioRulesEngine


def _decision():
    return FinalDecision(
        action="BUY", confidence=85.0, ranking=70.0, buy_score=80.0, sell_score=20.0,
        buy_probability=75.0, sell_probability=15.0, expected_return=5.0,
        expected_drawdown=2.0, expected_hold_days=5,
    )


def _validation():
    return ValidationResult(passed=True, action="BUY", confidence=85.0, rejection_reason=None)


def _risk():
    return RiskResult(
        safe=True, total_risk=20.0, risk_grade="A", atr_risk=10.0, gap_risk=10.0,
        overnight_risk=10.0, news_risk=10.0, liquidity_risk=10.0, volatility_risk=10.0,
        portfolio_risk=10.0, sector_risk=10.0, correlation_risk=10.0, capital_risk=10.0,
    )


def _sizing():
    return PositionSizingResult(
        quantity=100, capital_to_use=50_000.0, allocation_percent=0.05,
        risk_per_trade=1.0, stop_distance=2.0, position_value=50_000.0,
        kelly_fraction=0.5, volatility_adjustment=1.0, liquidity_adjustment=1.0,
        confidence_adjustment=1.0,
    )


def _portfolio(max_drawdown=0.0, daily_loss=0.0, open_positions=None, correlation=0.10):
    # Phase 26 (see PHASE26_NOTES.md, point 11): "open_positions" (a
    # dict of positions, not a count) and "exposure" are the REAL keys
    # PortfolioEngine.snapshot()/VirtualPortfolio.snapshot() produce —
    # "open_positions_count"/"open_exposure" (the old fixture keys) were
    # never actually read by anything after this phase's fix.
    return {
        "total_capital": 1_000_000.0,
        "available_capital": 900_000.0,
        "available_cash": 900_000.0,
        "exposure": 0.10,
        "open_positions": open_positions if open_positions is not None else {"A": {}, "B": {}},
        "sector_exposure": 0.05,
        "correlation": correlation,
        "average_daily_value": 10_000_000.0,
        "top5_weight": 0.10,
        "max_drawdown": max_drawdown,
        "daily_loss": daily_loss,
    }


ENGINE = PortfolioRulesEngine()


def _evaluate(max_drawdown=0.0, daily_loss=0.0, open_positions=None, correlation=0.10):
    return ENGINE.evaluate(
        decision=_decision(), validation=_validation(), risk=_risk(), sizing=_sizing(),
        portfolio=_portfolio(
            max_drawdown=max_drawdown, daily_loss=daily_loss,
            open_positions=open_positions, correlation=correlation,
        ),
    )


# ==========================================================
# BASELINE — everything healthy, trade allowed, full allocation
# ==========================================================

def test_healthy_portfolio_is_allowed_at_full_allocation():
    result = _evaluate()
    assert result.allowed is True
    assert result.rejection_reason is None
    assert result.diagnostics["drawdown_band"] == "normal"
    assert result.diagnostics["daily_loss_stage"] == "normal"


# ==========================================================
# DRAWDOWN BANDS
# ==========================================================

def test_drawdown_reduced_band_scales_allocation_but_still_allowed():
    healthy = _evaluate()
    reduced = _evaluate(max_drawdown=0.07)  # "reduced" band -> x0.75
    assert reduced.allowed is True
    assert reduced.diagnostics["drawdown_band"] == "reduced"
    assert reduced.allocation_allowed == round(healthy.allocation_allowed * 0.75, 4)


def test_drawdown_heavily_reduced_band_scales_allocation_more():
    healthy = _evaluate()
    heavily_reduced = _evaluate(max_drawdown=0.12)  # "heavily_reduced" -> x0.50
    assert heavily_reduced.allowed is True
    assert heavily_reduced.diagnostics["drawdown_band"] == "heavily_reduced"
    assert heavily_reduced.allocation_allowed == round(healthy.allocation_allowed * 0.50, 4)


def test_drawdown_halt_band_rejects_new_trade():
    result = _evaluate(max_drawdown=0.18)  # > 15% -> halt
    assert result.allowed is False
    assert result.rejection_reason == "Portfolio drawdown exceeds limit."
    assert result.allocation_allowed == 0.0


# ==========================================================
# DAILY-LOSS STAGES
# ==========================================================

def test_daily_loss_risk_reduction_stage_scales_allocation_but_still_allowed():
    healthy = _evaluate()
    reduced = _evaluate(daily_loss=0.035)  # risk_reduction stage -> x0.50
    assert reduced.allowed is True
    assert reduced.diagnostics["daily_loss_stage"] == "risk_reduction"
    assert reduced.allocation_allowed == round(healthy.allocation_allowed * 0.50, 4)


def test_daily_loss_trading_halt_stage_rejects_new_trade():
    result = _evaluate(daily_loss=0.045)  # trading_halt stage
    assert result.allowed is False
    assert result.rejection_reason == "Daily loss limit reached."


def test_daily_loss_emergency_stage_rejects_new_trade():
    result = _evaluate(daily_loss=0.06)  # emergency stage
    assert result.allowed is False
    assert result.rejection_reason == "Daily loss limit reached."


def test_daily_loss_warning_stage_allows_at_full_allocation():
    healthy = _evaluate()
    warning = _evaluate(daily_loss=0.025)  # warning stage -> no scaling
    assert warning.allowed is True
    assert warning.diagnostics["daily_loss_stage"] == "warning"
    assert warning.allocation_allowed == healthy.allocation_allowed


# ==========================================================
# Phase 26 (see PHASE26_NOTES.md, point 11) — real open_positions key,
# correlation dedup
# ==========================================================

def test_open_positions_read_from_real_open_positions_dict():
    # Below MAX_OPEN_POSITIONS -> allowed, and the reported count
    # reflects the REAL dict length, not a fabricated/absent count.
    result = _evaluate(open_positions={"A": {}, "B": {}, "C": {}})
    assert result.allowed is True
    assert result.diagnostics["open_positions"] == 3


def test_max_open_positions_reached_rejects_new_trade():
    # Reads the engine's own MAX_OPEN_POSITIONS (CHANGED 2026-09-03:
    # 15 -> 100) rather than a hardcoded count, so this test keeps
    # testing the real boundary instead of silently passing/failing
    # for the wrong reason whenever that cap is retuned again.
    at_cap_positions = {
        f"SYM{i}": {} for i in range(PortfolioRulesEngine.MAX_OPEN_POSITIONS)
    }
    result = _evaluate(open_positions=at_cap_positions)
    assert result.allowed is False
    assert result.rejection_reason == "Maximum portfolio positions reached."


def test_missing_open_positions_key_defaults_to_zero_not_crash():
    result = _evaluate(open_positions={})
    assert result.allowed is True
    assert result.diagnostics["open_positions"] == 0


def test_correlation_no_longer_rejects_here_validation_engine_is_sole_gate():
    # Phase 26: portfolio_rules.py used to ALSO hard-reject on
    # correlation > MAX_CORRELATION (0.80) — an exact duplicate of
    # decision/validation_engine.py's gate. That rejection was removed
    # here; correlation is still reported (diagnostics) and still
    # produces the >0.70 soft warning, but must NOT set rejection_reason
    # via this engine anymore.
    result = _evaluate(correlation=0.95)  # far above the old 0.80 threshold
    assert result.allowed is True
    assert result.rejection_reason is None
    assert result.diagnostics["portfolio_correlation"] == 0.95
    assert "Portfolio correlation is elevated." in result.warnings


def test_correlation_below_warning_threshold_no_warning():
    result = _evaluate(correlation=0.30)
    assert "Portfolio correlation is elevated." not in result.warnings
