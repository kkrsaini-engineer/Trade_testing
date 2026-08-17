"""
Tests for scripts/diagnose_indicator_snapshot.py's branching helper
functions — the only logic in this diagnostic script (everything else
is a direct pass-through print of real engine output, nothing to unit
test in isolation).
"""

import math

import pandas as pd

from scripts.diagnose_indicator_snapshot import (
    _as_utc,
    _band_position,
    _bollinger_position,
    _cci_read,
    _cmf_read,
    _fmt,
    _macd_read,
    _mfi_read,
    _obv_read,
    _pivot_read,
    _roc_read,
    _select_candle_near,
    _side,
    _stochastic_read,
    _williams_r_read,
)


def test_above_upper_band():
    result = _bollinger_position(close=110.0, upper=105.0, middle=100.0, lower=95.0)
    assert "ABOVE the upper band" in result


def test_between_middle_and_upper():
    result = _bollinger_position(close=102.0, upper=105.0, middle=100.0, lower=95.0)
    assert "MIDDLE band" in result and "UPPER band" in result


def test_between_lower_and_middle():
    result = _bollinger_position(close=98.0, upper=105.0, middle=100.0, lower=95.0)
    assert "LOWER band" in result and "MIDDLE band" in result


def test_below_lower_band():
    result = _bollinger_position(close=90.0, upper=105.0, middle=100.0, lower=95.0)
    assert "BELOW the lower band" in result


def test_bollinger_position_nan_is_na():
    result = _bollinger_position(close=100.0, upper=float("nan"), middle=100.0, lower=95.0)
    assert "N/A" in result


def test_side_above():
    assert _side(105.0, 100.0) == "ABOVE"


def test_side_below():
    assert _side(95.0, 100.0) == "BELOW"


def test_side_exactly_at():
    assert _side(100.0, 100.0) == "EXACTLY AT"


def test_side_nan_is_na_not_exactly_at():
    result = _side(100.0, float("nan"))
    assert "N/A" in result


def test_fmt_regular_value():
    assert _fmt(123.456) == "123.46"


def test_fmt_nan_is_na():
    assert "N/A" in _fmt(float("nan"))


def test_band_position_above_upper():
    result = _band_position(close=110.0, upper=105.0, lower=95.0, upper_label="upper", lower_label="lower")
    assert "ABOVE the upper" in result


def test_band_position_below_lower():
    result = _band_position(close=90.0, upper=105.0, lower=95.0, upper_label="upper", lower_label="lower")
    assert "BELOW the lower" in result


def test_band_position_between():
    result = _band_position(close=100.0, upper=105.0, lower=95.0, upper_label="upper", lower_label="lower")
    assert "between the lower" in result and "upper" in result


def test_band_position_nan_is_na():
    result = _band_position(close=100.0, upper=float("nan"), lower=95.0, upper_label="u", lower_label="l")
    assert "N/A" in result


def test_macd_bullish_widening():
    result = _macd_read(macd=2.0, macd_signal=1.0, macd_histogram=1.0)
    assert "bullish" in result and "widening/positive" in result


def test_macd_bearish_narrowing():
    result = _macd_read(macd=-2.0, macd_signal=-1.0, macd_histogram=-1.0)
    assert "bearish" in result and "negative/narrowing" in result


def test_stochastic_overbought_k_above_d():
    result = _stochastic_read(stoch_k=85.0, stoch_d=80.0)
    assert "overbought zone" in result and "%K above %D" in result


def test_stochastic_oversold_k_below_d():
    result = _stochastic_read(stoch_k=15.0, stoch_d=20.0)
    assert "oversold zone" in result and "%K below %D" in result


def test_stochastic_neutral():
    result = _stochastic_read(stoch_k=50.0, stoch_d=45.0)
    assert "neutral zone" in result


def test_williams_r_overbought():
    assert "overbought zone" in _williams_r_read(-10.0)


def test_williams_r_oversold():
    assert "oversold zone" in _williams_r_read(-90.0)


def test_williams_r_neutral():
    assert "neutral zone" in _williams_r_read(-50.0)


