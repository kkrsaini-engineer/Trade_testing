"""
2026-10-07 — backtest experiment switches (user-approved). All OFF by
default, so a plain run still uses exactly the live rules; nothing here
touches live trading.

From the first live-path top50 run (430 trades, gross +0.01R/trade, costs
0.09R/trade):
  - buy_only: SELL was 46 trades, PF 0.80, and delivery can't hold a
    short overnight (H14);
  - max_new_entries_per_day: the 11 days with >= 5 entries lost
    Rs 15.5k of the Rs 13.9k total (the other 351 trades: +Rs 1.6k);
  - min_target_to_cost: trades with stops <= 4% lost -0.27R each net;
  - mfe_r/mae_r now include the exit day's high/low (38 of 83
    final-target trades showed MFE < 1.75R, which is impossible).
"""

from pathlib import Path

import pandas as pd
import pytest

from analytics.backtest_engine import BacktestEngine
from execution.scanner import ScanResult
from risk.transaction_costs import CostModel
from tests.test_phase11_backtest_live_flow_2026_10_06 import FakeScanner, _day, _series

SIMPLE = CostModel(buy_pct=0.1, sell_pct=0.1, sell_flat_rupees=10.0, slippage_pct=0.0)


class MultiScanner(FakeScanner):
    """signals: {date: [(symbol, action, ranking), ...]}"""

    def _result(self, sym, bundle):
        last = bundle.market.iloc[-1]
        day = pd.Timestamp(last["timestamp"]).date().isoformat()
        match = [s for s in self.signals.get(day, []) if s[0] == sym]
        action, ranking = (match[0][1], match[0][2]) if match else ("NO_TRADE", 0.0)
        close = float(last["close"])
        stop, t1 = (close - 4.0, close + 4.0) if action == "BUY" else (close + 4.0, close - 4.0)
        return ScanResult(
            symbol=sym, action=action, score=70.0, probability=70.0, confidence=80.0,
            ranking=ranking, position_size=0, portfolio_allowed=action in ("BUY", "SELL"),
            diagnostics={"latest_close": close, "atr_14": 2.0, "stop_loss": stop, "target1": t1,
                         "market_regime": "BULL"},
        )


def _flat_then_up():
    return _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])


def _run(data, scanner, **kwargs):
    return BacktestEngine(scanner=scanner).run(
        historical_data=data, initial_capital=500_000.0, min_history=3, **kwargs
    )


# ==========================================================
# Defaults = live rules
# ==========================================================

def test_switches_are_off_by_default():
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "SELL")}))
    assert result.metrics["experiments"] == {
        "buy_only": False, "max_new_entries_per_day": 10, "min_target_to_cost": 0.0,
        "breakeven_after_r": 0.0,
    }
    assert result.closed_trades                      # SELL still traded
    assert "Experiments          : none (same rules as live)" in result.report()


# ==========================================================
# buy_only (mirrored: SELL skipped, BUY untouched)
# ==========================================================

def test_buy_only_skips_sell_candidates():
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "SELL")}), buy_only=True)
    assert result.closed_trades == []
    assert result.metrics["entry_skips"]["SELL disabled (buy_only)"] == 1
    assert "BUY only" in result.report()


def test_buy_only_keeps_buy_candidates():
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "BUY")}), buy_only=True)
    assert len(result.closed_trades) == 1


# ==========================================================
# max_new_entries_per_day
# ==========================================================

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_daily_cap_keeps_the_best_ranked_entries(direction):
    df = _flat_then_up()
    data = {"A.NS": df, "B.NS": df.copy(), "C.NS": df.copy()}
    signals = {_day(df, 4): [("A.NS", direction, 60.0), ("B.NS", direction, 90.0), ("C.NS", direction, 75.0)]}
    result = _run(data, MultiScanner(signals), max_new_entries_per_day=2)
    assert sorted(t["symbol"] for t in result.closed_trades) == ["B.NS", "C.NS"]
    assert result.metrics["entry_skips"]["daily entry cap reached"] == 1


