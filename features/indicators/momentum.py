"""
Momentum Indicators.

Responsibilities:
- RSI
- MACD
- ROC
- Williams %R
- CCI
- Stochastic Oscillator

Adds momentum features to the incoming dataframe.
"""

from __future__ import annotations

import numpy as np

import pandas as pd

from core.exceptions import IndicatorError
from core.logger import get_logger
from features.indicators.smoothing import wilders_smoothing

logger = get_logger(__name__)


class MomentumIndicators:
    """Momentum indicator engine."""

    def calculate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise IndicatorError("Empty dataframe received.")

        required = {"high", "low", "close"}
        missing = required.difference(dataframe.columns)
        if missing:
            raise IndicatorError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        # RSI(14)
        # FIX (real-data accuracy audit — see PHASE30_NOTES.md): was a
        # plain 14-bar rolling mean of gain/loss. Every broker/charting
        # platform uses Wilder's smoothing here (the original RSI
        # definition) — see features/indicators/smoothing.py's
        # docstring for the full before/after evidence.
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = wilders_smoothing(gain, 14)
        avg_loss = wilders_smoothing(loss, 14)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi_14"] = 100 - (100 / (1 + rs))

        # MACD (12,26,9)
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["macd_histogram"] = df["macd"] - df["macd_signal"]

        # ROC(12)
        df["roc_12"] = df["close"].pct_change(12) * 100

        # Williams %R(14)
        hh = df["high"].rolling(14, min_periods=14).max()
        ll = df["low"].rolling(14, min_periods=14).min()
        df["williams_r_14"] = -100 * ((hh - df["close"]) / (hh - ll))

        # CCI(20)
        tp = (df["high"] + df["low"] + df["close"]) / 3
        sma_tp = tp.rolling(20, min_periods=20).mean()
        mad = tp.rolling(20, min_periods=20).apply(
            lambda x: (x - x.mean()).abs().mean(),
            raw=False,
        )
        df["cci_20"] = (tp - sma_tp) / (0.015 * mad)

        # Stochastic (14,3)
        low14 = df["low"].rolling(14, min_periods=14).min()
        high14 = df["high"].rolling(14, min_periods=14).max()
        df["stoch_k"] = 100 * (df["close"] - low14) / (high14 - low14)
        df["stoch_d"] = df["stoch_k"].rolling(3, min_periods=3).mean()
        # Aliases: sell_strategy.py expects these exact column names.
        df["stochastic_k"] = df["stoch_k"]
        df["stochastic_d"] = df["stoch_d"]

        # ADX(14) — trend strength, required by buy/sell strategy engines.
        prev_close = df["close"].shift(1)
        prev_high = df["high"].shift(1)
        prev_low = df["low"].shift(1)

        up_move = df["high"] - prev_high
        down_move = prev_low - df["low"]

        plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
        minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move

        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        # FIX (real-data accuracy audit — see PHASE30_NOTES.md): every
        # smoothing step in real Wilder's ADX (TR, +DM, -DM, and the
        # final DX average) uses Wilder's smoothing, not a plain
        # rolling mean — this was using plain rolling means for all
        # four, which was the direct cause of a real ADX regime
        # misclassification found against a broker's live chart
        # (BASELINE here vs RANGE_BOUND on the broker, same candle).
        atr_14 = wilders_smoothing(tr, 14)
        plus_di = 100 * (wilders_smoothing(plus_dm, 14) / atr_14)
        minus_di = 100 * (wilders_smoothing(minus_dm, 14) / atr_14)

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        df["adx_14"] = wilders_smoothing(dx, 14)

        logger.info("Momentum indicators calculated.")

        return df
