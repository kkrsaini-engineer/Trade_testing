"""
2026-10-08 — Morning Executor entry limits and sizing ("A-plan",
user-approved). The rules themselves live in risk/entry_sizing.py (unit
tests in test_phase18); these tests drive scripts/morning_executor.main().

Why: on 2026-10-08 the executor opened 46 positions in one morning (62
open, median size ~Rs 5.5k, round-trip cost ~0.6%). Old sizing was
min(5% of AVAILABLE capital, 500k / number of candidates), and the only
real exposure limit was an accident ("15+ positions AND 75% invested").
Now: 5% of TOTAL capital per position (min Rs 10k), at most 10 new
positions per morning (best-ranked first), at most 40% of capital deployed
in one morning, and entries stop at 85% exposure. BUY and SELL are counted
and sized identically.
"""

import json
from pathlib import Path

import pytest

import risk.entry_sizing as es
import scripts.morning_executor as me


def _write_candidates(tmp_path, directions, first=0, prev_closes=None):
    prev_closes = prev_closes or {}
    candidates = []
    for i, d in enumerate(directions):
        sym = f"S{first + i}.NS"
        prev = prev_closes.get(sym, 100.0)
        candidates.append({
            "symbol": sym, "direction": d, "prev_close": prev, "atr_14": prev * 0.02,
            "stop_loss": prev * (0.96 if d == "BUY" else 1.04), "target1": prev * (1.04 if d == "BUY" else 0.96),
            "ranking": 90.0 - i, "probability": 70.0, "confidence": 80.0, "market_regime": "BULL",
        })
    today = me.date.today()
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports/candidates_order.json").write_text(json.dumps({
        "scan_date": me.previous_trading_day(today).isoformat(), "candidates": candidates,
    }))


def _state(tmp_path):
    path = tmp_path / "storage/trades/virtual_portfolio_state.json"
    return json.loads(path.read_text()) if path.exists() else None


def _morning(tmp_path, monkeypatch, directions, first=0, prices=None, morning_deploy=1.0, cap=10,
             max_exposure=None, prev_closes=None):
    """One run of main(). Defaults lift the 40% morning limit so the other
    limits can be tested on their own."""
    monkeypatch.chdir(tmp_path)
    _write_candidates(tmp_path, directions, first, prev_closes)
    fetched, sent = [], []
    prices = prices or {}
    monkeypatch.setattr(me, "MAX_NEW_ENTRIES_PER_DAY", cap)
    monkeypatch.setattr(es, "MAX_MORNING_DEPLOY", morning_deploy)
    if max_exposure is not None:
        monkeypatch.setattr(es, "MAX_EXPOSURE", max_exposure)
        monkeypatch.setattr(me, "MAX_EXPOSURE", max_exposure)
    monkeypatch.setattr(me, "is_trading_day", lambda d: True)
    monkeypatch.setattr(me, "fetch_open_price", lambda s, *a, **k: (fetched.append(s) or prices.get(s, 100.0), "ok"))
    monkeypatch.setattr(me, "check_overnight_news", lambda *a, **k: (True, ""))
    monkeypatch.setattr(me, "notify", lambda **kw: sent.append(kw["message"]))
    me.main()
    return fetched, sent, _state(tmp_path)


# ==========================================================
# Constants
# ==========================================================

def test_the_user_approved_numbers():
    assert es.POSITION_FRACTION == 0.05
    assert es.MIN_POSITION_VALUE == 10_000.0
    assert es.MAX_EXPOSURE == 0.85
    assert es.MAX_MORNING_DEPLOY == 0.40
    assert me.MAX_NEW_ENTRIES_PER_DAY == 10


# ==========================================================
# Daily entry cap — ranking order, BUY and SELL alike
# ==========================================================

@pytest.mark.parametrize("directions", [["BUY"] * 14, ["SELL"] * 14, ["BUY", "SELL"] * 7])
def test_only_the_top_ranked_ten_are_opened(tmp_path, monkeypatch, directions):
    fetched, sent, state = _morning(tmp_path, monkeypatch, directions)
    assert fetched == [f"S{i}.NS" for i in range(10)]            # best-ranked first, rest never fetched
    assert sorted(state["open_positions"]) == sorted(f"S{i}.NS" for i in range(10))
    assert "Executed: 10 | Skipped: 4" in sent[0]
    assert "4 lower-ranked candidate(s) not traded" in sent[0]
    assert sent[0].count("Daily entry cap") == 1                  # one line, not one per symbol


def test_each_position_is_five_percent_of_total_capital(tmp_path, monkeypatch):
    _, _, state = _morning(tmp_path, monkeypatch, ["BUY"] * 6)
    # Rs 25,000 at Rs 100 = 250 shares, every time — it does NOT shrink as cash is used.
    assert {p["quantity"] for p in state["open_positions"].values()} == {250}


