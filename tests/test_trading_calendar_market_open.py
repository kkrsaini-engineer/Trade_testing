"""
Tests for core/trading_calendar.py's market_open_now() (Phase 26, see
PHASE26_NOTES.md) — factored out of orchestrator.py's and
execution/scanner.py's prepare_orders(), both of which used to hardcode
market_open=True/holiday=False unconditionally.
"""

from datetime import datetime

from core.trading_calendar import market_open_now


def _ist(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm)


def test_open_during_normal_session_hours():
    # 2026-08-17 is a Monday, not an NSE holiday.
    assert market_open_now(_ist(2026, 8, 17, 11, 0)) is True


def test_closed_before_market_open_time():
    assert market_open_now(_ist(2026, 8, 17, 9, 0)) is False


def test_closed_after_market_close_time():
    assert market_open_now(_ist(2026, 8, 17, 16, 0)) is False


def test_open_exactly_at_open_boundary():
    assert market_open_now(_ist(2026, 8, 17, 9, 15)) is True


def test_open_exactly_at_close_boundary():
    assert market_open_now(_ist(2026, 8, 17, 15, 30)) is True


def test_closed_on_weekend_even_during_session_hours():
    # 2026-08-16 is a Sunday.
    assert market_open_now(_ist(2026, 8, 16, 11, 0)) is False


def test_closed_on_nse_holiday_even_during_session_hours():
    # 2026-01-26 is Republic Day (see core/trading_calendar.py's
    # NSE_HOLIDAYS_2026).
    assert market_open_now(_ist(2026, 1, 26, 11, 0)) is False
