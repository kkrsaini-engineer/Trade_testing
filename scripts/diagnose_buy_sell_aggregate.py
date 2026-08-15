"""
DIAGNOSTIC: AGGREGATE BUY/SELL PIPELINE AUDIT (READ-ONLY)

Does NOT modify anything. Scans the ENTIRE watchlist against the ACTUAL
current (possibly corrupted) persisted portfolio state — the SAME state
Paper Trading actually uses — and produces aggregated rejection-category
counts for BOTH the BUY and SELL pipelines separately.

Categorization rule: a symbol is only attributed to a single category
(capital/cash_reserve, insufficient history, volume, turnover) if THAT
is the ONLY failing check category. If more than one category fails
simultaneously for the same symbol, it is counted under "multiple
simultaneous failures" instead — never assumed to be capital alone.

Usage:
    python scripts/diagnose_buy_sell_aggregate.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from execution.scanner import MarketScanner  # noqa: E402
from market.volatility import fetch_india_vix  # noqa: E402
from paper_trading.virtual_portfolio import VirtualPortfolio  # noqa: E402

logger = get_logger(__name__)

# IMPORTANT: only checks that ACTUALLY set validation.rejection_reason
# (confirmed by direct inspection of decision/validation_engine.py) are
# used for categorization. "cash_reserve" and "turnover" both only
# append a warning and feed an informational score — they NEVER cause
# validation.passed to become False on their own, so including them
# here would over-count "multiple simultaneous failures" and misreport
# them as blocking causes.
CATEGORY_CHECKS = {
    "capital_cash_reserve": {"capital"},  # capital blocks; cash_reserve is warning-only
    "insufficient_history": {"minimum_history"},
    "volume": {"average_volume", "current_volume"},
    "turnover": set(),  # turnover NEVER blocks on its own (warning-only) — always 0 by construction
}


def categorize(checks: dict) -> str:
    """Which category bucket(s) have at least one failing check."""
    failed_categories = set()
    for category, check_names in CATEGORY_CHECKS.items():
        if any(checks.get(name) is False for name in check_names):
            failed_categories.add(category)

    # Any OTHER failing check outside the 4 named categories also counts
    # toward "multiple simultaneous failures" bookkeeping accuracy, but
    # we report against the 4 requested buckets specifically below.
    if len(failed_categories) == 0:
        return "other_or_none"
    if len(failed_categories) == 1:
        return next(iter(failed_categories))
    return "multiple"


def main() -> None:
    portfolio = VirtualPortfolio()
    portfolio_dict = portfolio.engine.snapshot()
    print("Portfolio dict used for this audit (actual current persisted state):")
    for key in ("total_capital", "available_capital", "total_pnl", "exposure"):
        print(f"  {key}: {portfolio_dict.get(key)!r}")

    symbols = WatchlistManager("storage/watchlist/nifty500.json").load()
    scanner = MarketScanner()
    broker_status = {
        "status": "ONLINE", "mode": "DIAGNOSTIC",
        "connected": True, "order_allowed": True, "available_margin": 1e12,
    }
    # BUG FIX: same as diagnose_buy_pipeline.py — this diagnostic never
    # set "vix", so RiskManager.evaluate() always saw the hardcoded 20.0
    # fallback during this audit instead of the real market condition.
    market_state = {
        "max_trade_candidates": 20, "max_watchlist": 50,
        "market_open": True, "holiday": False,
        "vix": fetch_india_vix(),
    }

    buy_results = []
    sell_results = []

    print(f"\nScanning {len(symbols)} symbols against the ACTUAL current portfolio state...")
    for i, symbol in enumerate(symbols):
        r = scanner.scan_symbol(
            symbol=symbol, portfolio=portfolio_dict,
            broker_status=broker_status, market_state=market_state,
        )
        if r.action == "BUY":
            buy_results.append(r)
        elif r.action == "SELL":
            sell_results.append(r)
        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{len(symbols)} scanned")

    for label, results in (("BUY", buy_results), ("SELL", sell_results)):
        print("\n" + "=" * 50)
        print(f"{label} PIPELINE")
        print("=" * 50)

        total = len(results)
        cat_counts = Counter()
        for r in results:
            if r.portfolio_allowed:
                cat_counts["success"] += 1
                continue

            # Re-run ValidationEngine directly to get the FULL checks
            # dict for this exact symbol (scanner diagnostics only store
            # the pass/fail summary, not the full per-check breakdown).
            dataframe = r.diagnostics.get("_dataframe")
            if dataframe is None:
                cat_counts["no_dataframe_available"] += 1
                continue

            from decision.decision_engine import FinalDecision
            from decision.validation_engine import ValidationEngine

            fake_decision = FinalDecision(
                action=r.action, confidence=r.confidence, ranking=r.ranking,
                buy_score=r.diagnostics.get("buy_score", 0.0),
                sell_score=r.diagnostics.get("sell_score", 0.0),
                buy_probability=r.diagnostics.get("buy_probability", 0.0),
                sell_probability=r.diagnostics.get("sell_probability", 0.0),
                expected_return=r.diagnostics.get("expected_return", 0.0),
                expected_drawdown=r.diagnostics.get("expected_drawdown", 0.0),
                expected_hold_days=r.diagnostics.get("expected_hold_days", 0),
                reasons=[], diagnostics={},
            )
            validation = ValidationEngine().validate(
                decision=fake_decision, dataframe=dataframe, portfolio=portfolio_dict,
                broker_status=broker_status, market_state=market_state,
            )
            category = categorize(validation.checks)
            cat_counts[category] += 1

        print(f"Total {label} candidates: {total}")
        print(f"Rejected only because of capital/cash_reserve: {cat_counts.get('capital_cash_reserve', 0)}")
        print(f"Rejected because of insufficient history: {cat_counts.get('insufficient_history', 0)}")
        print(f"Rejected because of volume: {cat_counts.get('volume', 0)}")
        print(f"Rejected because of turnover: {cat_counts.get('turnover', 0)}")
        print(f"Rejected because of multiple simultaneous failures: {cat_counts.get('multiple', 0)}")
        other = cat_counts.get('other_or_none', 0) + cat_counts.get('no_dataframe_available', 0)
        if other:
            print(f"Rejected for other reasons (outside the 4 named categories): {other}")
        print(f"Successfully reached order placement: {cat_counts.get('success', 0)}")


if __name__ == "__main__":
    main()
