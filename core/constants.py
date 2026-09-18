"""
Application-wide constants.

Do not place configurable values here.
Only immutable constants shared across the project.
"""

from __future__ import annotations

from typing import Final

APP_NAME: Final[str] = "Quant Trading Platform"
APP_VERSION: Final[str] = "1.0.0"

BUY: Final[str] = "BUY"
SELL: Final[str] = "SELL"
NO_TRADE: Final[str] = "NO_TRADE"

VALID_SIGNALS: Final[tuple[str, ...]] = (
    BUY,
    SELL,
    NO_TRADE,
)

LONG: Final[str] = "LONG"
SHORT: Final[str] = "SHORT"

NSE: Final[str] = "NSE"
BSE: Final[str] = "BSE"

DATE_FORMAT: Final[str] = "%Y-%m-%d"
DATETIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

DEFAULT_FLOAT_PRECISION: Final[int] = 4

LOG_FORMAT: Final[str] = (
    "%(asctime)s | %(levelname)s | %(name)s | " "%(filename)s:%(lineno)d | %(message)s"
)

# BUGFIX (2026-09-18, Phase 3 — see BUG_AUDIT_2026-09-18.md item #14):
# MAX_OPEN_POSITIONS/MAX_SECTOR_EXPOSURE/MAX_CORRELATION used to be
# defined independently as class attributes in BOTH
# risk/portfolio_rules.py's PortfolioRulesEngine AND
# decision/validation_engine.py's ValidationEngine — two copies of the
# same portfolio-governance limit, hand-kept in sync. They were still in
# sync when this was found, but MAX_OPEN_POSITIONS itself is on record
# (see BUG_AUDIT_2026-09-18.md) as having ALREADY drifted between these
# same two files once before — duplicated constants are a structural
# landmine even while they happen to agree today. Both engines now
# import these from here instead of each defining their own copy, so
# there is exactly one place to change the limit and no way for the two
# to silently disagree again.
MAX_OPEN_POSITIONS: Final[int] = 100
MAX_SECTOR_EXPOSURE: Final[float] = 0.30
MAX_CORRELATION: Final[float] = 0.80
