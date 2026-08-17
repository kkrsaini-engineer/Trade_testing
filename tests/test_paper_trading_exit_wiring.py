"""
Integration tests for paper_trading/paper_trading_engine.py's monitoring
loop wired to risk/exit_strategy.py's ExitStrategyEngine (Phase 19 —
replaces risk/exit_engine.py's ExitEngine entirely).

Exercises the real PaperTradingEngine.run_cycle() against a FAKE scanner
(so no real market data / network is needed) but REAL VirtualPortfolio /
TradeDiary / TradeStore / ExitStrategyEngine objects, backed by tmp_path,
to prove the actual wiring — not just the isolated engine — behaves
correctly end to end:
  - A FULL_EXIT closes the position and removes it from the portfolio.
  - A PARTIAL_EXIT reduces quantity but leaves the position open, and is
    recorded in summary["partial_exits_today"].
  - HOLD leaves the position untouched.
  - Mirrored for BUY and SELL.
"""

import time

import pandas as pd
import pytest

from decision.decision_engine import FinalDecision
from execution.scanner import ScanResult
from paper_trading.paper_trading_engine import PaperTradingEngine
from paper_trading.virtual_portfolio import VirtualPortfolio
from risk.risk_manager import RiskResult
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore


def _final_decision(action="BUY"):
    return FinalDecision(
        action=action, confidence=80.0, ranking=70.0,
        buy_score=75.0, sell_score=25.0, buy_probability=70.0, sell_probability=20.0,
        expected_return=5.0, expected_drawdown=2.0, expected_hold_days=5,
    )


def _risk_result(safe=True):
    return RiskResult(
        safe=safe, total_risk=20.0, risk_grade="A",
        atr_risk=10.0, gap_risk=10.0, overnight_risk=10.0, news_risk=10.0,
        liquidity_risk=10.0, volatility_risk=10.0, portfolio_risk=10.0,
        sector_risk=10.0, correlation_risk=10.0, capital_risk=10.0,
    )


def _dataframe(close, atr=2.0, ema_20=100.0, ema_50=95.0):
    return pd.DataFrame([{
        "close": close, "atr_14": atr, "ema_20": ema_20, "ema_50": ema_50,
        "volatility_state": "NORMAL",
    }])


class FakeScanner:
    """Stands in for execution.scanner.MarketScanner — returns a
    caller-controlled ScanResult so the exit-decision wiring can be
    tested without real market data."""

    def __init__(self, close, atr=2.0, day_high=None, day_low=None, risk_safe=True):
        self.close = close
        self.atr = atr
        self.day_high = day_high if day_high is not None else close
        self.day_low = day_low if day_low is not None else close
        self.risk_safe = risk_safe

    def evaluate_position(self, symbol, position, portfolio, broker_status, market_state, bundle=None):
        direction = position.get("direction", "BUY")
        diagnostics = {
            "_dataframe": _dataframe(self.close, self.atr),
            "_final_decision": _final_decision(direction),
            "_risk_result": _risk_result(self.risk_safe),
            "latest_close": self.close,
            "latest_high": self.day_high,
            "latest_low": self.day_low,
            "stop_loss": 90.0, "target1": 110.0, "target2": 120.0,
            "buy_decision_confidence": 70.0, "sell_decision_confidence": 20.0,
            "sector": "IT", "market_regime": "NORMAL",
            "risk_safe": self.risk_safe, "risk_grade": "A", "total_risk": 20.0,
        }
        return ScanResult(
            symbol=symbol, action=direction, score=70.0, probability=70.0,
            confidence=80.0, ranking=70.0, position_size=0, portfolio_allowed=False,
            diagnostics=diagnostics,
        )


@pytest.fixture
def engine_setup(tmp_path):
    portfolio = VirtualPortfolio(
        initial_capital=500_000.0, state_path=str(tmp_path / "portfolio.json"),
    )
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    trade_store = TradeStore(path=str(tmp_path / "trades"))
    return portfolio, diary, trade_store


def _open_position(portfolio, diary, symbol, direction, entry_price, quantity=10):
    portfolio.engine.add_position(
        symbol=symbol, quantity=quantity, entry_price=entry_price, direction=direction,
    )
    trade_id = f"paper_{symbol.replace('.', '_')}_{int(time.time() * 1000)}"
    diary.open_trade(
        trade_id=trade_id, symbol=symbol, direction=direction, entry_price=entry_price,
        entry_date="2026-01-01", buy_probability=70.0, buy_confidence=80.0,
        entry_reasons=["test entry"],
    )
    return trade_id


