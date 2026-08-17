"""
Volatility Indicators.

Responsibilities:
- ATR
- Bollinger Bands
- Keltner Channel
- Donchian Channel

Adds volatility features to the dataframe.
"""

from __future__ import annotations

import pandas as pd

from core.exceptions import IndicatorError
from core.logger import get_logger
from features.indicators.smoothing import wilders_smoothing

logger = get_logger(__name__)


class VolatilityIndicators:
    """Volatility indicator engine."""

    def calculate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise IndicatorError("Empty dataframe received.")

        required = {"high", "low", "close"}
        missing = required.difference(dataframe.columns)
        if missing:
            raise IndicatorError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        # ATR(14)
        # FIX (real-data accuracy audit — see PHASE30_NOTES.md): was a
        # plain 14-bar rolling mean of True Range. Every broker/
        # charting platform uses Wilder's smoothing for ATR (the
        # original definition) — see features/indicators/smoothing.py's
        # docstring for the full before/after evidence. This column
        # feeds the ATR filter check, the overextension cap, position
        # sizing, and (via atr_ma below) volatility_state/market_regime's
        # volatility_regime — all pick up the fix automatically.
        df["atr_14"] = wilders_smoothing(tr, 14)

        # Bollinger Bands(20,2)
        sma20 = df["close"].rolling(20, min_periods=20).mean()
        std20 = df["close"].rolling(20, min_periods=20).std()

        df["bb_middle"] = sma20
        df["bb_upper"] = sma20 + (2 * std20)
        df["bb_lower"] = sma20 - (2 * std20)
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma20

        # Keltner Channel(20)
        # FIX (real-data accuracy audit — see PHASE30_NOTES.md): band
        # width uses the same Wilder's-smoothing fix as atr_14 above
        # (period kept at 20 — only the SMOOTHING METHOD changed, not
        # the period). A real side-by-side against a broker's live
        # chart found the Keltner MIDDLE band (EMA20) matched exactly
        # while the band width didn't — this is that gap's root cause.
        ema20 = df["close"].ewm(span=20, adjust=False).mean()
        atr20 = wilders_smoothing(tr, 20)

        df["kc_middle"] = ema20
        df["kc_upper"] = ema20 + (2 * atr20)
        df["kc_lower"] = ema20 - (2 * atr20)

        # Donchian Channel(20)
        df["dc_upper"] = df["high"].rolling(20, min_periods=20).max()
        df["dc_lower"] = df["low"].rolling(20, min_periods=20).min()
        df["dc_middle"] = (df["dc_upper"] + df["dc_lower"]) / 2

        # Supertrend(10, 3) — required by buy/sell strategy engines.
        # FIX (real-data accuracy audit — see PHASE30_NOTES.md): same
        # Wilder's-smoothing fix as atr_14/Keltner above (period kept
        # at 10, only the smoothing method changed). Supertrend's
        # boolean direction feeds TREND_CORE in tier2_score, so this is
        # a real (if usually small) behavior change, not cosmetic —
        # flagging explicitly rather than burying it in the ATR fix.
        st_period = 10
        st_multiplier = 3.0
        st_atr = wilders_smoothing(tr, st_period)
        hl2 = (df["high"] + df["low"]) / 2
        upperband = hl2 + (st_multiplier * st_atr)
        lowerband = hl2 - (st_multiplier * st_atr)

        supertrend = pd.Series(index=df.index, dtype="float64")
        direction = pd.Series(index=df.index, dtype="bool")  # True = uptrend

        for i in range(len(df)):
            if i == 0 or pd.isna(upperband.iloc[i - 1]):
                direction.iloc[i] = True
                supertrend.iloc[i] = lowerband.iloc[i]
                continue

            prev_supertrend = supertrend.iloc[i - 1]
            close_prev = df["close"].iloc[i - 1]

            curr_upper = (
                upperband.iloc[i]
                if (upperband.iloc[i] < prev_supertrend or close_prev > prev_supertrend)
                else prev_supertrend
            )
            curr_lower = (
                lowerband.iloc[i]
                if (lowerband.iloc[i] > prev_supertrend or close_prev < prev_supertrend)
                else prev_supertrend
            )

            if direction.iloc[i - 1] and df["close"].iloc[i] < curr_lower:
                direction.iloc[i] = False
            elif (not direction.iloc[i - 1]) and df["close"].iloc[i] > curr_upper:
                direction.iloc[i] = True
            else:
                direction.iloc[i] = direction.iloc[i - 1]

            supertrend.iloc[i] = curr_lower if direction.iloc[i] else curr_upper

        # True (uptrend / price above supertrend line) is treated as bullish signal.
        df["supertrend"] = direction

        # Volatility regime state, used by risk + strategy engines.
        atr_ma = df["atr_14"].rolling(20, min_periods=20).mean()
        df["volatility_state"] = "NORMAL"
        df.loc[df["atr_14"] > atr_ma * 1.20, "volatility_state"] = "HIGH"
        df.loc[df["atr_14"] < atr_ma * 0.80, "volatility_state"] = "LOW"

        logger.info("Volatility indicators calculated.")

        return df
