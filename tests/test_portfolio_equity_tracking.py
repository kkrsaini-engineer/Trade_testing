"""
Tests for portfolio/portfolio.py's Phase 22 equity-tracking additions —
`PortfolioEngine.update_equity_tracking()` and the new `max_drawdown` /
`daily_loss` / `emergency_stop` fields in `snapshot()`.

See PHASE21_NOTES.md's "9 + 10" section for how it was discovered that
these inputs were never populated anywhere in the live pipeline, and
PHASE22_NOTES.md for the wiring implemented here.
"""

from portfolio.portfolio import PortfolioEngine, PortfolioState, PortfolioPosition


def _engine(total_capital=100_000.0):
    state = PortfolioState(
        total_capital=total_capital,
        available_capital=total_capital,
    )
    return PortfolioEngine(state=state)


# ==========================================================
# FIRST CALL — establishes the initial baseline
# ==========================================================

def test_first_call_sets_peak_and_day_start_to_current_equity():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")
    assert engine.state.peak_equity == 100_000.0
    assert engine.state.day_start_equity == 100_000.0
    assert engine.state.current_trading_day == "2026-08-17"

    snap = engine.snapshot()
    assert snap["max_drawdown"] == 0.0
    assert snap["daily_loss"] == 0.0
    assert snap["emergency_stop"] is False


# ==========================================================
# DRAWDOWN — peak never regresses, survives across day changes
# ==========================================================

def test_drawdown_reflects_drop_from_peak():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")  # peak = 100,000

    # Portfolio value drops to 90,000 (position marked down).
    engine.state.available_capital = 90_000.0
    engine.update_equity_tracking("2026-08-17")  # same day, peak untouched

    snap = engine.snapshot()
    assert snap["max_drawdown"] == 0.10  # (100,000 - 90,000) / 100,000


def test_peak_equity_never_regresses_on_a_pullback():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 120_000.0
    engine.update_equity_tracking("2026-08-17")  # new peak = 120,000
    assert engine.state.peak_equity == 120_000.0

    engine.state.available_capital = 110_000.0
    engine.update_equity_tracking("2026-08-17")  # pullback, peak must hold
    assert engine.state.peak_equity == 120_000.0

    snap = engine.snapshot()
    assert round(snap["max_drawdown"], 4) == round((120_000.0 - 110_000.0) / 120_000.0, 4)


# ==========================================================
# DAILY LOSS — resets on a new trading day, holds steady within a day
# ==========================================================

def test_daily_loss_measured_against_start_of_day_equity():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")  # day_start = 100,000

    engine.state.available_capital = 96_000.0
    engine.update_equity_tracking("2026-08-17")  # same day

    snap = engine.snapshot()
    assert snap["daily_loss"] == 0.04  # (100,000 - 96,000) / 100,000


def test_daily_loss_baseline_resets_on_new_trading_day():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 96_000.0
    engine.update_equity_tracking("2026-08-17")  # daily_loss = 4% today

    # New trading day starts at 96,000 — that becomes the new baseline,
    # even though it's below the ORIGINAL 100,000. Yesterday's loss must
    # not carry over into today's daily_loss figure.
    engine.update_equity_tracking("2026-08-18")
    assert engine.state.day_start_equity == 96_000.0

    snap = engine.snapshot()
    assert snap["daily_loss"] == 0.0  # flat vs today's own baseline


def test_daily_loss_never_negative_on_a_gain_day():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 105_000.0
    engine.update_equity_tracking("2026-08-17")

    snap = engine.snapshot()
    assert snap["daily_loss"] == 0.0  # a gain, not a "negative loss"


# ==========================================================
# EMERGENCY STOP — auto-linked to the graduated systems' most severe stage
# ==========================================================

def test_emergency_stop_true_when_daily_loss_hits_emergency_stage():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 94_000.0  # 6% down -> emergency stage
    engine.update_equity_tracking("2026-08-17")

    snap = engine.snapshot()
    assert snap["daily_loss"] >= 0.05
    assert snap["emergency_stop"] is True


