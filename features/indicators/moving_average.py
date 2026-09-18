"""
Moving Average Indicators.

Responsibilities:
- SMA
- EMA
- WMA
- VWMA

Adds columns to the incoming dataframe.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.exceptions import IndicatorError
from core.logger import get_logger

logger = get_logger(__name__)


class MovingAverageIndicators:
    """Moving average indicator engine."""

    def calculate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise IndicatorError("Empty dataframe received.")

        required = {"close", "volume"}
        missing = required.difference(dataframe.columns)
        if missing:
            raise IndicatorError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        # Simple Moving Averages
        for period in (20, 50, 100, 200):
            df[f"sma_{period}"] = (
                df["close"].rolling(window=period, min_periods=period).mean()
            )

        # Exponential Moving Averages
        for period in (9, 20, 50, 100, 200):
            df[f"ema_{period}"] = df["close"].ewm(span=period, adjust=False).mean()

        # Weighted Moving Average
        #
        # BUGFIX (2026-09-18, Phase 4 dead-code/audit cleanup): the old
        # version passed `raw=False` to `.apply()`, so `prices` arrived as
        # a `pd.Series` carrying the ORIGINAL dataframe's row index (e.g.
        # [37, 38, ..., 56] for one 20-row window), while `weights` was a
        # fresh `pd.Series(range(1, period + 1))` with index [0, 1, ...,
        # period - 1]. `prices * weights` aligns by INDEX, not position --
        # since the two indices essentially never matched, almost every
        # product came out NaN, and `.sum()` on an all-NaN Series returns
        # 0.0 (pandas default `skipna=True`). That silently made every
        # `wma_20`/`wma_50` value 0.0 regardless of the actual price data.
        # Fixed by using `raw=True`, which passes `prices` as a plain
        # numpy ndarray (purely positional, no index) so the elementwise
        # multiply against the numpy `weights` array lines up correctly.
        for period in (20, 50):
            weights = np.arange(1, period + 1, dtype="float64")

            df[f"wma_{period}"] = (
                df["close"]
                .rolling(period)
                .apply(
                    lambda prices: (prices * weights).sum() / weights.sum(),
                    raw=True,
                )
            )

        # Volume Weighted Moving Average
        for period in (20, 50):
            pv = df["close"] * df["volume"]
            df[f"vwma_{period}"] = (
                pv.rolling(period).sum() / df["volume"].rolling(period).sum()
            )

        logger.info("Moving average indicators calculated.")

        return df
