"""
RUN PAPER TRADING (daily cycle)

Runs one day's paper-trading cycle: monitors every open virtual
position through the Exit Engine, opens new virtual positions for any
fresh BUY/SELL signal that clears production validation, and writes a
daily report.

Usage:
    python scripts/run_paper_trading.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from paper_trading.paper_trading_engine import PaperTradingEngine  # noqa: E402
from storage.trades.trade_diary import TradeDiary  # noqa: E402

logger = get_logger(__name__)

REPORT_PATH = "reports/paper_trading_daily_report.csv"


def average_holding_days(closed_trades: list[dict]) -> float:
    days = [t.get("holding_days", 0) for t in closed_trades]
    return round(sum(days) / len(days), 2) if days else 0.0


def write_daily_report(summary: dict, diary: TradeDiary) -> None:
    Path("reports").mkdir(exist_ok=True)

    open_trades = diary.get_open_trades()
    closed_trades = diary.get_closed_trades()
    snap = summary["portfolio_snapshot"]

    row = {
        "Date": summary["date"],
        "OpenedToday": len(summary["opened_today"]),
        "ClosedToday": len(summary["closed_today"]),
        "PartialExitsToday": len(summary.get("partial_exits_today", [])),
        "Monitored": len(summary["monitored"]),
        "OpenPositions": len(open_trades),
        "ClosedPositions": len(closed_trades),
        "CashBalance": round(snap.get("available_capital", 0.0), 2),
        "PortfolioValue": round(snap.get("portfolio_value", 0.0), 2),
        "RealizedPnL": round(snap.get("total_pnl", 0.0), 2),
        "PortfolioReturnPercent": round(snap.get("portfolio_return_percent", 0.0), 2),
        "WinRate": snap.get("win_rate"),
        "LossRate": snap.get("loss_rate"),
        "AverageHoldingDays": average_holding_days(closed_trades),
        "SectorExposure": json.dumps(snap.get("sector_exposure", {})),
        "ClosedToday_Detail": json.dumps(summary["closed_today"]),
        "OpenedToday_Detail": json.dumps(summary["opened_today"]),
        "PartialExitsToday_Detail": json.dumps(summary.get("partial_exits_today", [])),
    }

    file_exists = Path(REPORT_PATH).exists()
    with open(REPORT_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    logger.info("Daily paper-trading report appended to %s", REPORT_PATH)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the NSE trading-day check (for manual testing on holidays/weekends). "
             "Note: market data will still reflect the last actual trading day's prices, "
             "since no new data exists on a genuine holiday.",
    )
    args = parser.parse_args()

    symbols = WatchlistManager("storage/watchlist/nifty500.json").load()
    if not symbols:
        logger.warning("Watchlist empty; nothing to scan.")
        symbols = []

    engine = PaperTradingEngine()
    summary = engine.run_cycle(symbols, force=args.force)

    ran_cycle = summary.get("status") != "SKIPPED_NON_TRADING_DAY"
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"ran_cycle={'true' if ran_cycle else 'false'}\n")

    write_daily_report(summary, engine.diary)

    if ran_cycle:
        snap = summary["portfolio_snapshot"]
        monitored_count = len(summary["monitored"])
        opened_at_start = summary.get("open_positions_at_start", 0)
        errors = summary.get("monitoring_errors", [])

        if summary.get("cycle_aborted"):
            reason = (
                f"⚠️ Cycle ABORTED early due to a non-recoverable failure: "
                f"{summary.get('cycle_abort_reason', 'see Monitoring Failures alert')}. "
                f"{monitored_count} position(s) were monitored before the abort; "
                f"remaining positions were skipped this cycle."
            )
        elif monitored_count > 0:
            reason = f"{monitored_count} position(s) successfully evaluated."
        elif opened_at_start == 0:
            reason = "No active positions in portfolio."
        elif errors:
            reason = (
                f"Monitoring skipped for all {opened_at_start} open position(s) — "
                f"evaluation errors: " + "; ".join(errors[:5])
                + (f" (+{len(errors)-5} more)" if len(errors) > 5 else "")
            )
        else:
            reason = f"{opened_at_start} open position(s) existed, but none produced a valid evaluation this cycle."

        opened_list = summary.get("opened_today", [])
        opened_lines = ""
        new_trades_capital = 0.0
        if opened_list:
            new_trades_capital = sum(o.get("price", 0) * o.get("quantity", 0) for o in opened_list)
            opened_lines = "\n\nNew Trades Opened\n" + "\n".join(
                f"  {o['symbol']} ({o['action']}) @ {o['price']} x {o.get('quantity', '?')}" for o in opened_list
            )

        final_open_positions = len(snap.get("open_positions", {}))
        opening_balance = summary.get("opening_balance", 0.0)
        remaining_balance = snap.get("available_capital", 0.0)
        portfolio_value = snap.get("portfolio_value", 0)
        # Total capital CURRENTLY tied up across ALL open positions
        # (regardless of when they were opened) — Portfolio Value minus
        # Cash Balance. This is what "Used Balance" should show; the
        # old version only counted TODAY's new trades, which was 0 (and
        # therefore meaningless) on any day with no new opens.
        total_capital_in_positions = portfolio_value - remaining_balance

        new_trades_line = (
            f"New Trades Capital Used: {new_trades_capital:.2f}\n" if opened_list else ""
        )

        Path("reports").mkdir(exist_ok=True)
        with open("reports/paper_trading_summary_latest.json", "w") as f:
            json.dump({
                "date": summary["date"],
                "opening_balance": opening_balance,
                "total_capital_in_positions": total_capital_in_positions,
                "remaining_balance": remaining_balance,
                "portfolio_value": portfolio_value,
                "realized_pnl": snap.get("total_pnl", 0),
                "portfolio_return_percent": snap.get("portfolio_return_percent", 0),
                "opened_today": summary["opened_today"],
                "closed_today": summary["closed_today"],
                "open_positions_now": final_open_positions,
            }, f, indent=2, default=str)

        opened_count = len(summary["opened_today"])
        closed_count = len(summary["closed_today"])
        expected_open_now = opened_at_start - closed_count + opened_count
        reconciliation_line = (
            f"\n\nPosition Count Check\n"
            f"Previous Open ({opened_at_start}) - Closed ({closed_count}) + Opened ({opened_count}) "
            f"= {expected_open_now}\n"
            + (
                f"✓ Matches actual open positions ({final_open_positions})."
                if expected_open_now == final_open_positions
                else f"❌ MISMATCH — actual open positions is {final_open_positions}, "
                     f"expected {expected_open_now} (difference of {final_open_positions - expected_open_now})."
            )
        )

        notify(
            event_type="daily_portfolio_summary",
            message=(
                f"🏁 Paper Trading Completed — {summary['date']}\n"
                f"Opening Balance: {opening_balance:.2f}\n"
                f"Total Capital in Open Positions: {total_capital_in_positions:.2f}\n"
                f"{new_trades_line}"
                f"Remaining Balance: {remaining_balance:.2f}\n\n"
                f"Portfolio Value: {portfolio_value:.2f}\n"
                f"Cash Balance: {remaining_balance:.2f}\n"
                f"Realized PnL: {snap.get('total_pnl', 0):.2f} "
                f"({snap.get('portfolio_return_percent', 0):.2f}%)\n"
                f"Opened: {opened_count} | "
                f"Closed: {closed_count} | "
                f"Monitored: {monitored_count}\n"
                f"Open Positions Now: {final_open_positions}\n"
                f"Reason:\n{reason}"
                f"{opened_lines}"
                f"{reconciliation_line}"
            ),
            dedup_key=f"portfolio_summary::{summary['date']}::{now_ist().strftime('%H:%M:%S.%f')}",
        )

    if summary.get("status") == "SKIPPED_NON_TRADING_DAY":
        print(f"\n=== PAPER TRADING — {summary['date']} ===")
        print("SKIPPED: not an NSE trading day (weekend or holiday). No new entries, no monitoring.")
        return

    print(f"\n=== PAPER TRADING — {summary['date']} ===")
    print(f"Opened today : {len(summary['opened_today'])}")
    for o in summary["opened_today"]:
        print(f"  + {o['symbol']:15s} {o['action']:4s} @ {o['price']}")
    print(f"Closed today : {len(summary['closed_today'])}")
    for c in summary["closed_today"]:
        print(f"  - {c['symbol']:15s} PnL={c['pnl']:.2f}")
    partial_exits = summary.get("partial_exits_today", [])
    print(f"Partial exits today : {len(partial_exits)}")
    for p in partial_exits:
        print(f"  ~ {p['symbol']:15s} booked {p['quantity']} @ {p['exit_price']}, {p['remaining_quantity']} remaining")
    print(f"Monitored (held): {len(summary['monitored'])}")

    snap = summary["portfolio_snapshot"]
    print(f"\nPortfolio value : {snap.get('portfolio_value', 0):.2f}")
    print(f"Cash balance    : {snap.get('available_capital', 0):.2f}")
    print(f"Realized PnL    : {snap.get('total_pnl', 0):.2f}")
    print(f"Return          : {snap.get('portfolio_return_percent', 0):.2f}%")
    if snap.get("win_rate") is not None:
        print(f"Win rate        : {snap['win_rate']}%")


if __name__ == "__main__":
    main()
