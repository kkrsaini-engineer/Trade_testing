"""
Tests for scripts/diagnose_indicator_snapshot.py's branching helper
functions — the only logic in this diagnostic script (everything else
is a direct pass-through print of real engine output, nothing to unit
test in isolation).
"""

from scripts.diagnose_indicator_snapshot import (
    _bollinger_position,
    _cci_read,
    _macd_read,
    _roc_read,
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


def test_side_above():
    assert _side(105.0, 100.0) == "ABOVE"


def test_side_below():
    assert _side(95.0, 100.0) == "BELOW"


def test_side_exactly_at():
    assert _side(100.0, 100.0) == "EXACTLY AT"


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
