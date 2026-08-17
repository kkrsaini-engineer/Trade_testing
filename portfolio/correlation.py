"""
Portfolio Correlation (Phase 26 — see PHASE26_NOTES.md, point 11).

Responsibilities:
- Compute a real average pairwise correlation across the CURRENTLY OPEN
  positions' daily returns, replacing the `portfolio.get("correlation",
  0.0)` input that (before this phase) no producer anywhere in the
  codebase ever actually populated — it was always the 0.0 default,
  everywhere it was read (risk_manager.py, validation_engine.py,
  portfolio_rules.py).
- Fetch the historical closes needed for that computation.

No strategy/decision/risk-scoring logic lives here — this module only
computes a number; callers (paper_trading_engine.py,
scripts/generate_full_report.py) decide what to do with it.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_LOOKBACK_DAYS = 20
DEFAULT_MIN_POSITIONS = 2
DEFAULT_MIN_OVERLAPPING_DAYS = 10


def compute_portfolio_correlation(
    closes: dict[str, pd.Series],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_positions: int = DEFAULT_MIN_POSITIONS,
    min_overlapping_days: int = DEFAULT_MIN_OVERLAPPING_DAYS,
) -> float | None:
    """Average pairwise Pearson correlation of daily returns across the
    given symbols' close-price series, over the last `lookback_days`
    trading days.

    Returns None — NEVER 0.0 — when correlation cannot be meaningfully
    computed (fewer than `min_positions` symbols with data, or fewer
    than `min_overlapping_days` overlapping trading days once aligned).
    0.0 is a specific CLAIM ("these positions are genuinely
    uncorrelated") that must not be fabricated when the honest answer
    is "not enough data to know" — callers must treat None as "leave
    the existing default in place", not "assume 0 correlation" (see
    the callers' own docstrings for how they handle this).

    A negative result (diversifying positions) is reported as-is, not
    clipped to 0 — that is real, useful information for the downstream
    `correlation <= MAX_CORRELATION` checks.
    """
    if len(closes) < min_positions:
        return None

    combined = pd.DataFrame(closes)

    # Aligns by the DatetimeIndex each Series carries (from
    # data/market_data.py's fetch()) — NOT by raw row position, so a
    # symbol with a different trading history (e.g. newly listed) still
    # lines up correctly on shared calendar days.
    combined = combined.tail(lookback_days + 1)  # +1: pct_change() drops the first row

    returns = combined.pct_change().dropna(how="any")

    if len(returns) < min_overlapping_days or returns.shape[1] < min_positions:
        return None

    corr_matrix = returns.corr()
    symbols = list(returns.columns)

    pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            value = corr_matrix.iloc[i, j]
            if pd.notna(value):
                pairs.append(float(value))

    if not pairs:
        return None

    return sum(pairs) / len(pairs)


def fetch_correlation_inputs(
    symbols: Iterable[str],
    market_provider: object,
    period: str = "2mo",
) -> dict[str, pd.Series]:
    """Fetches recent daily closes for each symbol via `market_provider`
    (expects a `.fetch(symbol=..., period=...) -> pd.DataFrame` — see
    data/market_data.py's MarketDataProvider, the SAME provider already
    used elsewhere in this codebase; deliberately NOT the full
    DataEngine bundle — correlation only needs OHLCV closes, not
    fundamentals/news, so this avoids doubling those fetches).

    Symbols whose fetch fails, or that return no usable "close" column,
    are silently skipped (not substituted with fabricated data) —
    compute_portfolio_correlation() above already handles "too few
    symbols" by returning None rather than a fake number.
    """
    closes: dict[str, pd.Series] = {}

    for symbol in symbols:
        try:
            dataframe = market_provider.fetch(symbol=symbol, period=period)
        except Exception as exc:
            logger.warning("Correlation input fetch failed for %s: %s", symbol, exc)
            continue

        if dataframe is None or dataframe.empty or "close" not in dataframe.columns:
            continue

        closes[symbol] = dataframe["close"]

    return closes
