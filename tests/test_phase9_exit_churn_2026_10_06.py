"""
Phase A of BUG_AUDIT_2026-10-05_PROFITABILITY.md — stop the exit churn —
plus the three stop safety fixes, mirrored BUY/SELL.

Real evidence these target (519 closed trades, storage/trades/diary/):
  - 467 trades (90%) force-closed by "Risk engine flagged this symbol as
    unsafe", avg hold 2.2 days, net ~+₹90; 234 opened and closed the same
    morning, median 2.4 minutes apart.
  - 31 stop-loss exits = −₹9,778, filled at the bar's LOW.
  - target1 re-fired a 50% sell every day (SOUTHBANK 38 -> 19 -> 10 -> 4).

C1/C2  risk-based forced exit: separate exit threshold (45 vs entry 35),
       never on the entry day except for system safety overrides.
C3     gap risk only when the gap is against the HELD direction.
C4     portfolio count/exposure excluded for a held position.
M1     fake "turnover missing" +20 skipped for a held position.
M2     entry-only validation checks skipped for a held position;
       a favourable circuit lock no longer counts as a safety event.
M10    sector score excludes the symbol's own contribution.
H2     stop/target fills at the level (or the open if gapped through),
       not the bar extreme.
H3     target1 booked only once (persisted partial_taken).
H4/H5  stop can only tighten (persisted stop_level) — also makes the
       break-even stop sticky.
"""

import time
from datetime import date

import pandas as pd
import pytest

from decision.decision_engine import FinalDecision
from decision.validation_engine import ValidationEngine, ValidationResult
from execution.scanner import MarketScanner
from paper_trading.paper_trading_engine import PaperTradingEngine, _should_force_exit
from paper_trading.virtual_portfolio import VirtualPortfolio
from risk.exit_strategy import ExitStrategyEngine, FULL_EXIT, HOLD, PARTIAL_EXIT
from risk.risk_manager import RiskManager, RiskResult
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore
from tests.test_paper_trading_exit_wiring import FakeScanner


# ==========================================================
# helpers
# ==========================================================

def _decision(action):
    return FinalDecision(
        action=action, confidence=80.0, ranking=70.0, buy_score=75.0, sell_score=25.0,
        buy_probability=70.0, sell_probability=20.0, expected_return=5.0,
        expected_drawdown=2.0, expected_hold_days=5,
    )


def _passed_validation(action):
    return ValidationResult(passed=True, action=action, confidence=80.0, rejection_reason=None)


