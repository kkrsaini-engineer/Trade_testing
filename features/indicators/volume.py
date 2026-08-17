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

        # VWAP — NOT a session-reset intraday VWAP. This is a running
        # cumulative typical-price-weighted average from the START of
        # whatever window was fetched (production: ~250 trading days
        # back, interval=1d) — closer to a "long-term baseline price"
        # than the intraday VWAP a broker's chart shows (which resets
        # every trading session; that concept only applies to
        # multiple-candles-per-day data, and production runs on
        # daily bars — one candle IS one session already, so a
        # "session reset" would be a no-op here). Kept as-is (real-data
        # accuracy audit, PHASE30_NOTES.md) — renamed nowhere in code
        # to avoid an invasive column-name change across every
        # consumer; documented here so `checks["price_above_vwap"]` /
        # `price_above_vwap` reads correctly as "above its own recent
        # baseline price," not "above today's session VWAP."
        typical_price = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_pv = (typical_price * df["volume"]).cumsum()
        cumulative_volume = df["volume"].cumsum()
        df["vwap"] = cumulative_pv / cumulative_volume

        # CMF (21) — FIX (real-data accuracy audit, PHASE30_NOTES.md):
        # was a 20-bar window; changed to 21 to match the broker
        # default confirmed via a real side-by-side chart comparison.
        # Column name kept as `cmf_20` (not renamed to `cmf_21`) to
        # avoid an invasive rename across every consumer
        # (buy_strategy.py/sell_strategy.py/buy_scoring.py/
        # sell_scoring.py/tests/diagnostics) for a period-only change —
        # the VALUE is now 21-period, only the column name is legacy.
        CMF_WINDOW = 21
        mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / (
            (df["high"] - df["low"]).replace(0, np.nan)
        )
        mfv = mfm.fillna(0) * df["volume"]
        df["cmf_20"] = (
            mfv.rolling(CMF_WINDOW, min_periods=CMF_WINDOW).sum()
            / df["volume"].rolling(CMF_WINDOW, min_periods=CMF_WINDOW).sum()
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
