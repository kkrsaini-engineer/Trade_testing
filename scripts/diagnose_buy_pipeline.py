"""
DIAGNOSTIC: INSPECT PORTFOLIO STATE + TRACE ONE BUY CANDIDATE (READ-ONLY)

Does NOT modify anything. Two parts:

1. Prints the exact current contents of the requested fields from
   storage/trades/virtual_portfolio_state.json.

2. Scans the watchlist using scanner.scan_symbols() (the SAME method
   Paper Trading's entry path uses) against the REAL, CURRENT persisted
   portfolio state (not a fresh/clean dict) until it finds one real BUY
   candidate, then re-runs ValidationEngine directly on that exact
   symbol/dataframe/portfolio to print the FULL checks dict — showing
   precisely which check failed and the exact rejection message.

Usage:
    python scripts/diagnose_buy_pipeline.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from decision.validation_engine import ValidationEngine  # noqa: E402
from execution.scanner import MarketScanner  # noqa: E402
from market.volatility import fetch_india_vix  # noqa: E402
from paper_trading.virtual_portfolio import VirtualPortfolio  # noqa: E402

logger = get_logger(__name__)

STATE_PATH = "storage/trades/virtual_portfolio_state.json"


def show_portfolio_state() -> dict:
    path = Path(STATE_PATH)
    if not path.exists():
        print(f"No state file found at {STATE_PATH}.")
        return {}

    with open(path) as f:
        state = json.load(f)

    print("=" * 60)
    print("PART 1 — storage/trades/virtual_portfolio_state.json (raw, unmodified)")
    print("=" * 60)
    for key in ("total_capital", "available_capital", "total_pnl", "total_pnl_percent", "exposure"):
        print(f"{key}: {state.get(key)!r}")
    print(f"open_positions count: {len(state.get('open_positions', {}))}")
    print(f"closed_positions count: {len(state.get('closed_positions', []))}")
    return state


def trace_one_buy_candidate() -> None:
    print("\n" + "=" * 60)
    print("PART 2 — Trace one real BUY candidate against the ACTUAL "
          "current portfolio state")
    print("=" * 60)

    portfolio = VirtualPortfolio()  # loads the SAME persisted state Paper Trading uses
    portfolio_dict = portfolio.engine.snapshot()
    print("\nPortfolio dict actually passed to the scanner today:")
    for key in ("total_capital", "available_capital", "total_pnl", "exposure"):
        print(f"  {key}: {portfolio_dict.get(key)!r}")

    symbols = WatchlistManager("storage/watchlist/nifty500.json").load()
    scanner = MarketScanner()
    broker_status = {
        "status": "ONLINE", "mode": "DIAGNOSTIC",
        "connected": True, "order_allowed": True, "available_margin": 1e12,
    }
    # BUG FIX: this diagnostic previously never set "vix", so
    # RiskManager.evaluate() during this trace always saw the hardcoded
    # 20.0 fallback instead of the real market condition — misleading
    # for a tool whose whole purpose is showing exactly what production
    # would see. Same fix as paper_trading_engine.py's monitoring loop.
    market_state = {
        "max_trade_candidates": 20, "max_watchlist": 50,
        "market_open": True, "holiday": False,
        "vix": fetch_india_vix(),
    }

    found = None
    for symbol in symbols:
        r = scanner.scan_symbol(
            symbol=symbol, portfolio=portfolio_dict,
            broker_status=broker_status, market_state=market_state,
        )
        if r.action in ("BUY", "SELL") and r.diagnostics.get("_dataframe") is not None:
            found = (symbol, r)
            break

    if found is None:
        print("\nNo BUY/SELL candidate found in this pass — cannot trace an example.")
        return

    symbol, r = found
    print(f"\nExample candidate found: {symbol} (Signal: {r.action})")
    print(f"portfolio_allowed: {r.portfolio_allowed}")
    print(f"validation_passed: {r.diagnostics.get('validation_passed')}")
    print(f"validation_rejection_reason: {r.diagnostics.get('validation_rejection_reason')}")

    # Re-run ValidationEngine directly to print the FULL checks dict.
    dataframe = r.diagnostics.get("_dataframe")
    final_decision_action = r.action
    from decision.decision_engine import FinalDecision
    fake_decision = FinalDecision(
        action=final_decision_action,
        confidence=r.confidence,
        ranking=r.ranking,
        buy_score=r.diagnostics.get("buy_score", 0.0),
        sell_score=r.diagnostics.get("sell_score", 0.0),
        buy_probability=r.diagnostics.get("buy_probability", 0.0),
        sell_probability=r.diagnostics.get("sell_probability", 0.0),
        expected_return=r.diagnostics.get("expected_return", 0.0),
        expected_drawdown=r.diagnostics.get("expected_drawdown", 0.0),
        expected_hold_days=r.diagnostics.get("expected_hold_days", 0),
        reasons=[],
        diagnostics={},
    )
    validation = ValidationEngine().validate(
        decision=fake_decision, dataframe=dataframe, portfolio=portfolio_dict,
        broker_status=broker_status, market_state=market_state,
    )
    print("\nFull ValidationEngine.checks dict:")
    for k, v in validation.checks.items():
        marker = "  <-- FAILED" if v is False else ""
        print(f"  {k}: {v}{marker}")
    print(f"\nFinal rejection_reason: {validation.rejection_reason}")


if __name__ == "__main__":
    show_portfolio_state()
    trace_one_buy_candidate()
