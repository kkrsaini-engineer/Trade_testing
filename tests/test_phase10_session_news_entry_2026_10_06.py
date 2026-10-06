"""
Phase B2/C1 of BUG_AUDIT_2026-10-05_PROFITABILITY.md (2026-10-06),
mirrored BUY/SELL where direction matters.

H1   previous full session's high/low checked against stop/targets (the
     once-a-day 9:20 check never saw anything after 9:20 yesterday).
M11  highest/lowest tracked from whole session ranges, not one snapshot.
H12  company-news check actually runs: "published_at" key, timezone-aware
     comparison, now_ist() correctly labelled IST.
M8   stale candidates_order.json is not executed again.
H6   gap-up (BUY) / gap-down (SELL) chase filter at >= 1%.
"""

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import scripts.morning_executor as mx
from core import trading_calendar as tc
from decision.decision_engine import FinalDecision
from paper_trading.paper_trading_engine import PaperTradingEngine, _previous_session_range
from paper_trading.virtual_portfolio import VirtualPortfolio
from portfolio.portfolio import PortfolioEngine, PortfolioState
from risk.exit_strategy import ExitStrategyEngine, FULL_EXIT, HOLD, PARTIAL_EXIT
from risk.risk_manager import RiskResult
from risk.transaction_costs import ZERO_COSTS
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore
from tests.test_paper_trading_exit_wiring import FakeScanner


def _decision(action):
    return FinalDecision(
        action=action, confidence=80.0, ranking=70.0, buy_score=75.0, sell_score=25.0,
        buy_probability=70.0, sell_probability=20.0, expected_return=5.0,
        expected_drawdown=2.0, expected_hold_days=5,
    )


def _safe_risk():
    return RiskResult(
        safe=True, total_risk=20.0, risk_grade="A",
        atr_risk=10.0, gap_risk=10.0, overnight_risk=10.0, news_risk=10.0,
        liquidity_risk=10.0, volatility_risk=10.0, portfolio_risk=10.0,
        sector_risk=10.0, correlation_risk=10.0, capital_risk=10.0,
    )


# ==========================================================
# H1 — previous-session stop / target touches (exit engine)
# ==========================================================

ENGINE = ExitStrategyEngine()


def _evaluate(direction, close, prev=None, stop_level=None, partial_taken=False,
              day_high=None, day_low=None, highest=None, lowest=None):
    ema_20, ema_50 = (100.0, 95.0) if direction == "BUY" else (95.0, 100.0)
    df = pd.DataFrame([{
        "close": close, "atr_14": 2.0, "ema_20": ema_20, "ema_50": ema_50, "volatility_state": "NORMAL",
    }])
    prev = prev or {}
    position = {
        "direction": direction, "entry_price": 100.0, "current_price": close, "holding_days": 3,
        "highest_price": highest if highest is not None else close,
        "lowest_price": lowest if lowest is not None else close,
        "day_high": day_high if day_high is not None else close,
        "day_low": day_low if day_low is not None else close,
        "emergency_exit": False, "partial_taken": partial_taken, "stop_level": stop_level,
        "prev_day_open": prev.get("open"), "prev_day_high": prev.get("high"), "prev_day_low": prev.get("low"),
    }
    return ENGINE.evaluate(decision=_decision(direction), risk=_safe_risk(), dataframe=df, position=position)