def test_full_exit_closes_position_buy(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0)

    # Price crashes well past the initial stop (100 - 2*2 = 96).
    scanner = FakeScanner(close=80.0, atr=2.0, day_high=81.0, day_low=79.0)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" not in portfolio.engine.state.open_positions
    assert len(summary["closed_today"]) == 1
    assert summary["closed_today"][0]["symbol"] == "TESTCO.NS"


def test_full_exit_closes_position_sell(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "SELL", entry_price=100.0)

    # Price rallies well past the initial stop (100 + 2*2 = 104).
    scanner = FakeScanner(close=120.0, atr=2.0, day_high=121.0, day_low=119.0)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" not in portfolio.engine.state.open_positions
    assert len(summary["closed_today"]) == 1


def test_partial_exit_reduces_quantity_but_stays_open(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0, quantity=10)

    # partial_target = 100 + 2*2 = 104; final_target = 100 + 3.5*2 = 107 — land between.
    scanner = FakeScanner(close=105.0, atr=2.0, day_high=105.5, day_low=104.5)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" in portfolio.engine.state.open_positions
    remaining = portfolio.engine.state.open_positions["TESTCO.NS"]
    assert remaining.quantity == 5  # 50% of 10
    assert len(summary["partial_exits_today"]) == 1
    assert summary["partial_exits_today"][0]["symbol"] == "TESTCO.NS"
    assert len(summary["closed_today"]) == 0


def test_hold_leaves_position_untouched(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0, quantity=10)

    scanner = FakeScanner(close=101.0, atr=2.0, day_high=101.5, day_low=100.5)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" in portfolio.engine.state.open_positions
    assert portfolio.engine.state.open_positions["TESTCO.NS"].quantity == 10
    assert len(summary["closed_today"]) == 0
    assert len(summary["partial_exits_today"]) == 0
    assert len(summary["monitored"]) == 1


def test_emergency_exit_on_risk_unsafe_forces_full_exit(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0)

    # Otherwise a plain HOLD (small move, no stop/target hit) — but
    # risk_safe=False must force an immediate FULL_EXIT.
    scanner = FakeScanner(close=101.0, atr=2.0, risk_safe=False)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" not in portfolio.engine.state.open_positions
    assert len(summary["closed_today"]) == 1


# ==========================================================
# THESIS-DECAY BASELINE CAPTURE — Point 16, PHASE28_NOTES.md. Real live
# entries (scripts/morning_executor.py) hardcode the diary's
# buy_confidence to 0.0, so it can't serve as the thesis-decay
# baseline. Instead, the engine must capture the FIRST real
# held-direction confidence the monitoring loop computes as the
# baseline, exactly once, and keep passing it (plus each day's fresh
# confidence) into ExitStrategyEngine. Mirrored for BUY and SELL.
# ==========================================================

class DecayScanner:
    """Like FakeScanner, but the caller controls buy/sell
    held-direction confidence per cycle — starts at `start_confidence`,
    switches to `end_confidence` from `drop_at_cycle` onward (1-indexed)
    — to exercise the thesis-decay wiring end-to-end. Close price stays
    fixed in an otherwise-uneventful HOLD range for every other exit
    check the whole time."""

    def __init__(
        self, close, atr=2.0, start_confidence=70.0, end_confidence=70.0, drop_at_cycle=None,
        ema_20=100.0, ema_50=95.0,
    ):
        self.close = close
        self.atr = atr
        self.start_confidence = start_confidence
        self.end_confidence = end_confidence
        self.drop_at_cycle = drop_at_cycle
        self.ema_20 = ema_20
        self.ema_50 = ema_50
        self._cycle = 0

    def evaluate_position(self, symbol, position, portfolio, broker_status, market_state, bundle=None):
        self._cycle += 1
        direction = position.get("direction", "BUY")
        confidence = (
            self.end_confidence
            if self.drop_at_cycle is not None and self._cycle >= self.drop_at_cycle
            else self.start_confidence
        )
        diagnostics = {
            "_dataframe": _dataframe(self.close, self.atr, self.ema_20, self.ema_50),
            "_final_decision": _final_decision(direction),
            "_risk_result": _risk_result(True),
            "latest_close": self.close,
            "latest_high": self.close,
            "latest_low": self.close,
            "stop_loss": 90.0, "target1": 110.0, "target2": 120.0,
            "buy_decision_confidence": confidence if direction == "BUY" else 20.0,
            "sell_decision_confidence": confidence if direction == "SELL" else 70.0,
            "sector": "IT", "market_regime": "NORMAL",
            "risk_safe": True, "risk_grade": "A", "total_risk": 20.0,
        }
        return ScanResult(
            symbol=symbol, action=direction, score=70.0, probability=70.0,
            confidence=80.0, ranking=70.0, position_size=0, portfolio_allowed=False,
            diagnostics=diagnostics,
        )


