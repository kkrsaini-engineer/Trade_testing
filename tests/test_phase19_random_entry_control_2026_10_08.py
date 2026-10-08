"""
2026-10-08 — random-entry CONTROL for the backtest (user-approved).

Four live-path runs (net -Rs 14k .. -9k) and a ranking test (Spearman -0.05)
say the scan's score doesn't separate good trades from bad. The control
answers the next question: does the scan's choice of stock/day add anything
over the exits? signal_path="random" picks random symbols with a random
BUY/SELL on random days; gap filters, sizing, the real exit engine and costs
are identical. It is a measuring stick, not a strategy.
"""

import random
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analytics.backtest_engine import BacktestEngine, _atr14, random_candidates
from data.data_engine import DataBundle
from features.indicators.volatility import VolatilityIndicators
from scripts.run_backtest import random_summary_text, summarize_random_runs
from tests.test_phase11_backtest_live_flow_2026_10_06 import FakeScanner

TOTAL = 500_000.0


def _walk(seed, rows=60, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(0.0005, 0.015, rows))
    open_ = np.concatenate([[start], close[:-1]]) * (1 + rng.normal(0, 0.003, rows))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, rows)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, rows)))
    return pd.DataFrame({
        "timestamp": pd.bdate_range("2026-01-01", periods=rows),
        "open": open_, "high": high, "low": low, "close": close, "volume": [1e6] * rows,
    })


def _data(n=8):
    return {f"S{i}.NS": _walk(i) for i in range(n)}


def _run(signal_path="random", scanner=None, **kwargs):
    scanner = scanner or FakeScanner({})
    result = BacktestEngine(scanner=scanner).run(
        historical_data=_data(), initial_capital=TOTAL, min_history=20, signal_path=signal_path, **kwargs,
    )
    return result, scanner


def _bundles(n=6):
    return {f"S{i}.NS": DataBundle(symbol=f"S{i}.NS", market=_walk(i), fundamentals={}, news=[]) for i in range(n)}


# ==========================================================
# The control never touches the scan
# ==========================================================

def test_random_path_never_calls_the_scanners_signal_functions():
    result, scanner = _run()
    assert scanner.calls == []                       # neither scan_symbol nor scan_symbols
    assert result.closed_trades                      # but trades happen and exits run
    assert result.metrics["signal_path"] == "random"


def test_report_says_it_is_a_control():
    result, _ = _run(random_seed=3)
    text = result.report()
    assert "CONTROL: random entries, NOT the scan" in text
    assert "Random control       : seed 3, 2 candidates/day, 10% SELL" in text


def test_unknown_path_still_rejected():
    with pytest.raises(ValueError):
        _run(signal_path="magic")


# ==========================================================
# Reproducible, and different per seed
# ==========================================================

def _signature(result):
    return [(t["symbol"], t["direction"], t["entry_date"], t["exit_date"], round(t["realized_pnl"], 2))
            for t in result.closed_trades]


def test_same_seed_same_trades():
    assert _signature(_run(random_seed=7)[0]) == _signature(_run(random_seed=7)[0])


def test_different_seeds_different_trades():
    assert _signature(_run(random_seed=1)[0]) != _signature(_run(random_seed=2)[0])


# ==========================================================
# Direction mix — BUY and SELL treated identically, just by share
# ==========================================================

@pytest.mark.parametrize("share,expected", [(0.0, {"BUY"}), (1.0, {"SELL"})])
def test_sell_share_extremes(share, expected):
    result, _ = _run(random_sell_share=share, random_seed=4)
    assert {t["direction"] for t in result.closed_trades} == expected


def test_mixed_share_gives_both_directions():
    result, _ = _run(random_sell_share=0.5, random_candidates_per_day=3, random_seed=5)
    assert {t["direction"] for t in result.closed_trades} == {"BUY", "SELL"}


# ==========================================================
# Candidate generator
# ==========================================================

def test_candidates_have_the_scan_format_and_correct_levels():
    cands = random_candidates(random.Random(0), _bundles(), count=4, sell_share=0.5)
    assert len(cands) == 4 and len({c["symbol"] for c in cands}) == 4
    for c in cands:
        assert c["market_regime"] == "RANDOM_CONTROL"
        assert c["atr_14"] > 0
        if c["direction"] == "BUY":
            assert c["stop_loss"] < c["prev_close"] < c["target1"]
        else:
            assert c["target1"] < c["prev_close"] < c["stop_loss"]      # mirrored


def test_count_is_capped_by_available_symbols_and_zero_gives_none():
    assert len(random_candidates(random.Random(0), _bundles(3), count=10, sell_share=0.1)) == 3
    assert random_candidates(random.Random(0), _bundles(3), count=0, sell_share=0.1) == []


def test_atr_matches_the_production_indicator():
    df = _walk(1, rows=120)
    expected = VolatilityIndicators().calculate(df.copy())["atr_14"].iloc[-1]
    assert _atr14(df) == pytest.approx(expected, rel=1e-6)


# ==========================================================
# Summary over seeds (CLI)
# ==========================================================

class _Res:
    def __init__(self, trades):
        self.closed_trades = trades


def _t(pnl, costs=10.0):
    return {"realized_pnl": pnl, "costs": costs}


def test_summary_means_and_gross():
    summary = summarize_random_runs([_Res([_t(100), _t(-40)]), _Res([_t(-100), _t(-20), _t(30)])])
    assert summary["seeds"] == 2
    assert summary["per_seed"][0] == {
        "seed": 0, "trades": 2, "net_pnl": 60.0, "gross_pnl": 80.0, "costs": 20.0,
        "profit_factor": 2.5, "win_rate": 50.0,
    }
    assert summary["mean_net_pnl"] == pytest.approx((60.0 - 90.0) / 2)
    assert summary["mean_trades"] == 2.5
    assert summary["sd_net_pnl"] > 0


def test_summary_text_has_every_seed_and_a_mean_row():
    text = random_summary_text(summarize_random_runs([_Res([_t(50)]), _Res([_t(-70)])]))
    assert "RANDOM-ENTRY CONTROL: 2 seed(s)" in text and "MEAN" in text
    assert text.count("\n") >= 4


# ==========================================================
# Wiring
# ==========================================================

def test_cli_and_workflow_wiring():
    cli = Path("scripts/run_backtest.py").read_text()
    for flag in ("--random-seeds", "--random-candidates-per-day", "--random-sell-share"):
        assert flag in cli
    assert 'choices=["live", "universe", "random"]' in cli
    wf = Path(".github/workflows/backtest_and_regression.yml").read_text()
    assert '          - "random"' in wf
    assert "      random_setup:" in wf and 'default: "5,2"' in wf
    run = wf[wf.index("- name: Run Institutional Backtest"):wf.index("- name: Run Regression Check")]
    assert '--random-seeds "${RS%%,*}" --random-candidates-per-day "${RS##*,}"' in run


def test_workflow_input_count_stays_within_githubs_limit():
    wf = Path(".github/workflows/backtest_and_regression.yml").read_text()
    inputs = wf[wf.index("    inputs:"):wf.index("jobs:")]
    names = [line for line in inputs.splitlines() if line.startswith("      ") and not line.startswith("       ")
             and line.strip().endswith(":")]
    assert len(names) <= 10


def test_shell_split_of_random_setup():
    out = subprocess.run(
        ["bash", "-c", 'RS="5,2"; echo "${RS%%,*}" "${RS##*,}"'], capture_output=True, text=True,
    ).stdout.split()
    assert out == ["5", "2"]
