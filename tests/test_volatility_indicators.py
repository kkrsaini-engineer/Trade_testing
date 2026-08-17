"""
Tests for features/indicators/volatility.py — no test file existed for
this module before. Focus: ATR (and everything built on it — Keltner
band width, Supertrend) now uses Wilder's smoothing (real-data accuracy
audit, PHASE30_NOTES.md) instead of a plain rolling mean; Bollinger and
Donchian are genuinely plain rolling windows by their own standard
definitions and are unchanged.
"""

import numpy as np
import pandas as pd

from features.indicators.volatility import VolatilityIndicators


def _synthetic_ohlc(n: int = 60, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"high": high, "low": low, "close": close})


def _true_range(df_ohlc: pd.DataFrame) -> pd.Series:
    prev_close = df_ohlc["close"].shift(1)
    return pd.concat(
        [
            df_ohlc["high"] - df_ohlc["low"],
            (df_ohlc["high"] - prev_close).abs(),
            (df_ohlc["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def test_atr_is_positive_after_warmup():
    df = VolatilityIndicators().calculate(_synthetic_ohlc())
    valid = df["atr_14"].dropna()
    assert not valid.empty
    assert (valid > 0).all()


def test_atr_differs_from_plain_rolling_mean_atr():
    df_ohlc = _synthetic_ohlc()
    df = VolatilityIndicators().calculate(df_ohlc)
    old_atr = _true_range(df_ohlc).rolling(14, min_periods=14).mean()
    assert abs(df["atr_14"].iloc[-1] - old_atr.iloc[-1]) > 1e-6


def test_keltner_middle_is_ema20_unchanged():
    df_ohlc = _synthetic_ohlc()
    df = VolatilityIndicators().calculate(df_ohlc)
    expected_ema20 = df_ohlc["close"].ewm(span=20, adjust=False).mean()
    assert abs(df["kc_middle"].iloc[-1] - expected_ema20.iloc[-1]) < 1e-9


def test_keltner_band_width_differs_from_plain_rolling_atr20():
    df_ohlc = _synthetic_ohlc()
    df = VolatilityIndicators().calculate(df_ohlc)
    old_atr20 = _true_range(df_ohlc).rolling(20, min_periods=20).mean()
    old_upper = df["kc_middle"] + 2 * old_atr20
    assert abs(df["kc_upper"].iloc[-1] - old_upper.iloc[-1]) > 1e-6


def test_bollinger_and_donchian_unchanged_plain_rolling():
    df_ohlc = _synthetic_ohlc()
    df = VolatilityIndicators().calculate(df_ohlc)
    expected_sma20 = df_ohlc["close"].rolling(20, min_periods=20).mean()
    assert abs(df["bb_middle"].iloc[-1] - expected_sma20.iloc[-1]) < 1e-9
    expected_dc_upper = df_ohlc["high"].rolling(20, min_periods=20).max()
    assert abs(df["dc_upper"].iloc[-1] - expected_dc_upper.iloc[-1]) < 1e-9


def test_supertrend_direction_is_boolean_and_populated():
    df = VolatilityIndicators().calculate(_synthetic_ohlc())
    assert df["supertrend"].dtype == bool
    assert df["supertrend"].notna().all()


def test_volatility_state_derived_from_the_fixed_atr():
    df = VolatilityIndicators().calculate(_synthetic_ohlc())
    assert set(df["volatility_state"].unique()).issubset({"NORMAL", "HIGH", "LOW"})
