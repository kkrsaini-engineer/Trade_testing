"""
Feature Engineering Engine.

Responsibilities:
- Validate market data
- Generate all technical features
- Merge features into a single dataframe
- No BUY/SELL logic
- No scoring
"""

from __future__ import annotations

import pandas as pd

from core.exceptions import DataError
from core.logger import get_logger
from features.technical_features import TechnicalFeatureEngine

logger = get_logger(__name__)


class FeatureEngineeringEngine:
    """Coordinates feature generation."""

    REQUIRED_COLUMNS = (
        "open",
        "high",
        "low",
        "close",
        "volume",
    )

    def __init__(
        self,
        technical_engine: TechnicalFeatureEngine | None = None,
    ) -> None:
        self._technical = technical_engine or TechnicalFeatureEngine()
        # features.multi_timeframe.MultiTimeframeEngine used to run here —
        # disconnected (found during an architecture review): it computed
        # "mtf_sma_20/50/200"/"mtf_trend" as plain rolling means of the
        # SAME daily close series (no real weekly/4H resampling — despite
        # the "multi-timeframe" name), and those columns were never read
        # by any BUY/SELL check or downstream consumer (verified: zero
        # references anywhere in the codebase). Running it every scan was
        # pure wasted computation for output nobody used. The module
        # itself is left in place (features/multi_timeframe.py) as a
        # starting point if genuine multi-timeframe support (real
        # weekly/4H OHLCV resampling) gets built later — see that file's
        # docstring. If you want it fully removed from the repo, it needs
        # deleting on GitHub directly (a zip upload can add/overwrite
        # files but can't delete existing ones).

    def generate(self, market_data: pd.DataFrame) -> pd.DataFrame:
        """
        Generate all engineered features.

        Args:
            market_data: Normalized OHLCV dataframe.

        Returns:
            DataFrame containing original columns plus engineered features.
        """
        self._validate(market_data)

        logger.info("Generating technical features...")
        df = self._technical.generate(market_data.copy())

        logger.info("Feature engineering complete.")

        return df

    def _validate(self, dataframe: pd.DataFrame) -> None:
        if dataframe.empty:
            raise DataError("Market dataframe is empty.")

        missing = [
            column
            for column in self.REQUIRED_COLUMNS
            if column not in dataframe.columns
        ]

        if missing:
            raise DataError(f"Missing required market columns: {missing}")
