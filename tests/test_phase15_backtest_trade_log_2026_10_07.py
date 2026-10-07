"""
2026-10-07 — backtest made fit for stop/target tuning (user-approved,
before any C5 change).

The first realistic-cost run (2y, 11 symbols) printed "Average R:R 11.90"
on a run whose real average win / average loss was ~1.0, and "Cost per
side 0.0%" although the delivery cost model WAS applied. It also kept no
per-trade record, could only test 11 stocks, and scanned with
scan_symbols() — the two-pass path production does not run.

Now: per-trade log (costs, net R, MFE/MAE in R) written to
reports/backtest_trades_latest.csv; real payoff ratio + break-even win
rate; BUY/SELL split; honest cost line; signal_path="live" (default) uses
scan_symbol() like the nightly scan; --universe top50/top100; regression
check compares like-for-like setups only.
"""

import json
import sys

import pytest

from analytics.backtest_engine import BacktestEngine
from risk.transaction_costs import CostModel, round_trip_cost
from scripts import regression_check
from scripts.run_backtest import TRADE_COLUMNS, top_liquid_symbols, write_trades_csv
from tests.test_phase11_backtest_live_flow_2026_10_06 import FakeScanner, _day, _series

SIMPLE = CostModel(buy_pct=0.1, sell_pct=0.1, sell_flat_rupees=10.0, slippage_pct=0.0)


def _run(df, signals, scanner=None, **kwargs):
    scanner = scanner or FakeScanner(signals)
    result = BacktestEngine(scanner=scanner).run(
        historical_data={"X.NS": df}, initial_capital=500_000.0, min_history=3, **kwargs
    )
    return result, scanner


def _stop_df(direction):
    if direction == "BUY":   # entry 100, stop 96 touched
        return _series([(100.0, 100.5, 99.5, 100.0), (99.0, 99.5, 95.0, 97.0), (97.0, 97.5, 96.5, 97.0)])
    return _series([(100.0, 100.5, 99.5, 100.0), (101.0, 105.0, 100.5, 103.0), (103.0, 103.5, 102.5, 103.0)])


# ==========================================================
# Signal path
# ==========================================================

def test_default_signal_path_is_the_production_per_symbol_scan():
    df = _stop_df("BUY")
    result, scanner = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    assert set(scanner.calls) == {"scan_symbol"}
    assert result.metrics["signal_path"] == "live"
    assert result.closed_trades


def test_universe_signal_path_uses_the_two_pass_scan():
    df = _stop_df("BUY")
    result, scanner = _run(df, {_day(df, 4): ("X.NS", "BUY")}, signal_path="universe")
    assert set(scanner.calls) == {"scan_symbols"}
    assert result.metrics["signal_path"] == "universe"


def test_both_paths_give_the_same_trades_for_the_same_signals():
    df = _stop_df("SELL")
    signals = {_day(df, 4): ("X.NS", "SELL")}
    live, _ = _run(df, signals)
    universe, _ = _run(df, signals, signal_path="universe")
    assert live.closed_trades == universe.closed_trades


def test_unknown_signal_path_is_rejected():
    df = _stop_df("BUY")
    with pytest.raises(ValueError):
        _run(df, {}, signal_path="magic")


# ==========================================================
# Per-trade record
# ==========================================================

@pytest.mark.parametrize("direction,exit_price", [("BUY", 96.0), ("SELL", 104.0)])
def test_losing_stop_out_is_about_minus_one_r_net_of_costs(direction, exit_price):
    df = _stop_df(direction)
    result, _ = _run(df, {_day(df, 4): ("X.NS", direction)}, cost_model=SIMPLE)
    t = result.closed_trades[0]

    qty = t["initial_quantity"]
    assert t["initial_stop"] == (96.0 if direction == "BUY" else 104.0)
    assert t["risk_per_share"] == pytest.approx(4.0)
    expected_costs = round_trip_cost(direction, 100.0, exit_price, qty, SIMPLE)
    assert t["costs"] == pytest.approx(expected_costs, abs=0.01)
    assert t["gross_pnl"] == pytest.approx(-4.0 * qty, abs=0.01)
    assert t["realized_pnl"] == pytest.approx(-4.0 * qty - expected_costs, abs=0.01)
    assert t["r_multiple"] == pytest.approx(t["realized_pnl"] / (4.0 * qty), abs=0.001)
    assert t["r_multiple"] < -1.0                      # costs make a stop-out worse than -1R
    assert t["exit_category"] == "stop_loss"
    assert t["mae_r"] is not None and t["mfe_r"] is not None


def test_final_target_is_1_75_r_gross():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])
    result, _ = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    t = result.closed_trades[0]
    assert t["exit_price"] == 107.0
    assert t["target2"] == 107.0
    assert t["r_multiple"] == pytest.approx(1.75, abs=0.001)


def test_no_costs_means_gross_equals_net():
    df = _stop_df("BUY")
    result, _ = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    t = result.closed_trades[0]
    assert t["costs"] == 0.0
    assert t["gross_pnl"] == pytest.approx(t["realized_pnl"], abs=0.01)


# ==========================================================
# Trade-quality metrics
# ==========================================================

def _t(pnl, direction="BUY", category="final_target", mfe_r=0.0, costs=0.0):
    return {
        "realized_pnl": pnl, "direction": direction, "exit_category": category,
        "mfe_r": mfe_r, "costs": costs, "gross_pnl": pnl + costs, "r_multiple": pnl / 100.0,
    }


