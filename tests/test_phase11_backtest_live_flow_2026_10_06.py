"""
BUG_AUDIT_2026-10-05_PROFITABILITY.md M9 — analytics/backtest_engine.py
now replays the real live flow (2026-10-06):

  night scan on day D's close  ->  Morning-Executor rules + fill at D+1's
  OPEN  ->  daily ExitStrategyEngine monitoring (stops / targets / risk).

Before: it filled at the signal bar's own close (look-ahead), had no
stop/target/exit engine at all (positions closed only on an opposite
signal), used random broker slippage, and sliced symbols by row number.

These tests use a fake scanner so the mechanics can be checked exactly,
without real market data. Mirrored BUY/SELL where direction matters.
"""

import pandas as pd
import pytest

from analytics.backtest_engine import BacktestEngine
from decision.decision_engine import FinalDecision
from execution.scanner import ScanResult
from risk.risk_manager import RiskResult


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


class FakeScanner:
    """signals: {date_str: (symbol, action)} — emitted at that day's close.
    ATR is fixed at 2.0, so for an entry at 100 the stop is 96 (BUY) /
    104 (SELL), target1 104 / 96, target2 107 / 93."""

    def __init__(self, signals):
        self.signals = signals
        self._last_full_scan_results = []
        self.scanned_last_dates = []
        self.calls = []

    def _result(self, sym, bundle):
        df = bundle.market
        last = df.iloc[-1]
        day = pd.Timestamp(last["timestamp"]).date().isoformat()
        self.scanned_last_dates.append(day)
        signal = self.signals.get(day)
        action = signal[1] if signal and signal[0] == sym else "NO_TRADE"
        close = float(last["close"])
        stop, t1 = (close - 4.0, close + 4.0) if action == "BUY" else (close + 4.0, close - 4.0)
        return ScanResult(
            symbol=sym, action=action, score=70.0, probability=70.0, confidence=80.0,
            ranking=70.0, position_size=0, portfolio_allowed=action in ("BUY", "SELL"),
            diagnostics={
                "latest_close": close, "atr_14": 2.0, "stop_loss": stop, "target1": t1,
                "market_regime": "BULL",
            },
        )

    # signal_path="live" (default, = production nightly scan)
    def scan_symbol(self, symbol, portfolio, broker_status, market_state, bundle=None):
        self.calls.append("scan_symbol")
        return self._result(symbol, bundle)

    # signal_path="universe"
    def scan_symbols(self, symbols, portfolio, broker_status, market_state, bundles):
        self.calls.append("scan_symbols")
        results = [self._result(sym, bundles[sym]) for sym in symbols]
        self._last_full_scan_results = results
        return [r for r in results if r.action in ("BUY", "SELL")]

    def evaluate_position(self, symbol, position, portfolio, broker_status, market_state, bundle=None):
        df = bundle.market
        last = df.iloc[-1]
        direction = position["direction"]
        ema_20, ema_50 = (100.0, 95.0) if direction == "BUY" else (95.0, 100.0)
        frame = pd.DataFrame([{
            "close": float(last["close"]), "atr_14": 2.0, "ema_20": ema_20, "ema_50": ema_50,
            "volatility_state": "NORMAL",
        }])
        return ScanResult(
            symbol=symbol, action=direction, score=70.0, probability=70.0, confidence=80.0,
            ranking=70.0, position_size=0, portfolio_allowed=False,
            diagnostics={
                "_risk_result": _safe_risk(), "_final_decision": _decision(direction),
                "_dataframe": frame,
                "latest_close": float(last["close"]), "latest_open": float(last["open"]),
                "latest_high": float(last["high"]), "latest_low": float(last["low"]),
                "buy_decision_confidence": 70.0, "sell_decision_confidence": 30.0,
            },
        )


