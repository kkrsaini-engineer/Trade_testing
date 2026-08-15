"""
Market Volatility Engine.

Responsibilities:
- Historical Volatility
- ATR Regime
- Volatility Percentile
- Volatility State
- India VIX fetch (live, real-data — see fetch_india_vix())

No strategy or decision logic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)

# Fallback used ONLY when the live fetch genuinely fails (network error,
# empty response, etc.) — 20.0 is a neutral/"calm market" placeholder,
# same value that used to be hardcoded everywhere. It must stay clearly
# distinguishable from a real reading in logs, which is why every call
# site logs a warning when this path is hit instead of silently
# proceeding as if it were live data.
_VIX_FALLBACK = 20.0

_VIX_TICKER = "^INDIAVIX"


def fetch_india_vix() -> float:
    """Fetch the latest India VIX close from yfinance.

    Previously every market_state dict in this codebase hardcoded
    "vix": 20 — meaning risk_manager.py's VIX-based risk checks
    (vix >= 30 / vix >= 35) could never fire regardless of actual
    market conditions. This fetches a real, current value using the
    same yfinance path already used for every other symbol in this
    repo (no new dependency).
    """
    try:
        # Local import to avoid making yfinance a hard import-time
        # dependency of this module for callers that never need VIX
        # (e.g. backtest replays that already have a vix column).
        from data.market_data import MarketDataProvider

        provider = MarketDataProvider()
        df = provider.fetch(symbol=_VIX_TICKER, interval="1d", period="5d")
        if df.empty:
            logger.warning(
                "India VIX fetch returned no data — using fallback value %.1f.",
                _VIX_FALLBACK,
            )
            return _VIX_FALLBACK

        vix_value = float(df["close"].iloc[-1])
        if vix_value <= 0:
            logger.warning(
                "India VIX fetch returned a non-positive value (%.2f) — "
                "using fallback value %.1f.",
                vix_value,
                _VIX_FALLBACK,
            )
            return _VIX_FALLBACK

        logger.info("India VIX fetched: %.2f", vix_value)
        return round(vix_value, 2)

    except Exception as exc:
        logger.warning(
            "India VIX fetch failed (%s) — using fallback value %.1f. "
            "Risk checks that depend on VIX will run against this "
            "fallback, not a live reading.",
            exc,
            _VIX_FALLBACK,
        )
        return _VIX_FALLBACK


class MarketVolatilityEngine:
    """Analyze market volatility."""

    REQUIRED_COLUMNS = {"close", "atr_14"}

    def evaluate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise DataError("Empty dataframe received.")

        missing = self.REQUIRED_COLUMNS.difference(dataframe.columns)
        if missing:
            raise DataError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        returns = np.log(df["close"] / df["close"].shift(1))
        df["historical_volatility"] = returns.rolling(
            20, min_periods=20
        ).std() * np.sqrt(252)

        atr_mean = df["atr_14"].rolling(50, min_periods=50).mean()
        df["atr_ratio"] = df["atr_14"] / atr_mean

        df["volatility_percentile"] = (
            df["historical_volatility"].rolling(252, min_periods=20).rank(pct=True)
            * 100
        )

        df["volatility_state"] = "NORMAL"
        df.loc[df["atr_ratio"] >= 1.20, "volatility_state"] = "HIGH"
        df.loc[df["atr_ratio"] <= 0.80, "volatility_state"] = "LOW"

        logger.info("Market volatility evaluated.")

        return df
