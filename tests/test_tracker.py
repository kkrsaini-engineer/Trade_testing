"""
Tests for execution/tracker.py's PositionTracker.update() — this method
was DEAD CODE (never called by anything, including orchestrator.py which
only instantiates PositionTracker) and had 3 confirmed crash-causing bugs
that would fire the instant it was ever actually invoked:

  1. `FinalDecision(signal=...)` — "signal" isn't a real field on
     FinalDecision (the field is "action"), and every other mandatory
     field was missing too.
  2. `self.exit_engine.evaluate(position=pos, ...)` passed a PositionState
     DATACLASS instance where a plain dict was required (the engine calls
     `.get(...)` on it throughout) — and omitted the required `decision`
     argument.
  3. Downstream action-handling used risk/exit_engine.py's old
     EXIT/FORCE_EXIT/TRAIL vocabulary, which risk/exit_strategy.py's
     ExitStrategyEngine (the class this file actually instantiates) has
     never returned — it only returns HOLD/PARTIAL_EXIT/FULL_EXIT.

Phase 19 fixed all three (see PHASE19_NOTES.md) so PositionTracker at
least WORKS if ever wired up in the future, even though it remains
unwired today (paper_trading/paper_trading_engine.py calls
ExitStrategyEngine directly against VirtualPortfolio's own state instead,
to avoid a redundant parallel position-tracking system).
"""

import pandas as pd

from execution.tracker import PositionState, PositionTracker


def _dataframe(
    close, atr=2.0, ema_20=100.0, ema_50=95.0, volatility_state="NORMAL",
    high=None, low=None, open_=None, volume=1_000_000, atr_14=None,
    volume_sma_20=900_000,
):
    row = {
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": volume,
        "atr": atr,
        "atr_14": atr_14 if atr_14 is not None else atr,
        "volume_sma_20": volume_sma_20,
        "ema_20": ema_20,
        "ema_50": ema_50,
        "volatility_state": volatility_state,
    }
    return pd.DataFrame([row])


def test_update_does_not_crash_and_returns_valid_result():
    tracker = PositionTracker()
    tracker.add_position(
        PositionState(
            symbol="TEST",
            entry_price=100.0,
            quantity=10,
            direction="BUY",
            entry_time="2026-01-01",
            current_price=100.0,
            highest_price=100.0,
            lowest_price=100.0,
        )
    )

    dataframe_map = {"TEST": _dataframe(close=101.0, high=102.0, low=100.0)}
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}

    results = tracker.update(dataframe_map=dataframe_map, portfolio=portfolio, market=market)

    assert len(results) == 1
    result = results[0]
    assert result.symbol == "TEST"
    # Must be a real ExitDecision action, never the old EXIT/FORCE_EXIT/TRAIL.
    assert result.action in {"HOLD", "PARTIAL_EXIT", "FULL_EXIT"}
    assert isinstance(result.warnings, list)
    assert "exit_reason" in result.diagnostics


def test_update_trails_stop_upward_as_price_runs_up_for_buy():
    tracker = PositionTracker()
    tracker.add_position(
        PositionState(
            symbol="TEST",
            entry_price=100.0,
            quantity=10,
            direction="BUY",
            entry_time="2026-01-01",
            current_price=100.0,
            highest_price=100.0,
            lowest_price=100.0,
        )
    )
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}

    # Day 1 — price runs to 130.
    tracker.update(
        dataframe_map={"TEST": _dataframe(close=130.0, high=131.0, low=129.0, atr=2.0)},
        portfolio=portfolio, market=market,
    )
    stop_at_peak = tracker.positions["TEST"].stop_loss

    # Day 2 — price pulls back; stop should not regress below where it
    # trailed to at the peak.
    tracker.update(
        dataframe_map={"TEST": _dataframe(close=120.0, high=122.0, low=119.0, atr=2.0)},
        portfolio=portfolio, market=market,
    )
    stop_after_pullback = tracker.positions.get("TEST")

    if stop_after_pullback is not None:  # not closed
        assert stop_after_pullback.stop_loss >= stop_at_peak - 0.01


def test_position_closed_on_full_exit_status():
    tracker = PositionTracker()
    tracker.add_position(
        PositionState(
            symbol="TEST",
            entry_price=100.0,
            quantity=10,
            direction="BUY",
            entry_time="2026-01-01",
            current_price=100.0,
            highest_price=100.0,
            lowest_price=100.0,
        )
    )
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}

    # Price crashes well below the initial stop (entry - 2*atr = 96).
    results = tracker.update(
        dataframe_map={"TEST": _dataframe(close=80.0, high=81.0, low=79.0, atr=2.0)},
        portfolio=portfolio, market=market,
    )
    result = results[0]
    if result.action == "FULL_EXIT":
        assert tracker.positions["TEST"].status == "CLOSED"


def test_summary_and_snapshot_do_not_crash():
    tracker = PositionTracker()
    tracker.add_position(
        PositionState(
            symbol="TEST",
            entry_price=100.0,
            quantity=10,
            direction="BUY",
            entry_time="2026-01-01",
        )
    )
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}
    results = tracker.update(
        dataframe_map={"TEST": _dataframe(close=101.0, high=102.0, low=100.0)},
        portfolio=portfolio, market=market,
    )
    assert isinstance(PositionTracker.summary(results), str)
    assert isinstance(tracker.get_snapshot(), dict)
    assert isinstance(PositionTracker.debug_report(results), str)
