"""
Tests for execution/scanner.py's Phase 26 wiring (see PHASE26_NOTES.md,
point 11) — MarketScanner._sector_exposure_ratio(), the per-symbol
sector-exposure computation that replaces the always-0.0
`portfolio.get("sector_exposure", 0.0)` fallback every caller got
before this phase (no producer supplied a real value; even when one
theoretically could, the raw {sector: $value} dict shape didn't match
what risk_manager.py/validation_engine.py/portfolio_rules.py expect —
a scalar ratio).
"""

from execution.scanner import MarketScanner


def test_no_sector_returns_zero():
    result = MarketScanner._sector_exposure_ratio(
        None, {"sector_exposure_by_sector": {"IT": 500_000.0}, "portfolio_value": 1_000_000.0}
    )
    assert result == 0.0


def test_missing_portfolio_value_returns_zero():
    result = MarketScanner._sector_exposure_ratio(
        "IT", {"sector_exposure_by_sector": {"IT": 500_000.0}}
    )
    assert result == 0.0


def test_zero_portfolio_value_returns_zero_not_crash():
    result = MarketScanner._sector_exposure_ratio(
        "IT", {"sector_exposure_by_sector": {"IT": 500_000.0}, "portfolio_value": 0.0}
    )
    assert result == 0.0


def test_missing_sector_exposure_by_sector_key_returns_zero():
    # Caller (backtest, diagnose scripts) didn't supply this — same 0.0
    # fallback as always, not a new regression.
    result = MarketScanner._sector_exposure_ratio("IT", {"portfolio_value": 1_000_000.0})
    assert result == 0.0


def test_sector_not_present_in_breakdown_returns_zero():
    # A sector with genuinely $0 exposure right now (no producer ever
    # claimed otherwise) — correctly 0.0, not a missing-data guess.
    result = MarketScanner._sector_exposure_ratio(
        "Pharma", {"sector_exposure_by_sector": {"IT": 500_000.0}, "portfolio_value": 1_000_000.0}
    )
    assert result == 0.0


def test_real_ratio_computed_correctly():
    result = MarketScanner._sector_exposure_ratio(
        "IT", {"sector_exposure_by_sector": {"IT": 300_000.0}, "portfolio_value": 1_000_000.0}
    )
    assert result == 0.3


def test_real_ratio_computed_correctly_different_sector():
    # Same portfolio, a DIFFERENT symbol's sector -> different ratio —
    # confirms this is genuinely per-symbol, not a single cached number.
    portfolio = {
        "sector_exposure_by_sector": {"IT": 300_000.0, "Banking": 100_000.0},
        "portfolio_value": 1_000_000.0,
    }
    assert MarketScanner._sector_exposure_ratio("IT", portfolio) == 0.3
    assert MarketScanner._sector_exposure_ratio("Banking", portfolio) == 0.1
