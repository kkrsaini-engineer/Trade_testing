"""
Test for 1 new bug found on 2026-09-22, while auditing the LIVE repo's real
2-day production trade data (storage/trades/trades_master.csv on
origin/main) to verify the Phase 6 (2026-09-19) fixes were working.

Confirmed via the real, live storage/trades/virtual_portfolio_state.json
that GLAND.NS and IRISDOREME.NS both had a nonzero realized_pnl from a real
partial exit, but NO realized_pnl_percent key at all in the saved JSON --
proving paper_trading/virtual_portfolio.py's save()/_load_or_init() never
round-trips that field, even though portfolio/portfolio.py's
PortfolioPosition dataclass carries it and partial_exit() (fixed in Phase 6)
sets it correctly in memory.

Since a daily script run is a fresh process (this module's own docstring),
this meant: even with the Phase 6 fix applied, realized_pnl_percent would
be correctly set in memory today, then silently reset to 0.0 the next time
the state file is saved and reloaded (tomorrow's run) -- for any position
that had a partial exit on a prior day and is still open.
"""

from portfolio.portfolio import PortfolioEngine, PortfolioState
from paper_trading.virtual_portfolio import VirtualPortfolio


def test_save_persists_realized_pnl_percent_for_an_open_position(tmp_path):
    state_path = tmp_path / "virtual_portfolio_state.json"

    vp = VirtualPortfolio(initial_capital=100_000.0, state_path=str(state_path))
    vp.engine.add_position("RELIANCE", quantity=100, entry_price=100.0, direction="BUY")

    # A partial exit today sets realized_pnl_percent in memory (Phase 6 fix).
    vp.engine.partial_exit("RELIANCE", quantity=50, exit_price=110.0)
    pos = vp.engine.state.open_positions["RELIANCE"]
    assert pos.realized_pnl_percent == 10.0

    vp.save()

    # Simulate tomorrow's run: a brand-new process loading the saved state.
    vp2 = VirtualPortfolio(initial_capital=100_000.0, state_path=str(state_path))
    reloaded = vp2.engine.state.open_positions["RELIANCE"]

    assert reloaded.realized_pnl == 500.0  # already round-tripped correctly before this fix
    assert reloaded.realized_pnl_percent == 10.0  # was silently reset to 0.0 before this fix


def test_save_persists_realized_pnl_percent_for_a_closed_position(tmp_path):
    state_path = tmp_path / "virtual_portfolio_state.json"

    vp = VirtualPortfolio(initial_capital=100_000.0, state_path=str(state_path))
    vp.engine.add_position("TCS", quantity=10, entry_price=1000.0, direction="BUY")
    vp.engine.close_position("TCS", exit_price=950.0)
    vp.save()

    vp2 = VirtualPortfolio(initial_capital=100_000.0, state_path=str(state_path))
    closed = vp2.engine.state.closed_positions[-1]

    assert closed.symbol == "TCS"
    assert closed.realized_pnl_percent == -5.0


def test_loading_an_old_state_file_without_realized_pnl_percent_key_still_works(tmp_path):
    # Backward compatibility: a state file saved BEFORE this fix has no
    # "realized_pnl_percent" key in its open_positions entries at all.
    import json

    state_path = tmp_path / "virtual_portfolio_state.json"
    state_path.write_text(json.dumps({
        "total_capital": 100_000.0,
        "available_capital": 90_000.0,
        "used_capital": 10_000.0,
        "open_positions": {
            "INFY": {
                "symbol": "INFY", "quantity": 10, "entry_price": 1000.0,
                "current_price": 1000.0, "direction": "BUY",
                "unrealized_pnl": 0.0, "unrealized_pnl_percent": 0.0,
                "realized_pnl": 500.0,
                # no "realized_pnl_percent" key -- legacy file.
                "highest_price": 1000.0, "lowest_price": 1000.0,
                "max_profit_percent": 0.0, "max_drawdown_percent": 0.0,
                "status": "OPEN", "updated_at": 0.0,
            }
        },
        "closed_positions": [],
    }))

    vp = VirtualPortfolio(initial_capital=100_000.0, state_path=str(state_path))
    pos = vp.engine.state.open_positions["INFY"]

    assert pos.realized_pnl == 500.0
    assert pos.realized_pnl_percent == 0.0  # falls back to the dataclass default, no crash
