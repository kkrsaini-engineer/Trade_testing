"""
Tests for scripts/diagnose_indicator_snapshot.py's _bollinger_position()
— the only branching logic in this diagnostic script (everything else
is a direct pass-through print of real engine output, nothing to unit
test in isolation).
"""

from scripts.diagnose_indicator_snapshot import _bollinger_position


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
