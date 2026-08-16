"""
Volume Indicators.

Responsibilities:
- OBV
- CMF
- MFI
- Volume SMA
- VWAP

Adds volume-based features to the dataframe.
"""

from __future__ import annotations

import numpy as np

import pandas as pd

from core.exceptions import IndicatorError
from core.logger import get_logger

logger = get_logger(__name__)


class VolumeIndicators:
    """Volume indicator engine."""

    def calculate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise IndicatorError("Empty dataframe received.")

        required = {"high", "low", "close", "volume"}
        missing = required.difference(dataframe.columns)
        if missing:
            raise IndicatorError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        # Volume SMA
        df["volume_sma_20"] = df["volume"].rolling(20, min_periods=20).mean()

        # OBV — FIX (user review): was an unbounded cumsum() from
        # whichever day the fetched dataframe happens to start (in
        # production, ~250 trading days back). That made
        # `checks["obv"] > 0`/`< 0` a "net tally over the last ~year"
        # test, dominated by whichever single day had the largest
        # signed volume anywhere in that year — not a "current trend"
        # signal at all, despite feeding a check literally named
        # accumulation/distribution. Changed to a rolling 20-day sum,
        # matching the window every other check in this pipeline uses
        # (CMF is already 20-day — OBV and CMF were previously
        # operating on mismatched time horizons despite being AND'ed
        # together in checks["accumulation"]/checks["distribution"]).
        # Interpretation is unchanged (positive = net buying pressure,
        # negative = net selling pressure) — only the window is fixed
        # to the recent 20 days instead of all-time-since-fetch.
        OBV_WINDOW = 20

        direction = df["close"].diff().fillna(0)
        signed_volume = df["volume"].where(direction >= 0, -df["volume"])
        signed_volume = signed_volume.where(direction != 0, 0)
        df["obv"] = signed_volume.rolling(OBV_WINDOW, min_periods=OBV_WINDOW).sum()

        # VWAP (cumulative)
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_pv = (typical_price * df["volume"]).cumsum()
        cumulative_volume = df["volume"].cumsum()
        df["vwap"] = cumulative_pv / cumulative_volume

        # CMF (20)
        mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (
            (df["high"] - df["low"]).replace(0, np.nan)
        )
        mfv = mfm.fillna(0) * df["volume"]
        df["cmf_20"] = (
            mfv.rolling(20, min_periods=20).sum()
            / df["volume"].rolling(20, min_periods=20).sum()
        )

        # MFI (14)
        raw_money_flow = typical_price * df["volume"]
        tp_diff = typical_price.diff()

        positive_flow = raw_money_flow.where(tp_diff > 0, 0.0)
        negative_flow = raw_money_flow.where(tp_diff < 0, 0.0).abs()

        pos_sum = positive_flow.rolling(14, min_periods=14).sum()
        neg_sum = negative_flow.rolling(14, min_periods=14).sum()

        money_ratio = pos_sum / neg_sum.replace(0, np.nan)
        df["mfi_14"] = 100 - (100 / (1 + money_ratio))

        logger.info("Volume indicators calculated.")

        return df