def test_cap_zero_turns_the_cap_off(tmp_path, monkeypatch):
    _, _, state = _morning(tmp_path, monkeypatch, ["BUY"] * 12, cap=0)
    assert len(state["open_positions"]) == 12


def test_cap_is_not_used_up_by_skipped_candidates(tmp_path, monkeypatch):
    # first two gap up 2% (chase filter) -> the next five still open
    _, _, state = _morning(tmp_path, monkeypatch, ["BUY"] * 9, prices={"S0.NS": 102.0, "S1.NS": 102.0}, cap=5)
    assert sorted(state["open_positions"]) == ["S2.NS", "S3.NS", "S4.NS", "S5.NS", "S6.NS"]


# ==========================================================
# 40% per morning
# ==========================================================

@pytest.mark.parametrize("directions", [["BUY"] * 15, ["SELL"] * 15, ["BUY", "SELL"] * 8])
def test_one_morning_deploys_at_most_forty_percent(tmp_path, monkeypatch, directions):
    fetched, sent, state = _morning(tmp_path, monkeypatch, directions, morning_deploy=0.40)
    assert len(state["open_positions"]) == 8                      # 8 x Rs 25k = Rs 200k = 40%
    assert len(fetched) == 8                                      # the rest cost no network call
    assert "No room left (morning deploy limit): " in sent[0]
    assert "Deployed this morning: Rs 200,000" in sent[0]


# ==========================================================
# 85% exposure, last entry shrunk to fit
# ==========================================================

def test_entries_stop_at_eighty_five_percent_over_several_mornings(tmp_path, monkeypatch):
    for morning in range(4):
        _morning(tmp_path, monkeypatch, ["BUY"] * 10, first=morning * 10)
    state = _state(tmp_path)
    assert len(state["open_positions"]) == 17                     # 10 + 7: 17 x Rs 25k = Rs 425k = 85%
    assert state["exposure"] == pytest.approx(0.85)
    assert state["available_capital"] == pytest.approx(75_000.0)  # 15% stays cash


def test_last_entry_is_shrunk_to_fit_not_refused(tmp_path, monkeypatch):
    # 83% limit = Rs 415k: after 10 x Rs 25k, room is Rs 165k = 6 x 25k + 15k
    _morning(tmp_path, monkeypatch, ["BUY"] * 10, max_exposure=0.83)
    _, _, state = _morning(tmp_path, monkeypatch, ["BUY"] * 10, first=10, max_exposure=0.83)
    quantities = sorted(p["quantity"] for p in state["open_positions"].values())
    assert len(quantities) == 17 and quantities[0] == 150        # the 17th gets Rs 15k
    assert state["exposure"] == pytest.approx(0.83)


def test_a_position_below_the_minimum_is_not_opened(tmp_path, monkeypatch):
    # 82% limit: room after 10 entries is Rs 160k = 6 x 25k + Rs 10k... use 81.9% -> Rs 9.5k left
    _morning(tmp_path, monkeypatch, ["BUY"] * 10, max_exposure=0.819)
    _, sent, state = _morning(tmp_path, monkeypatch, ["BUY"] * 10, first=10, max_exposure=0.819)
    assert len(state["open_positions"]) == 16
    assert "No room left (exposure limit)" in sent[0]


def test_old_75_percent_gate_with_many_positions_is_gone():
    snap = {"total_capital": 500_000.0, "available_capital": 100_000.0, "exposure": 0.80,
            "open_positions": {f"S{i}": {} for i in range(62)}}
    assert me.check_capital_portfolio_risk(snap)[0] is True


def test_exposure_gate_blocks_at_eighty_five_percent():
    snap = {"total_capital": 500_000.0, "available_capital": 80_000.0, "exposure": 0.85, "open_positions": {}}
    ok, reason = me.check_capital_portfolio_risk(snap)
    assert ok is False and "85%" in reason


def test_cash_floor_still_blocks():
    snap = {"total_capital": 500_000.0, "available_capital": 25_000.0, "exposure": 0.5, "open_positions": {}}
    assert me.check_capital_portfolio_risk(snap)[0] is False


# ==========================================================
# Expensive shares
# ==========================================================

def test_share_dearer_than_the_position_size_is_skipped_and_explained(tmp_path, monkeypatch):
    fetched, sent, state = _morning(
        tmp_path, monkeypatch, ["BUY"] * 3, prices={"S0.NS": 30_000.0}, prev_closes={"S0.NS": 30_000.0},
    )
    assert "S0.NS" not in state["open_positions"]
    assert sorted(state["open_positions"]) == ["S1.NS", "S2.NS"]
    assert "One share (Rs 30,000) costs more than the Rs 25,000 position size" in sent[0]


def test_executor_source_uses_the_shared_sizing_module():
    source = Path("scripts/morning_executor.py").read_text()
    assert "from risk.entry_sizing import" in source
    assert "500000.0 / max(len(candidates), 1)" not in source
    assert "available * 0.05" not in source