def test_payoff_ratio_and_break_even_win_rate():
    trades = [_t(300), _t(100), _t(-100, category="stop_loss"), _t(-100, category="stop_loss")]
    m = BacktestEngine._trade_quality_metrics(trades)
    assert m["avg_win"] == 200.0
    assert m["avg_loss"] == 100.0
    assert m["payoff_ratio"] == 2.0
    assert m["breakeven_win_rate"] == pytest.approx(33.33, abs=0.01)
    assert m["avg_r_multiple"] == pytest.approx(0.5)


def test_costs_and_pre_cost_profit_factor():
    trades = [_t(90, costs=10), _t(-110, category="stop_loss", costs=10)]
    m = BacktestEngine._trade_quality_metrics(trades)
    assert m["total_costs"] == 20.0
    assert m["pnl_before_costs"] == 0.0               # +100 gross, -100 gross
    assert m["profit_factor_before_costs"] == 1.0


def test_buy_and_sell_are_reported_separately():
    trades = [_t(100, "BUY"), _t(-50, "BUY"), _t(-80, "SELL"), _t(-20, "SELL")]
    by = BacktestEngine._trade_quality_metrics(trades)["by_direction"]
    assert by["BUY"] == {"trades": 2, "pnl": 50.0, "profit_factor": 2.0, "win_rate": 50.0}
    assert by["SELL"] == {"trades": 2, "pnl": -100.0, "profit_factor": 0.0, "win_rate": 0.0}


def test_losing_stop_outs_that_were_in_profit_first_are_counted():
    trades = [
        _t(-100, category="stop_loss", mfe_r=1.2),
        _t(-100, category="stop_loss", mfe_r=0.6),
        _t(-100, category="stop_loss", mfe_r=0.1),
        _t(50, category="stop_loss", mfe_r=2.0),       # trailing-stop winner: not a loser
    ]
    m = BacktestEngine._trade_quality_metrics(trades)
    assert (m["stop_losers"], m["stop_losers_reached_half_r"], m["stop_losers_reached_1r"]) == (3, 2, 1)


# ==========================================================
# Report text
# ==========================================================

def test_report_no_longer_prints_the_misleading_lines():
    df = _stop_df("BUY")
    result, _ = _run(df, {_day(df, 4): ("X.NS", "BUY")}, cost_model=SIMPLE)
    text = result.report()
    assert "Average R:R" not in text
    assert "Cost per side" not in text
    assert "Avg win / Avg loss" in text and "break-even win rate" in text
    assert "Indian delivery model" in text
    assert "(= production nightly scan)" in text
    assert "By direction (net of costs):" in text


def test_report_says_when_no_costs_were_applied():
    df = _stop_df("BUY")
    result, _ = _run(df, {_day(df, 4): ("X.NS", "BUY")})
    assert "NONE (gross P&L)" in result.report()


# ==========================================================
# CLI helpers + regression + workflow
# ==========================================================

def test_trades_csv_has_every_column(tmp_path):
    path = tmp_path / "trades.csv"
    write_trades_csv([{"symbol": "X.NS", "direction": "BUY", "r_multiple": -1.02, "extra": 1}], str(path))
    header, row = path.read_text().splitlines()
    assert header.split(",") == TRADE_COLUMNS
    assert row.startswith("X.NS,BUY,")


def test_top_liquid_symbols_ranks_by_turnover_and_skips_symbols_without_history():
    history = {"AAA": [{"turnover_lacs": 5}], "BBB": [{"turnover_lacs": 900}], "CCC": [{"turnover_lacs": 50}]}
    watchlist = ["AAA.NS", "BBB.NS", "CCC.NS", "NEW.NS"]
    assert top_liquid_symbols(2, watchlist=watchlist, history=history) == ["BBB.NS", "CCC.NS"]


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_regression_skips_different_setup_and_keeps_baseline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["regression_check.py"])
    base = {"engine_version": "v", "run_config": {"universe": "default", "signal_path": "live"}, "win_rate": 50}
    cur = {"engine_version": "v", "run_config": {"universe": "top50", "signal_path": "live"}, "win_rate": 40}
    _write(tmp_path / "reports/backtest_baseline.json", base)
    _write(tmp_path / "reports/backtest_result_latest.json", cur)

    regression_check.main()

    assert "not comparable" in capsys.readouterr().out
    assert json.loads((tmp_path / "reports/backtest_baseline.json").read_text()) == base


def test_backtest_workflow_exposes_universe_and_signal_path():
    # Plain-text checks: PyYAML is not in requirements.txt (CI has no yaml).
    wf = open(".github/workflows/backtest_and_regression.yml").read()
    universe = wf[wf.index("      universe:"):wf.index("      signal_path:")]
    for option in ('"default"', '"top50"', '"top100"'):
        assert f"- {option}" in universe
    signal = wf[wf.index("      signal_path:"):wf.index("      set_baseline:")]
    assert 'default: "live"' in signal
    run = wf[wf.index("- name: Run Institutional Backtest"):wf.index("- name: Run Regression Check")]
    assert '--universe "${{ inputs.universe }}"' in run and '--signal-path "${{ inputs.signal_path }}"' in run
