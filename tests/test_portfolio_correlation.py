"""
Tests for portfolio/correlation.py (Phase 26, see PHASE26_NOTES.md,
point 11) — real portfolio-correlation computation, replacing the
`portfolio.get("correlation", 0.0)` input that no producer anywhere in
the codebase ever actually populated before this phase.
"""

import pandas as pd

from portfolio.correlation import compute_portfolio_correlation, fetch_correlation_inputs


def _series(values, start="2024-01-01"):
    index = pd.date_range(start=start, periods=len(values), freq="D")
    return pd.Series(values, index=index)


def _prices_from_returns(returns, start_price=100.0):
    """Builds a price path whose day-over-day % returns are EXACTLY
    `returns` (up to floating point) — used to construct genuinely
    (anti-)correlated series, since two arbitrary monotonic price
    trends do NOT generally produce (anti-)correlated RETURNS (percent
    change compounds non-linearly — a straight linear price ramp and
    its "mirror" are not simply negated in return-space)."""
    prices = [start_price]
    for r in returns:
        prices.append(prices[-1] * (1 + r))
    return prices[1:]


# A non-monotonic, varied return sequence (not just up or just down) so
# correlation isn't trivially ~1.0 for any two same-signed price paths.
_VARIED_RETURNS = [
    0.01, -0.02, 0.015, -0.005, 0.02, -0.015, 0.005, -0.01, 0.025, -0.02,
    0.01, -0.005, 0.015, -0.025, 0.02, -0.01, 0.005, -0.015, 0.01, -0.02,
    0.015, -0.005, 0.02, -0.015, 0.01, -0.01, 0.005, -0.005, 0.015, -0.02,
]


# ==========================================================
# compute_portfolio_correlation() — pure function
# ==========================================================

def test_perfectly_correlated_positions_return_close_to_one():
    a_prices = _prices_from_returns(_VARIED_RETURNS, start_price=100.0)
    b_prices = _prices_from_returns(_VARIED_RETURNS, start_price=50.0)  # same returns, different scale
    closes = {"A": _series(a_prices), "B": _series(b_prices)}
    result = compute_portfolio_correlation(closes)
    assert result is not None
    assert result > 0.99


def test_perfectly_anti_correlated_positions_return_close_to_minus_one():
    a_prices = _prices_from_returns(_VARIED_RETURNS, start_price=100.0)
    inverse_returns = [-r for r in _VARIED_RETURNS]
    b_prices = _prices_from_returns(inverse_returns, start_price=100.0)
    closes = {"A": _series(a_prices), "B": _series(b_prices)}
    result = compute_portfolio_correlation(closes)
    assert result is not None
    assert result < -0.99


def test_single_position_returns_none_not_zero():
    # Correlation is mathematically undefined with only 1 position — the
    # honest answer is "unknown", not a fabricated 0.0.
    closes = {"A": _series([100 + i for i in range(30)])}
    assert compute_portfolio_correlation(closes) is None


def test_no_positions_returns_none():
    assert compute_portfolio_correlation({}) is None


def test_insufficient_overlapping_history_returns_none():
    closes = {
        "A": _series([100, 101, 102]),  # only 3 days -> < min_overlapping_days
        "B": _series([200, 202, 204]),
    }
    result = compute_portfolio_correlation(closes, min_overlapping_days=10)
    assert result is None


def test_three_positions_averages_pairwise_correlations():
    a_prices = _prices_from_returns(_VARIED_RETURNS, start_price=100.0)
    b_prices = _prices_from_returns(_VARIED_RETURNS, start_price=50.0)  # ~+1.0 with A
    c_prices = _prices_from_returns([-r for r in _VARIED_RETURNS], start_price=100.0)  # ~-1.0 with A, B
    closes = {"A": _series(a_prices), "B": _series(b_prices), "C": _series(c_prices)}
    result = compute_portfolio_correlation(closes)
    assert result is not None
    # Average of (~+1, ~-1, ~-1) pairs (A-B, A-C, B-C) should land near
    # -0.33, not at either extreme — confirms genuine averaging, not
    # max()/min() or a single-pair shortcut.
    assert -0.8 < result < 0.5


def test_custom_lookback_and_thresholds_respected():
    base = [100 + i for i in range(50)]
    closes = {"A": _series(base), "B": _series([v * 3 for v in base])}
    result = compute_portfolio_correlation(closes, lookback_days=5, min_overlapping_days=3)
    assert result is not None


def test_min_positions_threshold_enforced():
    closes = {
        "A": _series([100 + i for i in range(30)]),
        "B": _series([200 + i for i in range(30)]),
    }
    # Require 3 positions when only 2 are given -> None.
    assert compute_portfolio_correlation(closes, min_positions=3) is None


# ==========================================================
# fetch_correlation_inputs() — network mocked
# ==========================================================

class _FakeProvider:
    def __init__(self, data):
        self._data = data

    def fetch(self, symbol, period="2mo"):
        if symbol not in self._data:
            raise ConnectionError(f"no data for {symbol}")
        return self._data[symbol]


def test_fetch_correlation_inputs_skips_failed_symbols():
    good_df = pd.DataFrame({"close": [100.0, 101.0, 102.0]})
    provider = _FakeProvider({"GOOD": good_df})

    result = fetch_correlation_inputs(["GOOD", "BAD"], provider)

    assert "GOOD" in result
    assert "BAD" not in result


def test_fetch_correlation_inputs_skips_empty_or_missing_close_column():
    empty_df = pd.DataFrame()
    no_close_df = pd.DataFrame({"open": [1.0, 2.0]})
    provider = _FakeProvider({"EMPTY": empty_df, "NOCLOSE": no_close_df})

    result = fetch_correlation_inputs(["EMPTY", "NOCLOSE"], provider)

    assert result == {}


def test_fetch_correlation_inputs_returns_empty_dict_when_all_fail():
    provider = _FakeProvider({})
    result = fetch_correlation_inputs(["A", "B", "C"], provider)
    assert result == {}
