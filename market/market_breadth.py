"""
Market Breadth Engine.

Responsibilities:
- Advance/Decline statistics
- Advance/Decline Ratio
- Advance/Decline Line
- New High / New Low counts
- Breadth classification

No strategy or decision logic.

BACKLOG NOTE (2026-08-21, user-requested, NOT yet implemented — pending
backtest, do not enable blindly):
This engine exists but is currently UNWIRED — execution/scanner.py's
per-symbol scan sets dataframe["breadth"] = None (FIX #8) because no
market-wide advance/decline feed reaches it today. That None used to
make decision/validation_engine.py's "NaN values detected." check
reject EVERY single candidate, every day (found via
scripts/diagnose_buy_pipeline.py, fixed by excluding "breadth" from
that check alongside "chikou_span" — see validation_engine.py).
Excluding it from the NaN check only stops it from BLOCKING trades; it
does NOT wire up real breadth data — breadth stays None/inert until
someone actually does the below.

Idea discussed for a REAL (non-fabricated) breadth feed, without
needing any new external data source: scripts/generate_full_report.py
/ scripts/daily_scan.py already loop over the full ~500-symbol
watchlist once per day and fetch each symbol's OHLCV (close,
prev_close) as part of the existing per-symbol scan. Advance/decline
counts for THIS engine's REQUIRED_COLUMNS ("advance"/"decline"/
"new_high"/"new_low") could be aggregated from that same loop's
already-fetched closes (close > prev_close = advance, etc.) — no new
API/cost needed, and it would feed this engine with genuinely real
data instead of a fabricated placeholder.

Explicitly NOT scheduled for now: whether real breadth actually
improves this system's win rate is an empirical question, not a given
— strategy/buy_strategy.py's market_context_score already includes
market_regime (EMA50/200) and VIX as broad-market signals, and how
much NEW, non-redundant information breadth would add on top of those
is unknown without a real backtest (breadth-on vs breadth-off, same
candidates/period, compare win rate). Do this only after a real
backtest, once enough closed-trade history exists to run one — not as
a blind "more signals = better" change.
"""

from __future__ import annotations

import numpy as np

import pandas as pd

from core.exceptions import DataError
from core.logger import get_logger

logger = get_logger(__name__)


class MarketBreadthEngine:
    """Compute market breadth metrics."""

    REQUIRED_COLUMNS = {
        "advance",
        "decline",
        "new_high",
        "new_low",
    }

    def evaluate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty:
            raise DataError("Breadth dataframe is empty.")

        missing = self.REQUIRED_COLUMNS.difference(dataframe.columns)
        if missing:
            raise DataError(f"Missing required columns: {sorted(missing)}")

        df = dataframe.copy()

        total = (df["advance"] + df["decline"]).replace(0, np.nan)

        df["ad_ratio"] = df["advance"] / df["decline"].replace(0, np.nan)
        df["ad_percent"] = (df["advance"] / total) * 100
        df["ad_line"] = (df["advance"] - df["decline"]).cumsum()

        df["nh_nl_ratio"] = df["new_high"] / df["new_low"].replace(0, np.nan)

        df["breadth"] = "NEUTRAL"
        df.loc[df["ad_percent"] >= 60, "breadth"] = "STRONG"
        df.loc[df["ad_percent"] <= 40, "breadth"] = "WEAK"

        logger.info("Market breadth calculated.")

        return df