def test_cap_zero_means_no_cap():
    df = _flat_then_up()
    data = {"A.NS": df, "B.NS": df.copy(), "C.NS": df.copy()}
    signals = {_day(df, 4): [("A.NS", "BUY", 60.0), ("B.NS", "BUY", 90.0), ("C.NS", "BUY", 75.0)]}
    assert len(_run(data, MultiScanner(signals), max_new_entries_per_day=0).closed_trades) == 3


# ==========================================================
# min_target_to_cost
# ==========================================================
# Entry 100, ATR 2 -> target2 107: 250 shares gain Rs 1,750 at target2;
# SIMPLE round trip = 25.00 + 26.75 + 10 = Rs 61.75  -> ratio ~28.3.

@pytest.mark.parametrize("threshold,traded", [(30.0, False), (20.0, True)])
def test_target_to_cost_filter(threshold, traded):
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "BUY")}),
                  cost_model=SIMPLE, min_target_to_cost=threshold)
    assert bool(result.closed_trades) is traded
    if not traded:
        assert result.metrics["entry_skips"]["target too small vs costs"] == 1


def test_target_to_cost_filter_is_inert_without_a_cost_model():
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "BUY")}), min_target_to_cost=1000.0)
    assert len(result.closed_trades) == 1


# ==========================================================
# MFE/MAE include the exit day
# ==========================================================

def test_buy_target_trade_mfe_includes_the_exit_day_high():
    df = _flat_then_up()                              # exit day high 108, entry 100, 1R = 4
    t = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "BUY")})).closed_trades[0]
    assert t["exit_category"] == "final_target"
    assert t["mfe_r"] == pytest.approx(2.0)


def test_sell_stop_trade_mae_includes_the_exit_day_high():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 105.0, 100.5, 103.0)])   # exit day high 105
    t = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "SELL")})).closed_trades[0]
    assert t["exit_category"] == "stop_loss"
    assert t["mae_r"] == pytest.approx(1.25)


# ==========================================================
# CLI + workflow wiring (plain text — no PyYAML in CI)
# ==========================================================

def test_cli_and_workflow_pass_the_switches():
    cli = Path("scripts/run_backtest.py").read_text()
    for flag in ("--buy-only", "--max-new-entries-per-day", "--min-target-to-cost"):
        assert flag in cli
    assert "max_new_entries_per_day=args.max_new_entries_per_day" in cli
    wf = Path(".github/workflows/backtest_and_regression.yml").read_text()
    run = wf[wf.index("- name: Run Institutional Backtest"):wf.index("- name: Run Regression Check")]
    assert '--max-new-entries-per-day "${{ inputs.max_new_entries_per_day }}"' in run
    assert '--min-target-to-cost "${{ inputs.min_target_to_cost }}"' in run
    assert "inputs.buy_only && '--buy-only'" in run
    for name in ("buy_only:", "max_new_entries_per_day:", "min_target_to_cost:"):
        assert f"      {name}" in wf


# ==========================================================
# Incomplete (NaN) bars must never reach a trade or a P&L
# ==========================================================

@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_nan_last_bar_does_not_poison_the_open_at_end_trade(direction):
    # Position still open on the last row, whose close is NaN (unfinished
    # day). Run-1 on 2026-10-07 produced NaN P&L / costs for exactly this.
    df = _series([(100.0, 100.5, 99.5, 100.0)] + [(100.0, 100.6, 99.6, 100.2)] * 3)
    last = len(df) - 1
    df.loc[last, "close"] = float("nan")
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", direction)}), cost_model=SIMPLE)

    trades = result.closed_trades
    assert trades and all(t["realized_pnl"] == t["realized_pnl"] for t in trades)   # no NaN
    assert trades[-1]["exit_category"] == "open_at_end"
    assert result.metrics["dropped_incomplete_bars"] == 1
    for key in ("expectancy", "avg_r_multiple", "total_costs", "pnl_before_costs"):
        assert result.metrics[key] == result.metrics[key], key


def test_complete_data_reports_no_dropped_bars():
    df = _flat_then_up()
    result = _run({"X.NS": df}, FakeScanner({_day(df, 4): ("X.NS", "BUY")}))
    assert result.metrics["dropped_incomplete_bars"] == 0
    assert "incomplete bar" not in result.report()
