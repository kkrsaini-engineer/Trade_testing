"""
Tests for features/indicators/smoothing.py's wilders_smoothing() — the
shared helper RSI/ADX/ATR (and everything built on ATR: Keltner,
Supertrend) now use instead of a plain rolling mean.
"""

import pandas as pd

from features.indicators.smoothing import wilders_smoothing


def test_first_period_minus_one_bars_are_nan():
    series = pd.Series([float(i) for i in range(1, 21)])
    result = wilders_smoothing(series, 5)
    assert result.iloc[:4].isna().all()
    assert result.iloc[4:].notna().all()


def test_matches_hand_computed_wilders_recurrence():
    # Wilder's classic recurrence (SMA-seeded): first smoothed value is
    # the plain mean of the first `period` values, then
    # today = (yesterday * (period-1) + value) / period. Verify our
    # ewm(alpha=1/period, adjust=False)-based implementation converges
    # to this — not bar-for-bar identical near the seed (a documented,
    # accepted difference — see smoothing.py's docstring), but within a
    # small tolerance once a few bars past the seed.
    values = [10.0, 11.0, 9.0, 12.0, 8.0, 13.0, 14.0, 15.0, 16.0, 17.0]
    period = 4
    series = pd.Series(values)

    hand_seed = sum(values[:period]) / period
    hand = [None, None, None, hand_seed]
    prev = hand_seed
    for v in values[period:]:
        prev = (prev * (period - 1) + v) / period
        hand.append(prev)

    result = wilders_smoothing(series, period)

    # Last value (furthest from the seed) should be very close.
    assert abs(result.iloc[-1] - hand[-1]) < 0.5


def test_constant_series_smooths_to_the_same_constant():
    series = pd.Series([50.0] * 30)
    result = wilders_smoothing(series, 14)
    assert abs(result.iloc[-1] - 50.0) < 1e-9


def test_more_weight_on_recent_bars_than_plain_rolling_mean():
    # A late spike should move Wilder's smoothing MORE than a plain
    # rolling mean of the same window, since Wilder's weights recent
    # bars more heavily — this is the actual behavioral difference the
    # fix is about.
    values = [10.0] * 30 + [100.0]
    series = pd.Series(values)
    period = 14

    wilders = wilders_smoothing(series, period).iloc[-1]
    plain_rolling = series.rolling(period, min_periods=period).mean().iloc[-1]

    assert wilders > plain_rolling


def test_leading_nan_input_does_not_produce_early_values():
    # A series that itself starts with NaN (e.g. dx built from an
    # already-Wilder's-smoothed atr_14) should not get a value before
    # its own first real input, even though our fixed-position mask
    # only covers the first `period - 1` positions.
    series = pd.Series([float("nan")] * 5 + [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    result = wilders_smoothing(series, 3)
    assert result.iloc[:5].isna().all()
    assert result.iloc[5:].notna().all()
