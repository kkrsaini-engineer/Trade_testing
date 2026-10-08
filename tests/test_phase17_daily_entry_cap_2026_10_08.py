"""
2026-10-08 — daily entry cap in the live Morning Executor (user-approved,
max 5 new positions per morning, best-ranked first).

Why: on 2026-10-08 the executor opened 46 positions in one morning (62
open, median size ~Rs 5.5k, round-trip cost ~0.6%). In two top50
backtests the days with >= 5 entries lost Rs 15.5k and Rs 10.9k while all
the other trades were about break-even. Mirrored: the cap counts BUY and
SELL alike.

Sizing: 500000 / len(candidates) would still size each of 5 positions at
1/88 of the capital, so the split is now over min(candidates, cap).
"""

import json
from pathlib import Path

import pytest

import scripts.morning_executor as me
from analytics.backtest_engine import BacktestEngine
from paper_trading.virtual_portfolio import VirtualPortfolio
from tests.test_phase11_backtest_live_flow_2026_10_06 import _day, _series
from tests.test_phase16_backtest_experiments_2026_10_07 import MultiScanner


# ==========================================================
# Sizing
# ==========================================================

def test_cap_splits_the_capital_over_the_positions_that_can_open():
    # 88 candidates, cap 5: min(5% of 500k = 25k, 500k / 5 = 100k) = 25k
    assert me.entry_allocation(500_000.0, 88, cap=5) == 25_000.0


def test_without_a_cap_sizing_is_the_old_formula():
    assert me.entry_allocation(500_000.0, 88, cap=0) == pytest.approx(500_000.0 / 88)


def test_few_candidates_are_not_sized_above_the_old_rule():
    assert me.entry_allocation(500_000.0, 3, cap=5) == 25_000.0     # 5% binds
    assert me.entry_allocation(5_000_000.0, 3, cap=5) == pytest.approx(500_000.0 / 3)


def test_allocation_follows_available_capital():
    assert me.entry_allocation(200_000.0, 88, cap=5) == 10_000.0


def test_default_cap_is_five():
    assert me.MAX_NEW_ENTRIES_PER_DAY == 5


# ==========================================================
# main(): cap applied in ranking order, BUY and SELL alike
# ==========================================================

def _run_executor(tmp_path, monkeypatch, directions, cap=5):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    candidates = [
        {
            "symbol": f"S{i}.NS", "direction": d, "prev_close": 100.0, "atr_14": 2.0,
            "stop_loss": 96.0 if d == "BUY" else 104.0, "target1": 104.0 if d == "BUY" else 96.0,
            "ranking": 90.0 - i, "probability": 70.0, "confidence": 80.0, "market_regime": "BULL",
        }
        for i, d in enumerate(directions)
    ]
    today = me.date.today()
    (tmp_path / "reports/candidates_order.json").write_text(json.dumps({
        "scan_date": me.previous_trading_day(today).isoformat(), "candidates": candidates,
    }))
    fetched, sent = [], []
    monkeypatch.setattr(me, "MAX_NEW_ENTRIES_PER_DAY", cap)
    monkeypatch.setattr(me, "is_trading_day", lambda d: True)
    monkeypatch.setattr(me, "fetch_open_price", lambda s, *a, **k: (fetched.append(s) or 100.0, "ok"))
    monkeypatch.setattr(me, "check_overnight_news", lambda *a, **k: (True, ""))
    monkeypatch.setattr(me, "notify", lambda **kw: sent.append(kw["message"]))
    me.main()
    state = json.loads((tmp_path / "storage/trades/virtual_portfolio_state.json").read_text()) \
        if (tmp_path / "storage/trades/virtual_portfolio_state.json").exists() else None
    return fetched, sent, state


