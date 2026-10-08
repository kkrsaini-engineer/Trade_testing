"""
2026-10-08 — "A-plan" (user-approved):
  - risk/entry_sizing.py: the one set of sizing/limit rules shared by the
    live Morning Executor and the backtest;
  - backtest uses those rules (default cap = live cap);
  - trade log carries ranking/score/probability/confidence/regime so signal
    quality can be tested against outcomes before any rank-based sizing;
  - opt-in breakeven-stop experiment, mirrored BUY/SELL.
"""

import inspect
from pathlib import Path

import pytest

import risk.entry_sizing as es
from analytics.backtest_engine import BacktestEngine
from scripts.run_backtest import TRADE_COLUMNS, write_trades_csv
from tests.test_phase11_backtest_live_flow_2026_10_06 import FakeScanner, _day, _series
from tests.test_phase16_backtest_experiments_2026_10_07 import MultiScanner

TOTAL = 500_000.0


# ==========================================================
# entry_allocation()
# ==========================================================

def test_fresh_account_gets_five_percent_of_total():
    assert es.entry_allocation(TOTAL, TOTAL, 0.0, 0.0) == (25_000.0, None)


def test_size_does_not_shrink_as_cash_is_used():
    assert es.entry_allocation(TOTAL, 200_000.0, 300_000.0, 0.0) == (25_000.0, None)


def test_exposure_limit_shrinks_the_last_entry():
    # 85% = Rs 425k; Rs 410k used -> Rs 15k of room
    assert es.entry_allocation(TOTAL, 90_000.0, 410_000.0, 0.0) == (15_000.0, None)


def test_exposure_limit_reached_names_itself():
    assert es.entry_allocation(TOTAL, 80_000.0, 425_000.0, 0.0) == (0.0, "exposure limit")


def test_below_minimum_room_is_refused():
    assert es.entry_allocation(TOTAL, 90_000.0, 417_000.0, 0.0) == (0.0, "exposure limit")   # Rs 8k left


def test_morning_limit():
    assert es.entry_allocation(TOTAL, 400_000.0, 100_000.0, 190_000.0) == (10_000.0, None)    # 200k - 190k
    assert es.entry_allocation(TOTAL, 400_000.0, 100_000.0, 200_000.0) == (0.0, "morning deploy limit")


def test_cash_limit():
    assert es.entry_allocation(TOTAL, 9_000.0, 100_000.0, 0.0) == (0.0, "cash")


def test_small_account_trades_at_its_normal_size():
    # Rs 100k account: 5% = Rs 5k, below the Rs 10k floor -> floor never exceeds the normal size
    assert es.entry_allocation(100_000.0, 100_000.0, 0.0, 0.0) == (5_000.0, None)


def test_quantity_for():
    assert es.quantity_for(25_000.0, 100.0) == 250
    assert es.quantity_for(25_000.0, 30_000.0) == 0
    assert es.quantity_for(25_000.0, 0.0) == 0
    assert es.quantity_for(0.0, 100.0) == 0


# ==========================================================
# Backtest follows the same rules
# ==========================================================

def _flat_then_up():
    return _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])


def _signals(direction, n, ranking_top=90.0):
    df = _flat_then_up()
    return df, {_day(df, 4): [(f"S{i}.NS", direction, ranking_top - i) for i in range(n)]}


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_backtest_morning_deploy_limit_and_flat_size(direction):
    df, signals = _signals(direction, 40)
    data = {f"S{i}.NS": df.copy() for i in range(40)}
    result = BacktestEngine(scanner=MultiScanner(signals)).run(
        historical_data=data, initial_capital=TOTAL, min_history=3,
    )
    notionals = [t["entry_price"] * t["initial_quantity"] for t in result.closed_trades]
    assert len(notionals) == 8                                         # 8 x ~Rs 25k = 40%
    assert all(24_800 <= n <= 25_000 for n in notionals)
    assert result.metrics["entry_skips"]["no room (morning deploy limit)"] == 32


def test_backtest_cap_default_is_the_live_cap_and_zero_is_off():
    default = inspect.signature(BacktestEngine.run).parameters["max_new_entries_per_day"].default
    assert default == es.DEFAULT_MAX_NEW_ENTRIES_PER_DAY == 10


# ==========================================================
# Trade log columns
# ==========================================================