def test_emergency_stop_false_at_a_milder_stage():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 97_000.0  # 3% down -> risk_reduction
    engine.update_equity_tracking("2026-08-17")

    snap = engine.snapshot()
    assert snap["emergency_stop"] is False


# ==========================================================
# open positions ARE part of equity (not just available_capital)
# ==========================================================

def test_open_position_market_value_counts_toward_equity():
    engine = _engine(100_000.0)
    engine.state.available_capital = 50_000.0
    engine.state.open_positions["RELIANCE"] = PortfolioPosition(
        symbol="RELIANCE", quantity=100, entry_price=500.0, current_price=500.0,
        direction="BUY",
    )
    # equity = 50,000 cash + 100*500 = 100,000 -> matches total_capital,
    # i.e. no phantom loss just because capital moved from cash into a
    # position.
    engine.update_equity_tracking("2026-08-17")
    assert engine.state.peak_equity == 100_000.0

    # Position appreciates -> equity rises above the original peak.
    engine.state.open_positions["RELIANCE"].current_price = 520.0
    engine.update_equity_tracking("2026-08-17")
    assert engine.state.peak_equity == 102_000.0


# ==========================================================
# WEEKLY / MONTHLY LOSS — Phase 23 (see PHASE23_NOTES.md). Same pattern
# as daily_loss above, but reset on ISO-week / calendar-month boundaries
# instead of every trading day. 2026-08-17 (Mon) and 2026-08-19 (Wed) are
# the same ISO week (34) and month (08). 2026-08-24 (Mon) is a new week,
# same month. 2026-09-01 (Tue) is a new month.
# ==========================================================

def test_weekly_loss_holds_steady_within_the_same_iso_week():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")  # week_start = 100,000

    engine.state.available_capital = 95_000.0
    engine.update_equity_tracking("2026-08-19")  # still week 34

    snap = engine.snapshot()
    assert snap["weekly_loss"] == 0.05  # (100,000 - 95,000) / 100,000
    assert engine.state.week_start_equity == 100_000.0


def test_weekly_loss_baseline_resets_on_new_iso_week():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 95_000.0
    engine.update_equity_tracking("2026-08-19")  # 5% down, week 34

    engine.update_equity_tracking("2026-08-24")  # new ISO week (35)
    assert engine.state.week_start_equity == 95_000.0

    snap = engine.snapshot()
    assert snap["weekly_loss"] == 0.0  # flat vs this week's own baseline


def test_monthly_loss_holds_steady_within_the_same_month():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")  # month_start = 100,000

    engine.state.available_capital = 88_000.0
    engine.update_equity_tracking("2026-08-24")  # new week, same month (08)

    snap = engine.snapshot()
    assert snap["monthly_loss"] == 0.12  # (100,000 - 88,000) / 100,000
    assert engine.state.month_start_equity == 100_000.0


def test_monthly_loss_baseline_resets_on_new_month():
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")

    engine.state.available_capital = 88_000.0
    engine.update_equity_tracking("2026-08-31")  # still August

    engine.update_equity_tracking("2026-09-01")  # new month
    assert engine.state.month_start_equity == 88_000.0

    snap = engine.snapshot()
    assert snap["monthly_loss"] == 0.0


def test_new_week_does_not_force_a_new_month_baseline_and_vice_versa():
    # 2026-08-24 is a new ISO week (35) but the SAME month (08) as
    # 2026-08-17 — month_start_equity must not reset just because the
    # week did.
    engine = _engine(100_000.0)
    engine.update_equity_tracking("2026-08-17")
    original_month_start = engine.state.month_start_equity

    engine.state.available_capital = 90_000.0
    engine.update_equity_tracking("2026-08-24")

    assert engine.state.month_start_equity == original_month_start
    assert engine.state.week_start_equity == 90_000.0  # week DID reset