def _series(bars, start="2026-01-01", warmup=5):
    """bars: list of (open, high, low, close) for the days AFTER warm-up;
    warm-up days are flat at 100."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * warmup + list(bars)
    dates = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame({
        "timestamp": dates,
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [1_000_000.0] * len(rows),
    })


def _run(df, signals, **kwargs):
    engine = BacktestEngine(scanner=FakeScanner(signals))
    return engine.run(
        historical_data={"X.NS": df}, initial_capital=500_000.0, min_history=3, **kwargs
    )


def _day(df, i):
    return df["timestamp"].iloc[i].date().isoformat()


# ==========================================================
# Entry happens at the NEXT open, not the signal close
# ==========================================================

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_entry_fills_at_next_days_open_not_signal_close(direction):
    # Signal on the last warm-up day (close 100). Next day opens 100.5.
    df = _series([(100.5, 101.0, 100.0, 100.8)] + [(100.8, 101.2, 100.4, 100.9)] * 3)
    signal_day = _day(df, 4)
    result = _run(df, {signal_day: ("X.NS", direction)})

    trade = result.closed_trades[0]
    assert trade["entry_price"] == 100.5
    assert trade["entry_date"] == _day(df, 5)


# ==========================================================
# Stops and targets actually exit, at realistic prices
# ==========================================================

def test_buy_stop_exits_at_stop_level():
    # Enter at 100.0 (stop 96). Two days later the low touches 95.
    df = _series([(100.0, 100.5, 99.5, 100.0), (99.0, 99.5, 95.0, 97.0), (97.0, 97.5, 96.5, 97.0)])
    result = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    trade = result.closed_trades[0]
    assert trade["exit_price"] == 96.0
    assert trade["exit_reason"] == "Stop-loss triggered."


def test_sell_stop_exits_at_stop_level():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 105.0, 100.5, 103.0), (103.0, 103.5, 102.5, 103.0)])
    result = _run(df, {_day(df, 4): ("X.NS", "SELL")})
    trade = result.closed_trades[0]
    assert trade["exit_price"] == 104.0
    assert trade["exit_reason"] == "Stop-loss triggered."


def test_buy_stop_gapped_through_exits_at_that_open():
    df = _series([(100.0, 100.5, 99.5, 100.0), (94.0, 94.5, 93.0, 93.5)])
    result = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    assert result.closed_trades[0]["exit_price"] == 94.0


def test_buy_final_target_exits_at_target():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])
    result = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    trade = result.closed_trades[0]
    assert trade["exit_price"] == 107.0
    assert trade["exit_reason"] == "Final target achieved."


def test_partial_target_books_half_once():
    # Target1 104 touched on day 2, then price drifts sideways — no
    # second 50% sale on later days.
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 104.5, 100.8, 103.0)]
                 + [(103.0, 103.5, 102.5, 103.0)] * 3)
    result = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    trade = result.closed_trades[0]
    assert trade["exit_reason"].startswith("Open at backtest end")
    # 250 shares (5% of ₹5L at 100). 125 booked at 104 (+₹500), the other
    # 125 closed at 103 (+₹375). A second 50% sale would change this.
    assert trade["realized_pnl"] == pytest.approx(875.0)


# ==========================================================
# No more "close on opposite signal"
# ==========================================================

def test_opposite_signal_does_not_close_a_held_position():
    df = _series([(100.0, 100.5, 99.5, 100.0)] * 4)
    signals = {_day(df, 4): ("X.NS", "BUY"), _day(df, 6): ("X.NS", "SELL")}
    result = _run(df, signals)
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0]["exit_reason"].startswith("Open at backtest end")


# ==========================================================
# Morning-Executor rules apply at the open
# ==========================================================

@pytest.mark.parametrize("direction,open_price", [("BUY", 101.5), ("SELL", 98.5)])
def test_gap_chase_filter_skips_the_entry(direction, open_price):
    df = _series([(open_price, open_price + 0.5, open_price - 0.5, open_price)] * 3)
    result = _run(df, {_day(df, 4): ("X.NS", direction)})
    assert result.closed_trades == []
    assert result.metrics["entry_skips"]["gap chase filter"] == 1


# ==========================================================
# Costs, determinism, date alignment, no look-ahead
# ==========================================================

def test_cost_per_side_worsens_both_fills():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])
    plain = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    costly = _run(df, {_day(df, 4): ("X.NS", "BUY")}, cost_pct_per_side=0.5)
    assert costly.closed_trades[0]["entry_price"] == pytest.approx(100.5)
    assert costly.closed_trades[0]["realized_pnl"] < plain.closed_trades[0]["realized_pnl"]


def test_same_inputs_give_identical_results():
    df = _series([(100.0, 100.5, 99.5, 100.0), (99.0, 99.5, 95.0, 97.0)] * 3)
    a = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    b = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    assert a.closed_trades == b.closed_trades
    assert a.equity_curve == b.equity_curve


def test_scan_never_sees_bars_after_the_scan_day():
    df = _series([(100.0, 100.5, 99.5, 100.0)] * 4)
    scanner = FakeScanner({})
    BacktestEngine(scanner=scanner).run(historical_data={"X.NS": df}, initial_capital=500_000.0, min_history=3)
    # every scan's last visible bar is the day being simulated, in order
    assert scanner.scanned_last_dates == sorted(scanner.scanned_last_dates)
    assert scanner.scanned_last_dates[-1] == _day(df, len(df) - 1)


def test_symbol_missing_a_day_is_aligned_by_date_not_row():
    full = _series([(100.0, 100.5, 99.5, 100.0)] * 4)
    gappy = full.drop(index=6).reset_index(drop=True)  # Y has no bar on day 6
    scanner = FakeScanner({})
    BacktestEngine(scanner=scanner).run(
        historical_data={"X.NS": full, "Y.NS": gappy}, initial_capital=500_000.0, min_history=3,
    )
    # On day 6, only X is scanned — Y is not scanned with a stale bar
    day6 = _day(full, 6)
    assert scanner.scanned_last_dates.count(day6) == 1