def _risk_row(**overrides):
    row = {
        "atr_14": 1.0, "close": 100.0,
        "gap_up": False, "gap_down": False,
        "volatility_state": "LOW", "bb_width": 0.0,
        "market_regime": "SIDEWAYS",
        "volume_sma_20": 1_000_000, "spread": 0.0, "turnover": 1.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _portfolio(**overrides):
    p = {
        "open_positions": {}, "exposure": 0.0, "sector_exposure": 0.0,
        "correlation": 0.0, "total_capital": 500_000.0, "available_cash": 250_000.0,
    }
    p.update(overrides)
    return p


def _risk(action, dataframe, portfolio=None, market=None, monitoring=False):
    return RiskManager().evaluate(
        validation=_passed_validation(action),
        decision=_decision(action),
        dataframe=dataframe,
        portfolio=portfolio or _portfolio(),
        market=market or {},
        monitoring=monitoring,
    )


# ==========================================================
# C3 — gap risk is direction-aware for HELD positions only
# ==========================================================

@pytest.mark.parametrize("action,gap_col,expected", [
    ("BUY", "gap_up", 10.0),    # favourable for a long
    ("BUY", "gap_down", 75.0),  # adverse for a long
    ("SELL", "gap_down", 10.0),  # favourable for a short
    ("SELL", "gap_up", 75.0),   # adverse for a short
])
def test_monitoring_gap_risk_counts_only_adverse_gaps(action, gap_col, expected):
    result = _risk(action, _risk_row(**{gap_col: True}), monitoring=True)
    assert result.gap_risk == expected


@pytest.mark.parametrize("action,gap_col", [
    ("BUY", "gap_up"), ("BUY", "gap_down"), ("SELL", "gap_up"), ("SELL", "gap_down"),
])
def test_entry_gap_risk_is_unchanged_direction_blind(action, gap_col):
    result = _risk(action, _risk_row(**{gap_col: True}), monitoring=False)
    assert result.gap_risk == 75.0


# ==========================================================
# C4 — portfolio count/exposure excluded for HELD positions
# ==========================================================

@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_monitoring_excludes_portfolio_count_risk(action):
    crowded = _portfolio(open_positions={f"S{i}": {} for i in range(20)}, exposure=0.6)

    entry = _risk(action, _risk_row(), portfolio=crowded, monitoring=False)
    held = _risk(action, _risk_row(), portfolio=crowded, monitoring=True)

    assert entry.portfolio_risk == 45.0  # 35 (15+ positions) + 10 (exposure >= 0.5)
    assert held.portfolio_risk == 0.0
    assert held.diagnostics["portfolio_risk_entry_view"] == 45.0


# ==========================================================
# M1 — fake "turnover missing" penalty skipped for HELD positions
# ==========================================================

@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_monitoring_skips_turnover_penalty_when_column_absent(action):
    df = _risk_row()
    df = df.drop(columns=["turnover"])

    entry = _risk(action, df, monitoring=False)
    held = _risk(action, df, monitoring=True)

    assert entry.liquidity_risk == 20.0  # unchanged entry behavior
    assert held.liquidity_risk == 0.0


# ==========================================================
# C1/C2 — separate, higher exit threshold
# ==========================================================

def _elevated_row(action):
    # atr% = 6 -> atr_risk 95; HIGH volatility; adverse gap; SIDEWAYS
    # market; no turnover column. Weighted total ~37.9 in monitoring mode:
    # above the ENTRY line (35), below the EXIT line (45).
    adverse_gap = "gap_down" if action == "BUY" else "gap_up"
    df = _risk_row(atr_14=6.0, volatility_state="HIGH", **{adverse_gap: True})
    return df.drop(columns=["turnover"])


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_held_position_between_entry_and_exit_threshold_is_not_unsafe(action):
    held = _risk(action, _elevated_row(action), monitoring=True)

    assert RiskManager.MAX_TOTAL_RISK < held.total_risk <= RiskManager.MAX_EXIT_RISK
    assert held.safe is True
    assert held.diagnostics["risk_threshold"] == RiskManager.MAX_EXIT_RISK


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_entry_threshold_is_still_35(action):
    entry = _risk(action, _elevated_row(action), monitoring=False)
    assert entry.diagnostics["risk_threshold"] == RiskManager.MAX_TOTAL_RISK
    assert entry.safe is False


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_system_safety_override_still_forces_unsafe_in_monitoring(action):
    held = _risk(action, _risk_row(), market={"circuit_breaker": True}, monitoring=True)
    assert held.safe is False
    assert held.total_risk == 100.0
    assert held.diagnostics["circuit_override"] is True


# ==========================================================
# C1/C2 — paper-trading force-exit decision
# ==========================================================

def _risk_result(safe, **diag):
    return RiskResult(
        safe=safe, total_risk=50.0, risk_grade="D",
        atr_risk=10.0, gap_risk=10.0, overnight_risk=10.0, news_risk=10.0,
        liquidity_risk=10.0, volatility_risk=10.0, portfolio_risk=10.0,
        sector_risk=10.0, correlation_risk=10.0, capital_risk=10.0,
        diagnostics=diag,
    )


def test_safe_verdict_never_forces_exit():
    assert _should_force_exit(_risk_result(True), entered_today=False) is False
    assert _should_force_exit(_risk_result(True), entered_today=True) is False


def test_weighted_unsafe_verdict_does_not_force_exit_on_entry_day():
    assert _should_force_exit(_risk_result(False), entered_today=True) is False


def test_weighted_unsafe_verdict_forces_exit_after_entry_day():
    assert _should_force_exit(_risk_result(False), entered_today=False) is True


@pytest.mark.parametrize("flag", ["circuit_override", "emergency_stop", "daily_loss_lock"])
def test_system_safety_override_forces_exit_even_on_entry_day(flag):
    assert _should_force_exit(_risk_result(False, **{flag: True}), entered_today=True) is True


class _TrendAlignedScanner(FakeScanner):
    """FakeScanner always returns bullish EMAs, which fires the single-bar
    trend-reversal exit on any SELL position. Align EMAs with the held
    direction so these tests isolate the risk-verdict behaviour."""

    def evaluate_position(self, symbol, position, portfolio, broker_status, market_state, bundle=None):
        result = super().evaluate_position(symbol, position, portfolio, broker_status, market_state, bundle)
        if position.get("direction") == "SELL":
            df = result.diagnostics["_dataframe"].copy()
            df["ema_20"], df["ema_50"] = 95.0, 100.0
            result.diagnostics["_dataframe"] = df
        return result


@pytest.fixture
def engine_setup(tmp_path):
    portfolio = VirtualPortfolio(initial_capital=500_000.0, state_path=str(tmp_path / "portfolio.json"))
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    trade_store = TradeStore(path=str(tmp_path / "trades"))
    return portfolio, diary, trade_store


def _open(portfolio, diary, symbol, direction, entry_price, entry_date, quantity=10):
    portfolio.engine.add_position(symbol=symbol, quantity=quantity, entry_price=entry_price, direction=direction)
    trade_id = f"paper_{symbol.replace('.', '_')}_{int(time.time() * 1000)}"
    diary.open_trade(
        trade_id=trade_id, symbol=symbol, direction=direction, entry_price=entry_price,
        entry_date=entry_date, buy_probability=70.0, buy_confidence=80.0, entry_reasons=["test"],
    )


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_position_opened_today_survives_a_weighted_unsafe_verdict(engine_setup, direction):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary, "TESTCO.NS", direction, 100.0, entry_date=date.today().isoformat())

    scanner = _TrendAlignedScanner(close=101.0 if direction == "BUY" else 99.0, atr=2.0, risk_safe=False)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" in portfolio.engine.state.open_positions


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_older_position_still_closed_by_unsafe_verdict(engine_setup, direction):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary, "TESTCO.NS", direction, 100.0, entry_date="2026-01-01")

    scanner = _TrendAlignedScanner(close=101.0 if direction == "BUY" else 99.0, atr=2.0, risk_safe=False)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" not in portfolio.engine.state.open_positions


