"""
NSE TRADING CALENDAR

Determines whether a given date is an NSE trading day (not Saturday, not
Sunday, not an official NSE holiday), and counts holding days in TRADING
SESSIONS — not raw calendar days.

Holiday source: NSE circular NSE/CMTR/71775 (Dec 12, 2025), "Trading
holidays for the calendar year 2026". This list must be refreshed every
year — NSE publishes a new circular in December for the following year
(festival-based holidays like Holi/Diwali/Eid shift dates annually since
they follow lunar/regional calendars, unlike Republic Day/Independence
Day/Christmas which are fixed).

Source: https://www.nseindia.com/resources/exchange-communication-holidays
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

IST_OFFSET = timedelta(hours=5, minutes=30)

# NSE 2026 trading holidays (weekday ones only — the 4 that fall on a
# Saturday/Sunday are already non-trading days and listed separately by
# NSE for information only; they don't need to be in this set since
# is_trading_day() already excludes all Saturdays/Sundays).
NSE_HOLIDAYS_2026: set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali-Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
}

# Merge in any other year's holidays here as they're published. Keyed by
# the holiday's own date so multiple years can coexist without conflict.
ALL_NSE_HOLIDAYS: set[date] = set(NSE_HOLIDAYS_2026)


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    if d in ALL_NSE_HOLIDAYS:
        return False
    return True


def skip_reason(d: date | None = None) -> str | None:
    """Why `d` is not an NSE trading day: "Saturday", "Sunday", "NSE
    Holiday", or None if it IS a trading day. Used only for notification
    text — is_trading_day() remains the single source of truth for the
    actual skip decision."""
    d = d or date.today()
    if d.weekday() == 5:
        return "Saturday"
    if d.weekday() == 6:
        return "Sunday"
    if d in ALL_NSE_HOLIDAYS:
        return "NSE Holiday"
    return None


def now_ist() -> datetime:
    """Current wall-clock time in IST (UTC+5:30), for notification
    timestamps only — not used anywhere in trading-day logic."""
    return datetime.now(timezone.utc) + IST_OFFSET


# NSE normal equity market session: 9:15 AM - 3:30 PM IST.
MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)


def market_open_now(now: datetime | None = None) -> bool:
    """Whether NSE is genuinely open for trading RIGHT NOW — a trading
    day (see is_trading_day()) AND within normal session hours. Phase 26
    (see PHASE26_NOTES.md): factored out of orchestrator.py's and
    execution/scanner.py's `prepare_orders()` — both used to hardcode
    market_open=True unconditionally, so ValidationEngine's market-hours
    check could never actually fire."""
    now = now or now_ist()
    return is_trading_day(now.date()) and MARKET_OPEN_TIME <= now.time() <= MARKET_CLOSE_TIME


def previous_trading_day(d: date | None = None) -> date:
    d = (d or date.today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date | None = None) -> date:
    d = (d or date.today()) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_sessions_between(start: date, end: date) -> int:
    """Count NSE trading sessions strictly between start and end
    (exclusive of start, inclusive of end) — used for Holding Days so
    Saturday/Sunday/holidays never get counted.

    Example: Friday entry -> Monday (with a holiday on the Monday after,
    say) = however many actual trading sessions occurred in between,
    not raw calendar days.
    """
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count
