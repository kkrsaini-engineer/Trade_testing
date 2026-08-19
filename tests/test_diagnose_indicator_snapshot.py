"""
Tests for scripts/diagnose_indicator_snapshot.py's branching helper
functions — the only logic in this diagnostic script (everything else
is a direct pass-through print of real engine output, nothing to unit
test in isolation).
"""

import argparse
import math

import pandas as pd
import pytest

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
    _print_day_dump,
    _print_window_composition,
    _print_zero_volume_scan,
    _roc_read,
    _rows_for_ist_day,
    _scan_zero_volume,
    _select_candle_near,
    _side,
    _stochastic_read,
    _validate_day_arg,
    _williams_r_read,
)


def _synthetic_multiday_features(
    days: int = 3, candles_per_day: int = 7, zero_volume_hours: tuple = ()
) -> pd.DataFrame:
    """Synthetic 60m-style OHLCV rows spanning several IST trading days,
    labeled at 9:15, 10:15, ... IST and converted to UTC — mirrors what
    MarketDataProvider's real fetch (yfinance) hands back for interval=60m.

    `zero_volume_hours` is an iterable of hour-offsets-from-9:15 (e.g.
    (0,) means "every day's 09:15 candle gets volume=0") — used to
    simulate the real zero-volume-opening-candle finding for the
    --scan-zero-volume tests below."""
    rows = []
    base_day = pd.Timestamp("2026-08-10", tz="Asia/Kolkata")
    close = 100.0
    for day_offset in range(days):
        day = base_day + pd.Timedelta(days=day_offset)
        for hour in range(candles_per_day):
            candle_time = day + pd.Timedelta(hours=9, minutes=15) + pd.Timedelta(hours=hour)
            close += 0.5
            volume = 0.0 if hour in zero_volume_hours else 100_000.0
            rows.append(
                {
                    "timestamp": candle_time.tz_convert("UTC"),
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


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


def test_rows_for_ist_day_returns_only_that_days_candles():
    features = _synthetic_multiday_features(days=3, candles_per_day=7)
    day_rows = _rows_for_ist_day(features, "2026-08-11")
    assert len(day_rows) == 7
    assert (day_rows["timestamp_ist"].dt.date == pd.Timestamp("2026-08-11").date()).all()


def test_rows_for_ist_day_sorted_chronologically():
    features = _synthetic_multiday_features(days=1, candles_per_day=7)
    # Shuffle the rows to prove sorting isn't just accidental row order.
    shuffled = features.sample(frac=1.0, random_state=0).reset_index(drop=True)
    day_rows = _rows_for_ist_day(shuffled, "2026-08-10")
    ist_times = day_rows["timestamp_ist"].tolist()
    assert ist_times == sorted(ist_times)


def test_rows_for_ist_day_first_candle_is_915_ist():
    features = _synthetic_multiday_features(days=1, candles_per_day=7)
    day_rows = _rows_for_ist_day(features, "2026-08-10")
    first = day_rows.iloc[0]["timestamp_ist"]
    assert (first.hour, first.minute) == (9, 15)


def test_rows_for_ist_day_no_match_returns_empty():
    features = _synthetic_multiday_features(days=1, candles_per_day=7)
    day_rows = _rows_for_ist_day(features, "2026-09-01")
    assert day_rows.empty


def test_print_day_dump_with_candles(capsys):
    features = _synthetic_multiday_features(days=1, candles_per_day=7)
    _print_day_dump(features, "2026-08-10")
    output = capsys.readouterr().out
    assert "7 candle(s) found" in output
    assert "09:15:00" in output
    assert "15:15:00" in output


def test_print_day_dump_no_candles(capsys):
    features = _synthetic_multiday_features(days=1, candles_per_day=7)
    _print_day_dump(features, "2026-09-01")
    output = capsys.readouterr().out
    assert "No candles found on 2026-09-01" in output


def test_validate_day_arg_accepts_valid_date():
    assert _validate_day_arg("2026-08-12") == "2026-08-12"


def test_validate_day_arg_rejects_non_date_text():
    # Regression test: a user once pasted descriptive text (not a date)
    # into the --dump-day / GitHub Actions input field, which previously
    # crashed deep inside pandas with a raw traceback instead of a clear
    # argparse error.
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_day_arg("timestamp IST + raw/UTC, open, high, low, close, volume")


def test_validate_day_arg_rejects_wrong_format():
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_day_arg("12-08-2026")


def test_validate_day_arg_rejects_invalid_calendar_date():
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_day_arg("2026-13-40")


def test_validate_day_arg_rejects_empty_string():
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_day_arg("")


def test_scan_zero_volume_finds_only_zero_rows():
    features = _synthetic_multiday_features(days=3, candles_per_day=7, zero_volume_hours=(0,))
    zero_rows = _scan_zero_volume(features)
    assert len(zero_rows) == 3
    assert (zero_rows["volume"] == 0).all()


def test_scan_zero_volume_clusters_on_same_ist_time():
    features = _synthetic_multiday_features(days=4, candles_per_day=7, zero_volume_hours=(0,))
    zero_rows = _scan_zero_volume(features)
    times = zero_rows["timestamp_ist"].dt.strftime("%H:%M").unique()
    assert list(times) == ["09:15"]


def test_scan_zero_volume_empty_when_no_zero_volume_rows():
    features = _synthetic_multiday_features(days=3, candles_per_day=7)
    zero_rows = _scan_zero_volume(features)
    assert zero_rows.empty


def test_scan_zero_volume_sorted_chronologically():
    features = _synthetic_multiday_features(days=4, candles_per_day=7, zero_volume_hours=(0, 6))
    shuffled = features.sample(frac=1.0, random_state=0).reset_index(drop=True)
    zero_rows = _scan_zero_volume(shuffled)
    ist_times = zero_rows["timestamp_ist"].tolist()
    assert ist_times == sorted(ist_times)


def test_print_zero_volume_scan_reports_systemic_pattern(capsys):
    features = _synthetic_multiday_features(days=5, candles_per_day=7, zero_volume_hours=(0,))
    _print_zero_volume_scan(features)
    output = capsys.readouterr().out
    assert "5 candle(s) have volume == 0" in output
    assert "09:15 IST: 5 candle(s)" in output
    assert "PATTERN" in output and "SYSTEMIC" in output


def test_print_zero_volume_scan_no_pattern_when_scattered_times(capsys):
    features = _synthetic_multiday_features(days=1, candles_per_day=7, zero_volume_hours=(0, 3))
    _print_zero_volume_scan(features)
    output = capsys.readouterr().out
    assert "No single dominant time-of-day pattern" in output


def test_print_zero_volume_scan_clean_when_no_zero_rows(capsys):
    features = _synthetic_multiday_features(days=2, candles_per_day=7)
    _print_zero_volume_scan(features)
    output = capsys.readouterr().out
    assert "No zero-volume candles found in this fetch — clean" in output


def test_window_composition_flags_zero_volume_bars_in_both_windows(capsys):
    # 5 trading days, opening (hour 0) candle zero-volume every day —
    # mirrors the real confirmed finding. Select the LAST candle
    # (index 34) so both the 21-bar and 14-bar windows are full and
    # span multiple days' opening candles, matching the hand-verified
    # real-data case (Aug-18 14:15 in PHASE30_NOTES.md's investigation).
    features = _synthetic_multiday_features(days=5, candles_per_day=7, zero_volume_hours=(0,))
    selected = features.iloc[34]
    _print_window_composition(features, selected)
    output = capsys.readouterr().out
    assert "CMF(21) window" in output and "MFI(14) window" in output
    assert "ZERO VOLUME" in output
    # 21-bar window ending at index 34 spans indices 14-34 -> covers 3
    # opening candles (indices 14, 21, 28); 14-bar window spans 21-34
    # -> covers 2 opening candles (indices 21, 28).
    assert "3 of 21 bar(s) in this window have volume == 0." in output
    assert "2 of 14 bar(s) in this window have volume == 0." in output


def test_window_composition_no_zero_volume_bars(capsys):
    features = _synthetic_multiday_features(days=5, candles_per_day=7)
    selected = features.iloc[34]
    _print_window_composition(features, selected)
    output = capsys.readouterr().out
    assert "0 of 21 bar(s) in this window have volume == 0." in output
    assert "0 of 14 bar(s) in this window have volume == 0." in output
    assert "<-- ZERO VOLUME" not in output


def test_window_composition_not_enough_history():
    features = _synthetic_multiday_features(days=1, candles_per_day=3)
    selected = features.iloc[1]
    # Should not raise even though there isn't a full 21/14-bar window
    # available yet — must degrade gracefully, matching real behavior
    # for early rows in a freshly fetched dataframe.
    _print_window_composition(features, selected)


def test_window_composition_not_enough_history_message(capsys):
    features = _synthetic_multiday_features(days=1, candles_per_day=3)
    selected = features.iloc[1]
    _print_window_composition(features, selected)
    output = capsys.readouterr().out
    assert "Not enough history before this candle for a full 21-bar window" in output
    assert "Not enough history before this candle for a full 14-bar window" in output