# ==========================================================
# M2 — entry-only validation checks skipped for HELD positions
# ==========================================================

def _validate(monitoring, portfolio=None):
    return ValidationEngine().validate(
        decision=_decision("BUY"),
        dataframe=pd.DataFrame([{"timestamp": "2026-08-17", "close": 100.0, "volume_sma_20": 50_000}]),
        portfolio=portfolio or {"available_cash": 0.0, "sector_exposure": 0.99, "correlation": 0.99},
        broker_status={"connected": True, "order_allowed": True},
        market_state={"market_open": True, "holiday": False, "circuit_breaker": False},
        monitoring=monitoring,
    )


def test_entry_validation_still_applies_entry_only_checks():
    result = _validate(monitoring=False)
    assert result.checks["average_volume"] is False
    assert result.checks["capital"] is False
    assert result.checks["sector_exposure"] is False
    assert result.checks["correlation"] is False


def test_monitoring_validation_skips_entry_only_checks():
    result = _validate(monitoring=True)
    for name in ("average_volume", "capital", "sector_exposure", "correlation", "portfolio_risk"):
        assert result.checks[name] is True
        assert name in result.diagnostics["skipped_for_monitoring"]


def test_monitoring_validation_keeps_loss_limit_checks():
    result = _validate(monitoring=True, portfolio={"daily_loss": 0.04})
    assert result.checks["daily_loss"] is False


@pytest.mark.parametrize("likely,direction,held,expected", [
    (False, "upper", "BUY", False),
    (True, "upper", "BUY", False),   # favourable for a long
    (True, "lower", "BUY", True),    # adverse for a long
    (True, "lower", "SELL", False),  # favourable for a short
    (True, "upper", "SELL", True),   # adverse for a short
    (True, None, "BUY", True),       # unknown direction -> conservative
])
def test_circuit_counts_only_when_adverse_to_held_position(likely, direction, held, expected):
    assert MarketScanner._circuit_adverse_to_position(likely, direction, held) is expected


# ==========================================================
# M10 — own contribution excluded from the sector average
# ==========================================================