@pytest.mark.parametrize("directions", [["BUY"] * 9, ["SELL"] * 9, ["BUY", "SELL"] * 5])
def test_only_the_top_ranked_five_are_opened(tmp_path, monkeypatch, directions):
    fetched, sent, state = _run_executor(tmp_path, monkeypatch, directions)
    assert fetched == [f"S{i}.NS" for i in range(5)]               # best-ranked first, rest never fetched
    assert sorted(state["open_positions"]) == [f"S{i}.NS" for i in range(5)]
    assert "Executed: 5 | Skipped: " in sent[0]
    assert f"{len(directions) - 5} lower-ranked candidate(s) not traded" in sent[0]
    assert sent[0].count("Daily entry cap") == 1                    # one line, not one per symbol


def test_positions_are_sized_at_five_percent_not_one_over_n(tmp_path, monkeypatch):
    _, _, state = _run_executor(tmp_path, monkeypatch, ["BUY"] * 20)
    quantities = sorted((p["quantity"] for p in state["open_positions"].values()), reverse=True)
    # 5% of AVAILABLE capital, which shrinks with every entry (as before):
    # 250, 237, 225, 214, 203 shares at Rs 100. The old 1/20 split gave 50 (Rs 5k).
    assert quantities[0] == 250 and quantities[-1] >= 200


def test_cap_zero_turns_it_off(tmp_path, monkeypatch):
    _, _, state = _run_executor(tmp_path, monkeypatch, ["BUY"] * 8, cap=0)
    assert len(state["open_positions"]) == 8


def test_cap_is_not_used_up_by_skipped_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(me, "check_overnight_news", lambda *a, **k: (True, ""))
    # first two candidates gap up 2% (chase filter) -> the next five still open
    prices = {"S0.NS": 102.0, "S1.NS": 102.0}
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    cands = [{"symbol": f"S{i}.NS", "direction": "BUY", "prev_close": 100.0, "atr_14": 2.0,
              "stop_loss": 96.0, "target1": 110.0, "ranking": 90.0 - i} for i in range(9)]
    today = me.date.today()
    (tmp_path / "reports/candidates_order.json").write_text(json.dumps({
        "scan_date": me.previous_trading_day(today).isoformat(), "candidates": cands,
    }))
    monkeypatch.setattr(me, "MAX_NEW_ENTRIES_PER_DAY", 5)
    monkeypatch.setattr(me, "is_trading_day", lambda d: True)
    monkeypatch.setattr(me, "fetch_open_price", lambda s, *a, **k: (prices.get(s, 100.0), "ok"))
    monkeypatch.setattr(me, "notify", lambda **kw: None)
    me.main()
    state = json.loads((tmp_path / "storage/trades/virtual_portfolio_state.json").read_text())
    assert sorted(state["open_positions"]) == ["S2.NS", "S3.NS", "S4.NS", "S5.NS", "S6.NS"]


def test_executor_source_uses_the_shared_allocation_rule():
    source = Path("scripts/morning_executor.py").read_text()
    assert "allocation = entry_allocation(available, len(candidates))" in source
    assert "500000.0 / max(len(candidates), 1)" not in source


# ==========================================================
# Backtest uses the same split when its cap is on
# ==========================================================

def _backtest_notional(cap):
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])
    data = {f"S{i}.NS": df.copy() for i in range(40)}
    signals = {_day(df, 4): [(f"S{i}.NS", "BUY", 90.0 - i) for i in range(40)]}
    result = BacktestEngine(scanner=MultiScanner(signals)).run(
        historical_data=data, initial_capital=500_000.0, min_history=3, max_new_entries_per_day=cap,
    )
    return {round(t["entry_price"] * t["initial_quantity"]) for t in result.closed_trades}, len(result.closed_trades)


def test_backtest_cap_sizing_matches_the_live_rule():
    notionals, trades = _backtest_notional(cap=5)
    assert trades == 5
    assert max(notionals) == 25_000 and min(notionals) >= 20_000   # 5% of available, shrinking


def test_backtest_without_cap_keeps_the_old_split():
    notionals, trades = _backtest_notional(cap=0)
    assert trades > 5                               # capital, not a cap, is the limit
    assert max(notionals) == 12_500                 # 500k / 40 candidates, as before
