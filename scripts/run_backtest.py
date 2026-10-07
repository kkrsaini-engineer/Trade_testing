"""
MODULE 4 — INSTITUTIONAL BACKTESTING (CLI)

Thin command-line wrapper around analytics.backtest_engine.BacktestEngine
(the single canonical implementation — no logic duplicated here).

Roadmap coverage (Phase 2, Module 4):
    [x] Multi-year testing     — --period up to 10y
    [x] Bull/Bear/Range markets — regime_breakdown: metrics segmented by the
                                  SAME live MarketRegimeEngine's daily label
    [x] Walk-forward evaluation — walk_forward_windows: 4 sequential
                                  non-overlapping windows, metrics per window
    [x] Historical replay      — BacktestEngine.run()
    [x] Portfolio simulation   — equity_curve, closed_trades tracked
    Metrics — all 9 confirmed present in BacktestResult.metrics:
    [x] Win Rate  [x] Profit Factor  [x] CAGR  [x] Max Drawdown
    [x] Sharpe Ratio  [x] Sortino Ratio  [x] Expectancy
    [x] BUY Accuracy  [x] SELL Accuracy

Fetches real multi-year historical price data (via yfinance — requires
internet, run via GitHub Actions) for a representative symbol sample,
then runs the full historical replay / portfolio simulation.

This is a periodic VALIDATION tool, not a daily task — run manually
whenever you want to check strategy performance across historical
market conditions.

Usage:
    python scripts/run_backtest.py --period 2y
    python scripts/run_backtest.py --period 5y --symbols RELIANCE.NS,TCS.NS
    python scripts/run_backtest.py --period 2y --universe top50 --realistic-costs

2026-10-07: every closed trade is also written to
reports/backtest_trades_latest.csv (entry/exit, exit reason, costs, net R,
MFE/MAE in R) so stop/target changes can be judged trade by trade.
--universe topN picks the N most liquid watchlist symbols (NSE turnover
history) instead of the 11-stock sample. --signal-path universe tests the
two-pass scan path production does NOT run (see BacktestEngine.run()).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yfinance as yf  # noqa: E402

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from analytics.backtest_engine import BacktestEngine  # noqa: E402
from data.fundamental_data import normalize_fundamentals  # noqa: E402
from risk.transaction_costs import CostModel  # noqa: E402

logger = get_logger(__name__)

# BacktestEngine re-runs the full scan pipeline once PER historical day
# (e.g. 300 days = 300 full pipeline runs) — each one logs every single
# indicator/engine step at INFO level. That's appropriate for a single
# live scan, but produces thousands of noisy lines here. Quiet the
# noisiest internal modules so only this script's own progress/report
# output is visible; nothing about the actual computation changes.
for _noisy_logger in [
    "execution.scanner", "execution.broker", "features.feature_engineering",
    "features.technical_features", "features.indicators.moving_average",
    "features.indicators.momentum", "features.indicators.volatility",
    "features.indicators.volume", "features.indicators.breakout",
    "features.indicators.ichimoku", "features.indicators.pattern",
    "features.multi_timeframe", "news.sentiment_engine", "market.market_regime",
    "strategy.buy_strategy", "strategy.sell_strategy", "strategy.buy_scoring",
    "strategy.sell_scoring", "strategy.buy_probability", "strategy.sell_probability",
    "decision.decision_engine", "decision.validation_engine",
    "risk.risk_manager", "risk.position_sizing", "risk.portfolio_rules",
]:
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)

# These two specifically emit WARNING-level "Trade rejected"/"Validation
# warnings" on every single historical day evaluated (hundreds of them
# in a multi-year backtest) — routine and expected during replay, not
# actionable here, so raise to ERROR only for backtest usage.
for _warn_heavy_logger in ["decision.validation_engine", "risk.risk_manager"]:
    logging.getLogger(_warn_heavy_logger).setLevel(logging.ERROR)

OUTPUT_PATH = "reports/backtest_result_latest.json"
TRADES_PATH = "reports/backtest_trades_latest.csv"
TRADE_COLUMNS = [
    "symbol", "direction", "entry_date", "exit_date", "holding_days",
    "entry_price", "exit_price", "initial_quantity", "initial_stop", "target1", "target2",
    "risk_per_share", "exit_category", "exit_reason", "gross_pnl", "costs", "realized_pnl",
    "realized_pnl_percent", "r_multiple", "mfe_r", "mae_r",
    "max_profit_percent", "max_drawdown_percent",
]
UNIVERSE_SIZES = {"default": 0, "top50": 50, "top100": 100}

# Default representative sample spanning multiple sectors — kept small
# enough for a reasonable multi-year fetch runtime. Override with
# --symbols for a larger/different test set.
DEFAULT_SYMBOLS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "MARUTI.NS", "ITC.NS",
    "SUNPHARMA.NS", "LT.NS", "NTPC.NS", "TATASTEEL.NS", "DLF.NS", "BHARTIARTL.NS",
]


def _load_watchlist_symbols() -> list[str] | None:
    """Optionally pull the full production watchlist from the most
    recent Daily Scan report, if available, instead of the small
    default sample."""
    report_path = Path("reports/full_report.csv")
    if not report_path.exists():
        return None
    symbols = []
    seen = set()
    with open(report_path, newline="") as f:
        for row in csv.DictReader(f):
            symbol = row.get("Stock") or row.get("Symbol")
            if symbol and symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
    return symbols or None


def top_liquid_symbols(n: int, watchlist: list[str] | None = None, history: dict | None = None) -> list[str]:
    """The n most liquid watchlist symbols by average NSE turnover (same
    ranking the nightly scan uses). Only symbols WITH turnover history are
    eligible. NOTE: ranked by TODAY's liquidity, so a mild survivorship
    bias towards names that are big now."""
    from data import liquidity_history
    from data.watchlist import WatchlistManager
    from scripts.generate_full_report import order_by_liquidity

    if watchlist is None:
        watchlist = WatchlistManager("storage/watchlist/nifty500.json").load()
    if history is None:
        history = liquidity_history.load_history()
    eligible = [s for s in watchlist if (history or {}).get(s.split(".")[0])]
    return order_by_liquidity(eligible, history)[:n]


def write_trades_csv(trades: list[dict], path: str = TRADES_PATH) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow({k: t.get(k) for k in TRADE_COLUMNS})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="2y", choices=["3mo", "6mo", "1y", "2y", "5y", "10y", "max"])
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol list (default: representative 11-sector sample)")
    parser.add_argument("--initial-capital", type=float, default=500000.0)
    parser.add_argument(
        "--realistic-costs", action="store_true",
        help="Apply the same Indian delivery cost model as live paper trading (config.py COST_*).",
    )
    parser.add_argument(
        "--cost-per-side-pct", type=float, default=0.0,
        help="Transaction cost per fill in percent (e.g. 0.12 = 0.12%% each side). Default 0.",
    )
    parser.add_argument(
        "--universe", default="default", choices=sorted(UNIVERSE_SIZES),
        help="default = 11-stock sample; topN = N most liquid watchlist symbols. Ignored with --symbols.",
    )
    parser.add_argument(
        "--signal-path", default="live", choices=["live", "universe"],
        help="live = same per-symbol scan as the production nightly scan (default).",
    )
    # 2026-10-07 experiment switches — all off by default (= live rules).
    parser.add_argument("--buy-only", action="store_true", help="Skip every SELL candidate (H14).")
    parser.add_argument(
        "--max-new-entries-per-day", type=int, default=0,
        help="Open at most N new positions per morning, best-ranked first (0 = no cap).",
    )
    parser.add_argument(
        "--min-target-to-cost", type=float, default=0.0,
        help="Skip entries whose move to target2 is < X times the round-trip cost (0 = off; needs --realistic-costs).",
    )
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif UNIVERSE_SIZES[args.universe]:
        symbols = top_liquid_symbols(UNIVERSE_SIZES[args.universe])
        if not symbols:
            print("No liquidity history available — falling back to the default 11-symbol sample.")
            symbols = DEFAULT_SYMBOLS
    else:
        symbols = DEFAULT_SYMBOLS

    print(f"Fetching {args.period} of historical data for {len(symbols)} symbol(s)...")
    historical_data = {}
    fundamentals = {}
    # ValidationEngine requires len(dataframe) >= 250 rows at every
    # simulation step (decision/validation_engine.py). Fetching EXACTLY
    # the requested period (e.g. "1y" ≈ 249 rows) means the dataset
    # never reaches 250 even at its own last row — every single
    # simulated day fails validation, producing zero trades regardless
    # of period length (confirmed via a real backtest run: 625 genuine
    # BUY/SELL signals, 100% rejected on "Insufficient historical
    # candles"). Fetch a buffer of extra calendar days beyond the
    # requested period, matching the same fix already applied in
    # data/market_data.py for live scanning.
    PERIOD_BUFFER_DAYS = {
        "3mo": 90, "6mo": 90, "1y": 90, "2y": 90, "5y": 120, "10y": 150, "max": 0,
    }
    for symbol in symbols:
        try:
            if args.period == "max":
                df = yf.download(symbol, period="max", progress=False, auto_adjust=False)
            else:
                end = datetime.now() + timedelta(days=1)  # yfinance end= is exclusive
                requested_days = {
                    "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "10y": 3650,
                }[args.period]
                start = end - timedelta(days=requested_days + PERIOD_BUFFER_DAYS[args.period])
                df = yf.download(
                    symbol, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
                    progress=False, auto_adjust=False,
                )
            if df.empty:
                logger.warning("No historical data for %s — skipping.", symbol)
                continue
            df = df.reset_index()
            df.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df.columns]
            if "date" in df.columns:
                df = df.rename(columns={"date": "timestamp"})
            historical_data[symbol] = df
            logger.info("Fetched %d rows for %s", len(df), symbol)
        except Exception as exc:
            logger.warning("Historical fetch failed for %s: %s", symbol, exc)
            continue
        try:
            info = yf.Ticker(symbol).info
            # CONFIRMED ROOT CAUSE of a backtest producing zero BUY
            # trades across an entire year: raw yf.Ticker().info uses
            # camelCase keys (trailingPE, returnOnEquity, debtToEquity,
            # ...) but buy_fundamental_score()/sell_fundamental_score()
            # expect snake_case keys (pe, roe, debt_to_equity, ...).
            # Passed through unmapped, every key silently misses,
            # falling back to constant defaults — computing the exact
            # same fundamental score (22.0 BUY-side / 78.0 SELL-side)
            # for every symbol, every single day, regardless of the
            # real company data. This mapping is the same one already
            # used by live scanning (data/fundamental_data.py) — reused
            # here rather than duplicated.
            fundamentals[symbol] = normalize_fundamentals(info, symbol)
        except Exception:
            pass

    if not historical_data:
        print("No historical data could be fetched for any symbol — aborting.")
        return

    engine = BacktestEngine()
    result = engine.run(
        historical_data=historical_data,
        fundamentals=fundamentals,
        initial_capital=args.initial_capital,
        cost_pct_per_side=args.cost_per_side_pct,
        cost_model=CostModel.from_config() if args.realistic_costs else None,
        signal_path=args.signal_path,
        buy_only=args.buy_only,
        max_new_entries_per_day=args.max_new_entries_per_day,
        min_target_to_cost=args.min_target_to_cost,
    )

    report_text = result.report()
    print(report_text)

    result.metrics["run_config"] = {
        "period": args.period,
        "universe": "custom" if args.symbols else args.universe,
        "symbols": len(historical_data),
        "signal_path": args.signal_path,
        "realistic_costs": bool(args.realistic_costs),
        "cost_per_side_pct": args.cost_per_side_pct,
        "buy_only": bool(args.buy_only),
        "max_new_entries_per_day": args.max_new_entries_per_day,
        "min_target_to_cost": args.min_target_to_cost,
    }
    Path("reports").mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result.metrics, f, indent=2, default=str)
    logger.info("Backtest result written to %s", OUTPUT_PATH)
    write_trades_csv(result.closed_trades)
    logger.info("%d closed trades written to %s", len(result.closed_trades), TRADES_PATH)

    notify(
        event_type="backtest_result",
        message=(
            f"📈 Backtest Result ({args.period}, {len(historical_data)} symbols, "
            f"universe={args.universe if not args.symbols else 'custom'}, signal={args.signal_path}, "
            f"buy_only={args.buy_only}, max_entries/day={args.max_new_entries_per_day or 'off'}, "
            f"min_target_to_cost={args.min_target_to_cost or 'off'})\n\n{report_text}"
        ),
        dedup_key=f"backtest_result::{time.strftime('%Y-%m-%d %H:%M:%S')}",
    )


if __name__ == "__main__":
    main()