def test_peer_sector_score_excludes_the_symbol_itself():
    context = {
        "sector_scores": {"IT": 50.0},
        "sector_peer_stats": {"IT": {"sum": 75.0 + 25.0 + 25.0, "count": 3}},
        "symbol_sector_scores": {"A.NS": ("IT", 75.0), "B.NS": ("IT", 25.0), "C.NS": ("IT", 25.0)},
    }
    assert MarketScanner._peer_sector_score(context, "A.NS", "IT") == 25.0  # peers B, C only
    assert MarketScanner._peer_sector_score(context, "B.NS", "IT") == 50.0  # peers A, C


def test_peer_sector_score_is_none_when_no_peers():
    context = {
        "sector_peer_stats": {"IT": {"sum": 75.0, "count": 1}},
        "symbol_sector_scores": {"A.NS": ("IT", 75.0)},
    }
    assert MarketScanner._peer_sector_score(context, "A.NS", "IT") is None


def test_peer_sector_score_uses_full_mean_for_a_symbol_not_in_the_batch():
    context = {
        "sector_peer_stats": {"IT": {"sum": 100.0, "count": 2}},
        "symbol_sector_scores": {"A.NS": ("IT", 75.0), "B.NS": ("IT", 25.0)},
    }
    assert MarketScanner._peer_sector_score(context, "Z.NS", "IT") == 50.0


# ==========================================================
# H2/H3/H4/H5 — exit engine
# ==========================================================

ENGINE = ExitStrategyEngine()


def _exit_risk():
    return _risk_result(True)


def _df(close, atr=2.0, direction="BUY"):
    # EMAs aligned WITH the held direction, so the separate single-bar
    # trend-reversal exit doesn't fire and mask what is being tested.
    ema_20, ema_50 = (100.0, 95.0) if direction == "BUY" else (95.0, 100.0)
    return pd.DataFrame([{
        "close": close, "atr_14": atr, "ema_20": ema_20, "ema_50": ema_50, "volatility_state": "NORMAL",
    }])


def _pos(direction, current, day_high=None, day_low=None, day_open=None,
         partial_taken=False, stop_level=None, highest=None, lowest=None):
    return {
        "direction": direction, "entry_price": 100.0, "current_price": current,
        "holding_days": 5,
        "highest_price": highest if highest is not None else current,
        "lowest_price": lowest if lowest is not None else current,
        "day_high": day_high, "day_low": day_low, "day_open": day_open,
        "emergency_exit": False,
        "partial_taken": partial_taken, "stop_level": stop_level,
    }


def _evaluate(direction, close, atr=2.0, **pos):
    return ENGINE.evaluate(
        decision=_decision(direction), risk=_exit_risk(),
        dataframe=_df(close, atr, direction), position=_pos(direction, close, **pos),
    )


