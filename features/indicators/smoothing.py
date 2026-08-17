"""
Shared smoothing helper for indicators that need Wilder's smoothing
(RSI, ADX, ATR) rather than a plain rolling mean.

FIX (real-data accuracy audit — see PHASE30_NOTES.md): momentum.py's RSI/
ADX and volatility.py's ATR (and everything built on top of ATR — the
Keltner Channel band width, the Supertrend line) were all using a plain
N-period rolling mean (`.rolling(period, min_periods=period).mean()`,
every bar in the window weighted equally). A real side-by-side run
against a broker's live chart for the SAME symbol/candle found this
produces materially different numbers from what every broker/charting
platform actually shows — e.g. an ADX reading in a different regime
bucket entirely (24.80 "BASELINE" here vs 14.69 "RANGE_BOUND" on the
broker chart), confirmed on real market data before this fix.

Wilder's smoothing (J. Welles Wilder Jr.'s original RSI/ADX/ATR method)
is an exponentially-weighted moving average with decay factor
alpha = 1/period — this is the de facto industry-standard smoothing for
these three specific indicators (most other indicators in this codebase
— SMA, Bollinger, CCI, Stochastic, etc. — genuinely ARE plain rolling
means/windows by their own standard definitions, and are UNCHANGED by
this fix; only RSI/ADX/ATR's specific smoothing step was wrong).

Implementation note: pandas' `.ewm(alpha=1/period, adjust=False)` seeds
from the very first data point rather than an initial N-period SMA
(which is how Wilder's original hand calculation seeds it). The two
seeding methods converge to the same values after roughly 3-5x `period`
bars — the seed's influence decays geometrically. Production fetches
~250 bars (interval=1d, period=1y) before any check ever reads the
LATEST row, so this seeding difference has fully converged away by the
time it reaches a row anything in this codebase actually uses. Using
`.ewm()` directly avoids hand-rolling a stateful loop for a difference
that doesn't survive to the row that matters.
"""

from __future__ import annotations

import pandas as pd


def wilders_smoothing(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (alpha = 1/period). NaN for the first
    `period - 1` bars of `series` — the same "needs a full window
    before it means anything" behavior as the
    `rolling(period, min_periods=period)` calls this replaces (a
    series that already has its own leading NaNs, e.g. because it's
    itself built from an earlier Wilder's-smoothed column, is left as
    pandas' `.ewm()` naturally handles that — it doesn't manufacture a
    value before the first real input)."""
    smoothed = series.ewm(alpha=1.0 / period, adjust=False).mean()
    positions = pd.Series(range(len(series)), index=series.index)
    return smoothed.mask(positions < (period - 1))