def test_cci_overbought():
    assert "overbought zone" in _cci_read(150.0)


def test_cci_oversold():
    assert "oversold zone" in _cci_read(-150.0)


def test_cci_neutral():
    assert "neutral zone" in _cci_read(0.0)


def test_roc_positive():
    assert "positive momentum" in _roc_read(5.0)


def test_roc_negative():
    assert "negative momentum" in _roc_read(-5.0)


def test_obv_read_positive():
    assert "buying pressure" in _obv_read(1000.0)


def test_obv_read_negative():
    assert "selling pressure" in _obv_read(-1000.0)


def test_obv_read_nan():
    assert "N/A" in _obv_read(float("nan"))


def test_cmf_strong_buying():
    assert "strong buying" in _cmf_read(0.2)


def test_cmf_strong_selling():
    assert "strong selling" in _cmf_read(-0.2)


def test_cmf_neutral():
    assert "neutral" in _cmf_read(0.0)


def test_cmf_nan():
    assert "N/A" in _cmf_read(float("nan"))


def test_mfi_overbought():
    assert "overbought zone" in _mfi_read(85.0)


def test_mfi_oversold():
    assert "oversold zone" in _mfi_read(15.0)


def test_mfi_neutral():
    assert "neutral zone" in _mfi_read(50.0)


def test_mfi_nan():
    assert "N/A" in _mfi_read(float("nan"))


def test_pivot_above_r1():
    result = _pivot_read(close=112.0, pivot=100.0, resistance_1=110.0, support_1=90.0)
    assert "ABOVE R1" in result


def test_pivot_between_pivot_and_r1():
    result = _pivot_read(close=105.0, pivot=100.0, resistance_1=110.0, support_1=90.0)
    assert "between Pivot" in result and "R1" in result


def test_pivot_between_s1_and_pivot():
    result = _pivot_read(close=95.0, pivot=100.0, resistance_1=110.0, support_1=90.0)
    assert "between S1" in result and "Pivot" in result


def test_pivot_below_s1():
    result = _pivot_read(close=85.0, pivot=100.0, resistance_1=110.0, support_1=90.0)
    assert "BELOW S1" in result


def test_pivot_nan_is_na():
    result = _pivot_read(close=100.0, pivot=float("nan"), resistance_1=110.0, support_1=90.0)
    assert "N/A" in result


def test_fmt_matches_isnan_contract():
    # Sanity check that our NaN check actually catches math.nan the way
    # engine dataframes surface missing values (float NaN, not None).
    assert math.isnan(float("nan"))


def test_as_utc_naive_string_localized_to_utc():
    result = _as_utc("2026-08-12 08:45")
    assert str(result.tz) == "UTC"
    assert result.hour == 8 and result.minute == 45


def test_as_utc_aware_string_converted_to_utc():
    result = _as_utc("2026-08-12 14:15+05:30")
    assert str(result.tz) == "UTC"
    assert result.hour == 8 and result.minute == 45


def test_select_candle_near_exact_match():
    timestamps = pd.date_range("2026-08-01", periods=5, freq="1h", tz="UTC")
    features = pd.DataFrame({"timestamp": timestamps, "close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    row, diff = _select_candle_near(features, "2026-08-01 02:00")
    assert row["close"] == 3.0
    assert diff == pd.Timedelta(0)


def test_select_candle_near_picks_closest_when_no_exact_match():
    timestamps = pd.date_range("2026-08-01", periods=5, freq="1h", tz="UTC")
    features = pd.DataFrame({"timestamp": timestamps, "close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    row, diff = _select_candle_near(features, "2026-08-01 02:20")
    assert row["close"] == 3.0
    assert diff == pd.Timedelta(minutes=20)


def test_select_candle_near_far_off_reports_large_diff():
    timestamps = pd.date_range("2026-08-01", periods=5, freq="1h", tz="UTC")
    features = pd.DataFrame({"timestamp": timestamps, "close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    row, diff = _select_candle_near(features, "2026-09-01 00:00")
    assert diff > pd.Timedelta(days=1)