def test_thesis_baseline_captured_on_first_cycle_buy(engine_setup):
    portfolio, diary, trade_store = engine_setup
    trade_id = _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0)

    scanner = DecayScanner(close=101.0, atr=2.0, start_confidence=70.0)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)

    record = diary.get_diary(trade_id)
    assert record["entry_thesis_confidence"] == 70.0


def test_thesis_baseline_captured_on_first_cycle_sell(engine_setup):
    portfolio, diary, trade_store = engine_setup
    trade_id = _open_position(portfolio, diary, "TESTCO.NS", "SELL", entry_price=100.0)

    # ema_20 < ema_50 so trend_reversal (which checks ema_20 > ema_50 for
    # SELL) doesn't fire and this stays a plain HOLD, same precedent as
    # test_exit_strategy.py's SELL HOLD cases.
    scanner = DecayScanner(close=99.0, atr=2.0, start_confidence=65.0, ema_20=90.0, ema_50=95.0)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)

    record = diary.get_diary(trade_id)
    assert record["entry_thesis_confidence"] == 65.0


def test_thesis_baseline_not_overwritten_on_later_cycles(engine_setup):
    portfolio, diary, trade_store = engine_setup
    trade_id = _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0)

    # Confidence drops from cycle 2 onward -- the baseline captured on
    # cycle 1 must stay at the ORIGINAL value, not the latest one.
    scanner = DecayScanner(close=101.0, atr=2.0, start_confidence=70.0, end_confidence=30.0, drop_at_cycle=2)
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)
    engine.run_cycle(["TESTCO.NS"], force=True)
    engine.run_cycle(["TESTCO.NS"], force=True)
    engine.run_cycle(["TESTCO.NS"], force=True)

    record = diary.get_diary(trade_id)
    assert record["entry_thesis_confidence"] == 70.0


def test_thesis_decay_forces_partial_exit_at_max_hold_days_buy(engine_setup):
    portfolio, diary, trade_store = engine_setup
    trade_id = _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0, quantity=10)

    # Baseline captured at 70 on cycle 1; drops to 45 (25-point decay,
    # over the 20-point threshold) from cycle 2 onward. Close price
    # (101) stays in plain-HOLD range for every other exit check the
    # whole time, so only the thesis-decay time exit can fire this.
    scanner = DecayScanner(
        close=101.0, atr=2.0, start_confidence=70.0, end_confidence=45.0, drop_at_cycle=2,
    )
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    # holding_days is len(daily_log) BEFORE today's log entry is
    # written, so it only reaches MAX_HOLD_DAYS=30 on the 31st cycle.
    summary = None
    for _ in range(31):
        summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert len(summary["partial_exits_today"]) == 1
    assert summary["partial_exits_today"][0]["symbol"] == "TESTCO.NS"
    assert summary["partial_exits_today"][0]["trigger"] == "Time-Based Exit"
    record = diary.get_diary(trade_id)
    assert any("Thesis decayed" in n for n in record["daily_log"][-1]["notes"])


def test_thesis_decay_below_threshold_does_not_force_exit_at_max_hold_days(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open_position(portfolio, diary, "TESTCO.NS", "BUY", entry_price=100.0, quantity=10)

    # Baseline captured at 70; drops only to 55 (15-point decay, UNDER
    # the 20-point threshold) from cycle 2 onward -- must NOT force an
    # exit even once holding_days reaches MAX_HOLD_DAYS.
    scanner = DecayScanner(
        close=101.0, atr=2.0, start_confidence=70.0, end_confidence=55.0, drop_at_cycle=2,
    )
    engine = PaperTradingEngine(scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store)

    summary = None
    for _ in range(31):
        summary = engine.run_cycle(["TESTCO.NS"], force=True)

    assert "TESTCO.NS" in portfolio.engine.state.open_positions
    assert len(summary["partial_exits_today"]) == 0
    assert len(summary["closed_today"]) == 0
