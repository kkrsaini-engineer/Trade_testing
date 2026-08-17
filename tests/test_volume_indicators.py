"""
Tests for features/indicators/volume.py — no test file existed for this
module before. Focus: CMF's window changed from 20 to 21 bars (real-
data accuracy audit, PHASE30_NOTES.md; column name kept as `cmf_20` for
backward compatibility — see the FIX comment at its computation site).
OBV, VWAP, MFI, volume_sma_20 are unchanged and get a basic sanity
check.
"""

import numpy as np
import pandas as pd

from features.indicators.volume import VolumeIndicators


def _synthetic_ohlcv(n: int = 40, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    volume = rng.integers(10_000, 100_000, n).astype(float)
    return pd.DataFrame({"high": high, "low": low, "close": close, "volume": volume})


def test_cmf_needs_21_bars_not_20():
    # Exactly 20 rows: under the OLD 20-bar window this would have
    # produced one real value at the last row. Under the fixed 21-bar
    # window it must still be entirely NaN.
    df20 = VolumeIndicators().calculate(_synthetic_ohlcv(n=20))
    assert df20["cmf_20"].isna().all()

    df21 = VolumeIndicators().calculate(_synthetic_ohlcv(n=21))
    assert df21["cmf_20"].iloc[:20].isna().all()
    assert pd.notna(df21["cmf_20"].iloc[20])


def test_cmf_within_expected_range():
    df = VolumeIndicators().calculate(_synthetic_ohlcv())
    valid = df["cmf_20"].dropna()
    assert not valid.empty
    assert valid.between(-1.0001, 1.0001).all()


def test_obv_is_rolling_20_bar_signed_sum_unchanged():
    df = VolumeIndicators().calculate(_synthetic_ohlcv())
    assert df["obv"].iloc[:19].isna().all()
    assert pd.notna(df["obv"].iloc[19])


def test_vwap_is_cumulative_unchanged():
    df_ohlcv = _synthetic_ohlcv()
    df = VolumeIndicators().calculate(df_ohlcv)
    typical_price = (df_ohlcv["high"] + df_ohlcv["low"] + df_ohlcv["close"]) / 3
    expected = (typical_price * df_ohlcv["volume"]).cumsum() / df_ohlcv["volume"].cumsum()
    assert abs(df["vwap"].iloc[-1] - expected.iloc[-1]) < 1e-9


def test_mfi_within_0_100():
    df = VolumeIndicators().calculate(_synthetic_ohlcv())
    valid = df["mfi_14"].dropna()
    assert not valid.empty
    assert valid.between(0, 100).all()


def test_volume_sma_20_unchanged():
    df_ohlcv = _synthetic_ohlcv()
    df = VolumeIndicators().calculate(df_ohlcv)
    expected = df_ohlcv["volume"].rolling(20, min_periods=20).mean()
    assert abs(df["volume_sma_20"].iloc[-1] - expected.iloc[-1]) < 1e-9