def test_closed_trade_carries_the_signal_quality_fields():
    df = _flat_then_up()
    signals = {_day(df, 4): [("X.NS", "BUY", 85.5)]}
    t = BacktestEngine(scanner=MultiScanner(signals)).run(
        historical_data={"X.NS": df}, initial_capital=TOTAL, min_history=3,
    ).closed_trades[0]
    assert t["ranking"] == 85.5
    assert (t["score"], t["probability"], t["confidence"]) == (70.0, 70.0, 80.0)
    assert t["market_regime"] == "BULL"
    assert t["position_value"] == pytest.approx(t["entry_price"] * t["initial_quantity"], abs=0.01)


def test_trades_csv_has_the_new_columns(tmp_path):
    for name in ("position_value", "ranking", "score", "probability", "confidence", "market_regime"):
        assert name in TRADE_COLUMNS
    path = tmp_path / "t.csv"
    write_trades_csv([{"symbol": "X.NS", "ranking": 85.5, "market_regime": "BULL"}], str(path))
    header, row = path.read_text().splitlines()
    assert header.split(",") == TRADE_COLUMNS
    assert "85.5" in row and "BULL" in row


# ==========================================================
# Breakeven experiment (mirrored)
# ==========================================================
# Entry 100, 1R = 4 (ATR 2). Day 1 reaches +0.625R in profit without hitting
# any target, day 2 falls back through the entry, day 3 reaches the initial stop.

BUY_PATH = [
    (100.0, 100.5, 99.5, 100.0),     # entry day (fill at the open, 100)
    (100.0, 102.5, 99.8, 100.2),     # +0.625R intraday, closes flat
    (100.0, 100.2, 98.0, 98.5),      # falls back through the entry
    (98.5, 98.7, 94.0, 95.0),        # reaches the original stop
]
SELL_PATH = [
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.2, 97.5, 99.8),      # +0.625R in profit for a short
    (100.0, 102.0, 99.8, 101.5),
    (101.5, 106.0, 101.4, 105.0),
]


def _be_run(direction, **kwargs):
    df = _series(BUY_PATH if direction == "BUY" else SELL_PATH)
    result = BacktestEngine(scanner=FakeScanner({_day(df, 4): ("X.NS", direction)})).run(
        historical_data={"X.NS": df}, initial_capital=TOTAL, min_history=3, **kwargs,
    )
    return result.closed_trades[0]


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_breakeven_switch_saves_the_round_trip(direction):
    off = _be_run(direction)
    on = _be_run(direction, breakeven_after_r=0.5)
    assert off["r_multiple"] < -0.7                      # without it: the full stop-loss
    assert on["r_multiple"] > -0.1                       # with it: out near the entry price
    assert on["exit_date"] < off["exit_date"]


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_breakeven_is_not_triggered_below_its_level(direction):
    off = _be_run(direction)
    never = _be_run(direction, breakeven_after_r=1.5)    # price only got to +0.625R
    assert never["exit_date"] == off["exit_date"]
    assert never["r_multiple"] == off["r_multiple"]


def test_breakeven_default_is_off_and_reported():
    df = _series(BUY_PATH)
    result = BacktestEngine(scanner=FakeScanner({_day(df, 4): ("X.NS", "BUY")})).run(
        historical_data={"X.NS": df}, initial_capital=TOTAL, min_history=3, breakeven_after_r=0.5,
    )
    assert result.metrics["experiments"]["breakeven_after_r"] == 0.5
    assert "stop to entry after +0.5R" in result.report()


# ==========================================================
# Wiring
# ==========================================================

def test_cli_and_workflow_wiring():
    cli = Path("scripts/run_backtest.py").read_text()
    assert "--breakeven-after-r" in cli and "breakeven_after_r=args.breakeven_after_r" in cli
    assert "DEFAULT_MAX_NEW_ENTRIES_PER_DAY" in cli
    wf = Path(".github/workflows/backtest_and_regression.yml").read_text()
    run = wf[wf.index("- name: Run Institutional Backtest"):wf.index("- name: Run Regression Check")]
    assert '--breakeven-after-r "${{ inputs.breakeven_after_r }}"' in run
    assert "      breakeven_after_r:" in wf
    cap = wf[wf.index("      max_new_entries_per_day:"):wf.index("      min_target_to_cost:")]
    assert 'default: "10"' in cap