def test_buy_stop_gapped_through_fills_at_open():
    # stop = 96; opened at 93 (below it), low 92.
    result = _evaluate("BUY", 93.5, day_open=93.0, day_high=94.0, day_low=92.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 93.0


def test_sell_stop_gapped_through_fills_at_open():
    # stop = 104; opened at 107 (above it), high 108.
    result = _evaluate("SELL", 106.5, day_open=107.0, day_high=108.0, day_low=106.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 107.0


def test_buy_stop_touched_intraday_fills_at_stop_not_day_low():
    result = _evaluate("BUY", 98.0, day_open=99.0, day_high=99.5, day_low=94.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 96.0


def test_sell_stop_touched_intraday_fills_at_stop_not_day_high():
    result = _evaluate("SELL", 102.0, day_open=101.0, day_high=106.0, day_low=100.5)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 104.0


def test_buy_final_target_gapped_above_fills_at_open():
    # final target = 107; opened at 108.
    result = _evaluate("BUY", 108.5, day_open=108.0, day_high=109.0, day_low=107.5)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 108.0


def test_sell_final_target_gapped_below_fills_at_open():
    # final target = 93; opened at 92.
    result = _evaluate("SELL", 91.5, day_open=92.0, day_high=92.5, day_low=91.0)
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 92.0


@pytest.mark.parametrize("direction,close,day_high,day_low", [
    ("BUY", 105.0, 105.5, 104.5),   # past target1 (104), short of target2 (107)
    ("SELL", 95.0, 95.5, 94.5),     # past target1 (96), short of target2 (93)
])
def test_target1_does_not_refire_once_taken(direction, close, day_high, day_low):
    first = _evaluate(direction, close, day_high=day_high, day_low=day_low, partial_taken=False)
    again = _evaluate(direction, close, day_high=day_high, day_low=day_low, partial_taken=True)

    assert first.action == PARTIAL_EXIT
    assert again.action == HOLD
    assert again.diagnostics["partial_already_taken"] is True


def test_buy_stop_never_widens_when_atr_rises():
    # Yesterday's stop 96; today ATR 3 would put a fresh stop at 94.
    result = _evaluate("BUY", 98.0, atr=3.0, day_high=99.0, day_low=97.0, stop_level=96.0)
    assert result.diagnostics["computed_stop_before_floor"] == 94.0
    assert result.diagnostics["active_stop"] == 96.0
    assert result.action == HOLD


def test_sell_stop_never_widens_when_atr_rises():
    result = _evaluate("SELL", 102.0, atr=3.0, day_high=103.0, day_low=101.0, stop_level=104.0)
    assert result.diagnostics["computed_stop_before_floor"] == 106.0
    assert result.diagnostics["active_stop"] == 104.0
    assert result.action == HOLD


def test_buy_break_even_stays_after_price_dips_back():
    # Break-even (100) was reached earlier and persisted; price is now
    # 101 — below the 1.5 ATR trigger, so the old code dropped back to 96.
    result = _evaluate("BUY", 101.0, day_high=101.5, day_low=99.5, stop_level=100.0, highest=104.0)
    assert result.diagnostics["active_stop"] == 100.0
    assert result.action == FULL_EXIT  # day low 99.5 touched the sticky 100 stop
    assert result.suggested_exit_price == 100.0


def test_sell_break_even_stays_after_price_bounces_back():
    result = _evaluate("SELL", 99.0, day_high=100.5, day_low=98.5, stop_level=100.0, lowest=96.0)
    assert result.diagnostics["active_stop"] == 100.0
    assert result.action == FULL_EXIT
    assert result.suggested_exit_price == 100.0


# ==========================================================
# H3/H4/H5 — engine persists partial_taken / stop_level, and they
# survive the daily save/reload
# ==========================================================

def test_partial_exit_marks_partial_taken_and_records_stop(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary, "TESTCO.NS", "BUY", 100.0, entry_date="2026-01-01", quantity=10)

    scanner = FakeScanner(close=105.0, atr=2.0, day_high=105.5, day_low=104.5)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)

    pos = portfolio.engine.state.open_positions["TESTCO.NS"]
    assert pos.quantity == 5
    assert pos.partial_taken is True
    assert pos.stop_level is not None

    # Second day at the same price: target1 must NOT sell again.
    engine.run_cycle(["TESTCO.NS"], force=True)
    assert portfolio.engine.state.open_positions["TESTCO.NS"].quantity == 5


def test_partial_taken_and_stop_level_survive_save_and_reload(tmp_path):
    path = str(tmp_path / "state.json")
    vp = VirtualPortfolio(initial_capital=100_000.0, state_path=path)
    vp.engine.add_position("INFY", quantity=10, entry_price=100.0, direction="BUY")
    pos = vp.engine.state.open_positions["INFY"]
    pos.partial_taken = True
    pos.stop_level = 101.25
    vp.save()

    reloaded = VirtualPortfolio(initial_capital=100_000.0, state_path=path).engine.state.open_positions["INFY"]
    assert reloaded.partial_taken is True
    assert reloaded.stop_level == 101.25


def test_old_state_file_without_new_fields_loads_with_defaults(tmp_path):
    import json

    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "total_capital": 100_000.0, "available_capital": 99_000.0, "used_capital": 1_000.0,
        "open_positions": {"INFY": {
            "symbol": "INFY", "quantity": 10, "entry_price": 100.0, "current_price": 100.0,
            "direction": "BUY", "unrealized_pnl": 0.0, "unrealized_pnl_percent": 0.0,
            "realized_pnl": 0.0, "highest_price": 100.0, "lowest_price": 100.0,
            "max_profit_percent": 0.0, "max_drawdown_percent": 0.0,
            "status": "OPEN", "updated_at": 0.0,
        }},
        "closed_positions": [],
    }))
    pos = VirtualPortfolio(initial_capital=100_000.0, state_path=str(path)).engine.state.open_positions["INFY"]
    assert pos.partial_taken is False
    assert pos.stop_level is None