def test_buy_stop_touched_in_previous_session_exits_at_stop_price():
    # Stop 96. Yesterday's low 94 (after the 9:20 check); today it has
    # recovered to 99 — the old code would just HOLD.
    result = _evaluate("BUY", 99.0, prev={"open": 99.0, "high": 100.5, "low": 94.0}, stop_level=96.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 96.0
    assert result.diagnostics["prev_session_stop_hit"] is True


def test_sell_stop_touched_in_previous_session_exits_at_stop_price():
    result = _evaluate("SELL", 101.0, prev={"open": 101.0, "high": 106.0, "low": 99.5}, stop_level=104.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 104.0


def test_previous_session_gap_through_stop_fills_at_that_open():
    result = _evaluate("BUY", 93.0, prev={"open": 94.5, "high": 95.0, "low": 92.0}, stop_level=96.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 94.5


@pytest.mark.parametrize("direction,prev,expected_fill", [
    ("BUY", {"open": 105.0, "high": 108.0, "low": 104.5}, 107.0),   # final target 107
    ("SELL", {"open": 95.0, "high": 95.5, "low": 92.0}, 93.0),      # final target 93
])
def test_final_target_touched_in_previous_session(direction, prev, expected_fill):
    close = 104.0 if direction == "BUY" else 96.0
    result = _evaluate(direction, close, prev=prev, stop_level=96.0 if direction == "BUY" else 104.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == expected_fill


@pytest.mark.parametrize("direction,prev,close,expected_fill", [
    ("BUY", {"open": 102.0, "high": 104.5, "low": 101.5}, 102.5, 104.0),  # target1 104
    ("SELL", {"open": 98.0, "high": 98.5, "low": 95.5}, 97.5, 96.0),      # target1 96
])
def test_partial_target_touched_in_previous_session(direction, prev, close, expected_fill):
    result = _evaluate(direction, close, prev=prev, stop_level=96.0 if direction == "BUY" else 104.0)
    assert result.action == PARTIAL_EXIT
    assert result.suggested_exit_price == expected_fill
    assert result.diagnostics["partial_exit"] is True


def test_previous_session_partial_ignored_once_taken():
    result = _evaluate("BUY", 102.5, prev={"open": 102.0, "high": 104.5, "low": 101.5},
                       stop_level=96.0, partial_taken=True)
    assert result.action == HOLD


def test_previous_session_check_uses_the_stop_that_was_live_then():
    # highest is now 110 (yesterday's high), so TODAY's trailing stop is
    # 110 - 3*2 = 104. Yesterday's low of 100 must be judged against
    # yesterday's live stop (96), not today's 104 — otherwise the same
    # bar's high would be used to stop it out on its own low.
    result = _evaluate("BUY", 106.0, prev={"open": 103.0, "high": 106.5, "low": 100.0},
                       stop_level=96.0, partial_taken=True, highest=110.0, day_low=105.0, day_high=106.5)
    assert result.diagnostics["prev_session_stop_hit"] is False
    assert result.action == HOLD


def test_no_previous_session_data_means_old_behaviour():
    result = _evaluate("BUY", 99.0, prev=None, stop_level=96.0)
    assert result.action == HOLD
    assert result.diagnostics["prev_session_stop_hit"] is False


# ==========================================================
# H1 — which session counts as "previous" (engine helper)
# ==========================================================

def _bars(dates):
    return pd.DataFrame({
        "timestamp": pd.to_datetime(dates),
        "open": [100.0] * len(dates), "high": [105.0] * len(dates), "low": [95.0] * len(dates),
    })


def test_previous_session_returned_when_held_through_it():
    rng = _previous_session_range(_bars(["2026-10-05", "2026-10-06"]), "2026-10-06", "2026-10-01")
    assert rng == {"open": 100.0, "high": 105.0, "low": 95.0}


def test_previous_session_counted_on_entry_day_itself():
    # Entered at yesterday's open -> held through yesterday's session.
    assert _previous_session_range(_bars(["2026-10-05", "2026-10-06"]), "2026-10-06", "2026-10-05") is not None


def test_no_previous_session_for_a_position_opened_today():
    assert _previous_session_range(_bars(["2026-10-05", "2026-10-06"]), "2026-10-06", "2026-10-06") is None


def test_no_previous_session_when_todays_bar_is_missing():
    # Last row is yesterday: it is already checked as "today's" day_high/low.
    assert _previous_session_range(_bars(["2026-10-02", "2026-10-05"]), "2026-10-06", "2026-10-01") is None


def test_no_previous_session_without_timestamps():
    df = pd.DataFrame({"open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0]})
    assert _previous_session_range(df, "2026-10-06", "2026-10-01") is None


# ==========================================================
# H1 — end to end through PaperTradingEngine
# ==========================================================

class _SessionScanner(FakeScanner):
    """FakeScanner with a real two-bar (yesterday, today) dataframe."""

    def __init__(self, direction, prev_high, prev_low, close):
        super().__init__(close=close, atr=2.0)
        self.direction = direction
        self.prev_high = prev_high
        self.prev_low = prev_low

    def evaluate_position(self, symbol, position, portfolio, broker_status, market_state, bundle=None):
        result = super().evaluate_position(symbol, position, portfolio, broker_status, market_state, bundle)
        today = date.today()
        ema_20, ema_50 = (100.0, 95.0) if self.direction == "BUY" else (95.0, 100.0)
        result.diagnostics["_dataframe"] = pd.DataFrame({
            "timestamp": pd.to_datetime([(today - timedelta(days=1)).isoformat(), today.isoformat()]),
            "open": [self.close, self.close], "high": [self.prev_high, self.close],
            "low": [self.prev_low, self.close], "close": [self.close, self.close],
            "atr_14": [2.0, 2.0], "ema_20": [ema_20, ema_20], "ema_50": [ema_50, ema_50],
            "volatility_state": ["NORMAL", "NORMAL"],
        })
        return result


@pytest.fixture
def engine_setup(tmp_path):
    portfolio = VirtualPortfolio(initial_capital=500_000.0, state_path=str(tmp_path / "portfolio.json"))
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    trade_store = TradeStore(path=str(tmp_path / "trades"))
    return portfolio, diary, trade_store


def _open(portfolio, diary, direction):
    portfolio.engine.add_position(symbol="TESTCO.NS", quantity=10, entry_price=100.0, direction=direction)
    diary.open_trade(
        trade_id=f"paper_TESTCO_NS_{int(time.time() * 1000)}", symbol="TESTCO.NS", direction=direction,
        entry_price=100.0, entry_date="2026-01-01", buy_probability=70.0, buy_confidence=80.0,
        entry_reasons=["test"],
    )


@pytest.mark.parametrize("direction,prev_high,prev_low,close,fill", [
    ("BUY", 100.5, 94.0, 99.0, 96.0),    # stop 96 touched yesterday
    ("SELL", 106.0, 99.5, 101.0, 104.0),  # stop 104 touched yesterday
])
def test_engine_closes_on_previous_session_stop_at_stop_price(engine_setup, direction, prev_high, prev_low, close, fill):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary, direction)
    scanner = _SessionScanner(direction, prev_high, prev_low, close)
    # ZERO_COSTS: this test checks the fill PRICE; transaction costs
    # (2026-10-06, audit H13) are booked into the exit price and are
    # tested separately in test_phase12_costs_accounting_2026_10_06.py.
    engine = PaperTradingEngine(
        scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store, cost_model=ZERO_COSTS,
    )

    engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" not in portfolio.engine.state.open_positions
    closed = portfolio.engine.state.closed_positions[-1]
    assert closed.current_price == fill


# ==========================================================
# M11 — range observation
# ==========================================================

@pytest.mark.parametrize("direction,mfe,mae", [("BUY", 8.0, 5.0), ("SELL", 5.0, 8.0)])
def test_observe_range_updates_extremes_and_excursions(direction, mfe, mae):
    engine = PortfolioEngine(state=PortfolioState(total_capital=100_000.0, available_capital=100_000.0))
    engine.add_position("X", quantity=10, entry_price=100.0, direction=direction)
    engine.update_position("X", current_price=101.0)

    engine.observe_range("X", high=108.0, low=95.0)

    pos = engine.state.open_positions["X"]
    assert pos.highest_price == 108.0
    assert pos.lowest_price == 95.0
    assert pos.max_profit_percent == pytest.approx(mfe)
    assert pos.max_drawdown_percent == pytest.approx(mae)


def test_observe_range_ignores_missing_values():
    engine = PortfolioEngine(state=PortfolioState(total_capital=100_000.0, available_capital=100_000.0))
    engine.add_position("X", quantity=10, entry_price=100.0, direction="BUY")
    engine.update_position("X", current_price=101.0)
    engine.observe_range("X", high=None, low=None)
    pos = engine.state.open_positions["X"]
    assert pos.highest_price == 101.0


# ==========================================================
# H12 — timezone + news key
# ==========================================================

def test_now_ist_is_labelled_ist_with_unchanged_wall_clock():
    ist = tc.now_ist()
    utc = datetime.now(timezone.utc)
    assert ist.utcoffset() == timedelta(hours=5, minutes=30)
    assert abs((ist - utc).total_seconds()) < 5  # same instant
    assert abs((ist.replace(tzinfo=None) - (utc.replace(tzinfo=None) + timedelta(hours=5, minutes=30))).total_seconds()) < 5


def test_parse_iso_utc_handles_z_offsets_and_naive_values():
    assert mx._parse_iso_utc("2026-10-05T15:00:00Z", timezone.utc) == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    # naive scan timestamp interpreted as IST -> 20:30 IST == 15:00 UTC
    assert mx._parse_iso_utc("2026-10-05T20:30:00", tc.IST_TZ) == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    assert mx._parse_iso_utc("2026-10-05T20:30:00+05:30", timezone.utc) == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    assert mx._parse_iso_utc(None, timezone.utc) is None
    assert mx._parse_iso_utc("not a date", timezone.utc) is None


def _fake_headlines(published_at_values):
    return [
        {"title": "Company wins large order", "published_at": value, "symbol": "X"}
        for value in published_at_values
    ]


def test_symbol_news_now_sees_headlines_published_after_the_scan(monkeypatch):
    # Scan at 20:30 IST (15:00 UTC); one headline after it, one before.
    monkeypatch.setattr(
        mx.NewsDataProvider, "fetch",
        lambda self, symbol, limit=20: _fake_headlines(["2026-10-05T16:00:00+00:00", "2026-10-05T10:00:00+00:00"]),
    )
    ok, reason = mx._check_symbol_news("X.NS", "BUY", "2026-10-05T20:30:00+05:30")
    assert ok is True
    assert "1 new company headline" in reason.lower()


def test_symbol_news_still_reports_no_new_news_when_all_are_old(monkeypatch):
    monkeypatch.setattr(
        mx.NewsDataProvider, "fetch",
        lambda self, symbol, limit=20: _fake_headlines(["2026-10-05T10:00:00+00:00"]),
    )
    ok, reason = mx._check_symbol_news("X.NS", "BUY", "2026-10-05T20:30:00+05:30")
    assert ok is True
    assert "No NEW company-specific overnight news" in reason


def test_news_provider_writes_timezone_aware_published_at():
    source = Path("data/news_data.py").read_text()
    assert "datetime.fromtimestamp(ts, tz=timezone.utc)" in source


# ==========================================================
# M8 — stale candidates
# ==========================================================

@pytest.mark.parametrize("scan_date,today,stale", [
    ("2026-10-05", date(2026, 10, 6), False),   # Mon scan -> Tue run
    ("2026-10-02", date(2026, 10, 5), False),   # Fri scan -> Mon run
    ("2026-10-04", date(2026, 10, 5), False),   # Sun scan -> Mon run
    ("2026-10-01", date(2026, 10, 5), False),   # Fri 2 Oct is an NSE holiday, so Thu IS the last session
    ("2026-10-01", date(2026, 10, 6), True),    # Thu scan -> Tue run (Mon scan missing)
    ("2026-10-02", date(2026, 10, 6), True),    # two sessions old
    ("2026-10-06", date(2026, 10, 6), True),    # same day — not last night's scan
    (None, date(2026, 10, 6), True),
    ("garbage", date(2026, 10, 6), True),
])
def test_is_stale_scan(scan_date, today, stale):
    assert mx.is_stale_scan(scan_date, today) is stale


# ==========================================================
# H6 — gap chase filter
# ==========================================================

@pytest.mark.parametrize("direction,gap,chase", [
    ("BUY", 1.0, True), ("BUY", 2.4, True), ("BUY", 0.99, False), ("BUY", -1.5, False),
    ("SELL", -1.0, True), ("SELL", -2.4, True), ("SELL", -0.99, False), ("SELL", 1.5, False),
])
def test_gap_chase_filter(direction, gap, chase):
    assert mx.is_gap_chase(direction, gap) is chase
