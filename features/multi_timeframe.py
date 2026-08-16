"""
Multi-Timeframe Feature Engine.

*** NOT CURRENTLY WIRED IN (disconnected during an architecture review) ***
features/feature_engineering.py no longer calls this. Two problems found:

1. Despite the name, this does NOT do real multi-timeframe analysis — no
   weekly/4H OHLCV resampling happens anywhere in this file. `mtf_sma_20/
   50/200` below are plain rolling means of the SAME daily close series
   already used for the regular `sma_20/50/200` columns elsewhere —
   effectively a relabeled duplicate, not a higher timeframe.
2. Even when it WAS wired in, `mtf_sma_*`/`mtf_trend` were never read by
   any BUY/SELL check or any other downstream consumer (verified: zero
   references anywhere else in the codebase) — dead output, computed
   every single scan for nothing.

Left in place as a starting point, not as something ready to re-enable.
Genuine multi-timeframe support would need real weekly/4H OHLCV data
sourced and resampled (a real DataEngine change, not just this file),
answering a genuinely different question per timeframe (e.g. weekly =
macro trend, daily = setup, 4H = entry confirmation) rather than
recomputing the same daily rolling averages under a different name.

Responsibilities (as originally intended, not as currently implemented):
- Generate higher timeframe context from normalized OHLCV data.
- Add derived columns only.
- No strategy, scoring, or decision logic.
"""

from __future__ import annotations

import pandas as pd

from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)


class MultiTimeframeEngine:
    """Generate higher-timeframe trend features."""

    def generate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """
        Enrich a dataframe with higher-timeframe features.

        Args:
            dataframe: Normalized OHLCV dataframe.

        Returns:
            DataFrame with additional multi-timeframe columns.
        """
        if dataframe.empty:
            raise DataError(
                "Cannot generate multi-timeframe features from empty dataframe."
            )

        if "close" not in dataframe.columns:
            raise DataError("Required column 'close' is missing.")

        df = dataframe.copy()

        # Higher timeframe trend approximations.
        df["mtf_sma_20"] = df["close"].rolling(20, min_periods=20).mean()
        df["mtf_sma_50"] = df["close"].rolling(50, min_periods=50).mean()
        df["mtf_sma_200"] = df["close"].rolling(200, min_periods=200).mean()

        df["mtf_trend"] = "SIDEWAYS"
        df.loc[
            (df["mtf_sma_20"] > df["mtf_sma_50"])
            & (df["mtf_sma_50"] > df["mtf_sma_200"]),
            "mtf_trend",
        ] = "BULL"

        df.loc[
            (df["mtf_sma_20"] < df["mtf_sma_50"])
            & (df["mtf_sma_50"] < df["mtf_sma_200"]),
            "mtf_trend",
        ] = "BEAR"

        logger.info("Multi-timeframe features generated.")

        return df
