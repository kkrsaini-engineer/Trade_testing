"""
DAILY SCAN SCRIPT

Runs the MarketScanner against a list of NSE symbols and writes a CSV report.
This is what the "Daily Scan" GitHub Action actually executes (the workflow
used to just `echo` a placeholder — it never ran real code).

Usage:
    python scripts/daily_scan.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

# Make repo root importable when run as `python scripts/daily_scan.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from execution.scanner import MarketScanner  # noqa: E402
from market.volatility import fetch_india_vix  # noqa: E402

logger = get_logger(__name__)

# ==========================================================
# SYMBOL UNIVERSE
# ==========================================================
# Loaded from storage/watchlist/nifty500.json (shared with orchestrator.py).
# To scan the FULL official Nifty 500: download the constituent list from
# niftyindices.com (IndexConstituent -> Nifty 500), take the "Symbol"
# column, append ".NS" to each, and save as a JSON list at that same path.
WATCHLIST = WatchlistManager("storage/watchlist/nifty500.json").load()


def run_scan(symbols: list[str]) -> list[dict]:
    scanner = MarketScanner()

    portfolio = {
        "equity": 100000.0,
        "total_capital": 100000.0,
        "total_pnl": 0.0,
        "exposure": 0.0,
        "available_capital": 100000.0,
        "open_positions": {},
    }
    broker_status = {"status": "ONLINE", "mode": "SCAN", "connected": True, "order_allowed": True, "available_margin": 100000.0}
    # See scripts/generate_full_report.py for why "vix" is fetched live
    # here instead of being absent/hardcoded.
    market_state = {
        "max_trade_candidates": 20,
        "max_watchlist": 50,
        "market_open": True,
        "holiday": False,
        "vix": fetch_india_vix(),
    }

    # NOTE: scanner.scan_symbols() intentionally filters down to only
    # executable BUY/SELL candidates (that's what the orchestrator needs
    # for order placement). A scan REPORT needs the status of every symbol
    # (including NO_TRADE/ERROR), so we call scan_symbol() ourselves here.
    total = len(symbols)
    rows = []
    for i, symbol in enumerate(symbols, start=1):
        logger.info("[%d/%d] Scanning %s", i, total, symbol)
        r = scanner.scan_symbol(
            symbol=symbol,
            portfolio=portfolio,
            broker_status=broker_status,
            market_state=market_state,
        )
        rows.append(
            {
                "symbol": r.symbol,
                "action": r.action,
                "score": r.score,
                "probability": r.probability,
                "confidence": r.confidence,
                "ranking": r.ranking,
                "portfolio_allowed": r.portfolio_allowed,
                "latest_close": r.diagnostics.get("latest_close"),
                "market_regime": r.diagnostics.get("market_regime"),
                "error": r.diagnostics.get("error"),
            }
        )
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    if not rows:
        logger.warning("No scan results to write.")
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d rows to %s", len(rows), path)


def main() -> None:
    logger.info("Starting daily scan over %d symbols...", len(WATCHLIST))
    rows = run_scan(WATCHLIST)

    # Sort so the most interesting candidates (BUY/SELL, ranked) show up first.
    order = {"BUY": 0, "SELL": 1, "NO_TRADE": 2, "ERROR": 3}
    rows.sort(key=lambda r: (order.get(r["action"], 9), -(r["ranking"] or 0)))

    Path("reports").mkdir(exist_ok=True)
    write_csv(rows, "reports/daily_scan_results.csv")

    buys = [r for r in rows if r["action"] == "BUY"]
    sells = [r for r in rows if r["action"] == "SELL"]
    errors = [r for r in rows if r["action"] == "ERROR"]

    print("\n=== DAILY SCAN SUMMARY ===")
    print(f"Total scanned : {len(rows)}")
    print(f"BUY candidates: {len(buys)}")
    print(f"SELL candidates: {len(sells)}")
    print(f"Errors: {len(errors)}")

    if buys:
        print("\n--- BUY ---")
        for r in buys:
            print(f"{r['symbol']:15s} score={r['score']:.1f} conf={r['confidence']:.1f} close={r['latest_close']}")

    if sells:
        print("\n--- SELL ---")
        for r in sells:
            print(f"{r['symbol']:15s} score={r['score']:.1f} conf={r['confidence']:.1f} close={r['latest_close']}")

    if errors:
        print("\n--- ERRORS (data fetch/other issues) ---")
        for r in errors:
            print(f"{r['symbol']:15s} error={r['error']}")


if __name__ == "__main__":
    main()
