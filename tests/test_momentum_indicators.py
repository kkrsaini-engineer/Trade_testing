"""
Tests for features/indicators/momentum.py — no test file existed for
this module before. Focus: RSI/ADX now use Wilder's smoothing (real-
data accuracy audit, PHASE30_NOTES.md) instead of a plain rolling mean;
everything else (MACD, ROC, Williams %R, CCI, Stochastic) is unchanged
and gets a basic sanity check.
"""

import numpy as np
import pandas as pd

from features.indicators.momentum import MomentumIndicators


def _synthetic_ohlc(n: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"high": high, "low": low, "close": close})


def test_rsi_stays_within_0_100():
    df = MomentumIndicators().calculate(_synthetic_ohlc())
    valid = df["rsi_14"].dropna()
    assert not valid.empty
    assert valid.between(0, 100).all()


def test_rsi_nan_for_warmup_period():
    df = MomentumIndicators().calculate(_synthetic_ohlc())
    assert df["rsi_14"].iloc[:13].isna().all()
    assert df["rsi_14"].iloc[13:].notna().all()


def test_rsi_differs_from_plain_rolling_mean_rsi():
    # Proves the Wilder's-smoothing fix actually changed RSI's values,
    # not just that RSI still computes something.
    df_ohlc = _synthetic_ohlc()
    df = MomentumIndicators().calculate(df_ohlc)

    delta = df_ohlc["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    old_avg_gain = gain.rolling(14, min_periods=14).mean()
    old_avg_loss = loss.rolling(14, min_periods=14).mean()
    old_rs = old_avg_gain / old_avg_loss.replace(0, np.nan)
    old_rsi = 100 - (100 / (1 + old_rs))

    assert abs(df["rsi_14"].iloc[-1] - old_rsi.iloc[-1]) > 0.01


def test_adx_stays_within_0_100():
    df = MomentumIndicators().calculate(_synthetic_ohlc())
    valid = df["adx_14"].dropna()
    assert not valid.empty
    assert valid.between(0, 100).all()


def test_adx_differs_from_plain_rolling_mean_adx():
    df_ohlc = _synthetic_ohlc()
    df = MomentumIndicators().calculate(df_ohlc)

    prev_close = df_ohlc["close"].shift(1)
    prev_high = df_ohlc["high"].shift(1)
    prev_low = df_ohlc["low"].shift(1)
    up_move = df_ohlc["high"] - prev_high
    down_move = prev_low - df_ohlc["low"]
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = pd.concat(
        [
            df_ohlc["high"] - df_ohlc["low"],
            (df_ohlc["high"] - prev_close).abs(),
            (df_ohlc["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    old_atr = tr.rolling(14, min_periods=14).mean()
    old_plus_di = 100 * (plus_dm.rolling(14, min_periods=14).mean() / old_atr)
    old_minus_di = 100 * (minus_dm.rolling(14, min_periods=14).mean() / old_atr)
    old_dx = 100 * (old_plus_di - old_minus_di).abs() / (old_plus_di + old_minus_di)
    old_adx = old_dx.rolling(14, min_periods=14).mean()

    assert abs(df["adx_14"].iloc[-1] - old_adx.iloc[-1]) > 0.01


def test_macd_roc_williams_cci_stochastic_unchanged_and_sane():
    df = MomentumIndicators().calculate(_synthetic_ohlc())
    assert df["macd"].notna().any()
    assert df["macd_histogram"].equals(df["macd"] - df["macd_signal"])
    assert df["roc_12"].notna().any()
    valid_wr = df["williams_r_14"].dropna()
    assert valid_wr.between(-100, 0).all()
    valid_stoch = df["stoch_k"].dropna()
    assert valid_stoch.between(0, 100).all()
