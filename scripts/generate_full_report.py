"""
FULL REPORT SCAN SCRIPT

Same idea as daily_scan.py, but writes the full 72-column report format
(TradeID, Sector, EMA20/50/200, RSI, MACD, ADX, ATR, StopLoss/Targets,
BUY/SELL score-probability-confidence breakdowns, etc.) instead of the
lightweight summary CSV.

Usage:
    python scripts/generate_full_report.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify, SEVERITY_HIGH, SEVERITY_MEDIUM  # noqa: E402
from core.rejection_classifier import classify_tier4_block  # noqa: E402
from core.trading_calendar import is_trading_day, now_ist, skip_reason  # noqa: E402
from data import bhavcopy_status_log  # noqa: E402
from data.watchlist import WatchlistManager  # noqa: E402
from execution.scanner import MarketScanner  # noqa: E402
from market.volatility import fetch_india_vix  # noqa: E402
from storage.trades.trade_store import TradeStore  # noqa: E402

logger = get_logger(__name__)

WATCHLIST = WatchlistManager("storage/watchlist/nifty500.json").load()

# Exact column order the person asked for.
FIELDNAMES = [
    "TradeID", "Date", "Stock", "Sector", "Industry", "Signal", "Reason",
    "Confidence", "EntryPrice", "CurrentPrice", "Highest", "Lowest",
    "EMA20", "EMA50", "EMA200", "RSI", "MACD", "ADX", "ATR", "VolumeRatio",
    "RelativeStrength", "MomentumIndicators", "VolatilityIndicator",
    "VolumeIndicators", "BreakoutIndicators", "IchimokuIndicators",
    "PatternIndicators", "Breakout", "Pullback", "score", "probability",
    "confidence", "ranking", "SELL score 00.00", "SELL Confidence 00.00",
    "SELL Passed 0/0", "SELL  probability  %", "BUT score 00.00",
    "BUT Confidence 00.00", "BUT Passed 0/0", "BUT  probability  %",
    "portfolio_allowed", "latest_close", "market_regime",
    "Decision=NO_TRADE/TRADE", "Grade=ACCEPT/REJECT", "Rank=0.00",
    "Confidence=0.00", "PositionSize", "PositionRULE", "StopLoss",
    "Target1", "Target2", "RiskReward", "ExpectedHoldDays", "HoldingDays",
    "Return", "MaxProfit", "MaxDrawdown", "TechnicalScore",
    "FundamentalScore", "NewsScore", "OverallScore", "Status",
    "ExitReason", "ExitDate", "AIComment", "AIVersion", "ANALYSIS REPORT",
    "LEARNIG", "OPTIMER", "BACKTESET",
    # Explainability (audit requirement) — full tier breakdown for both
    # engines, present for every outcome (BUY/SELL/NO_TRADE).
    "BuyTier1Passed", "BuyTier1Detail", "BuyTechnicalChecks", "BuyTier2Score", "BuyTier3Score",
    "BuyOverallScore", "BuyThreshold",
    "SellTier1Passed", "SellTier1Detail", "SellTechnicalChecks", "SellTier2Score", "SellTier3Score",
    "SellOverallScore", "SellThreshold",
    "Tier4Block",
]

AI_VERSION = "v1.0"


def latest_trade_by_symbol(trade_store: TradeStore) -> dict:
    """Most recent journal record per symbol, keyed by symbol.
    Used to fill lifecycle fields (Status/Return/ExitDate/HoldingDays) for
    symbols that actually have an open or closed trade on record.
    """
    latest: dict[str, dict] = {}
    for t in trade_store.get_all_trades():
        sym = t.get("symbol")
        if not sym:
            continue
        prev = latest.get(sym)
        if prev is None or float(t.get("timestamp", 0)) > float(prev.get("timestamp", 0)):
            latest[sym] = t
    return latest


def build_row(trade_id: int, r, trade: dict | None = None) -> dict:
    """Map one ScanResult (with the enriched diagnostics from scanner.py)
    into a row matching the full report schema. `trade` is this symbol's
    latest journal record from trades_master.csv, if one exists — used to
    fill lifecycle fields (Status/Return/ExitDate/HoldingDays)."""
    d = r.diagnostics
    trade = trade or {}

    return {
        "TradeID": trade_id,
        "Date": date.today().isoformat(),
        "Stock": r.symbol,
        "Sector": d.get("sector") or "",
        "Industry": d.get("industry") or "",
        "Signal": r.action,
        "Reason": d.get("decision_reasons", "")[:500],  # keep rows CSV-friendly
        "Confidence": r.confidence,
        "EntryPrice": d.get("latest_close"),
        "CurrentPrice": d.get("latest_close"),
        "Highest": d.get("highest"),
        "Lowest": d.get("lowest"),
        "EMA20": d.get("ema_20"),
        "EMA50": d.get("ema_50"),
        "EMA200": d.get("ema_200"),
        "RSI": d.get("rsi_14"),
        "MACD": d.get("macd"),
        "ADX": d.get("adx_14"),
        "ATR": d.get("atr_14"),
        "VolumeRatio": d.get("volume_ratio"),
        # Column header "RelativeStrength" kept as-is (locked report
        # schema, see FIELDNAMES comment above) — the underlying value is
        # price-vs-its-own-20D-mean, NOT vs a benchmark; see the NOTE at
        # features/indicators/breakout.py's price_vs_20d_mean.
        "RelativeStrength": d.get("price_vs_20d_mean"),
        "MomentumIndicators": f"RSI:{d.get('rsi_14')} STOCH:{d.get('stoch_k')} ADX:{d.get('adx_14')}",
        "VolatilityIndicator": f"ATR:{d.get('atr_14')}",
        "VolumeIndicators": f"CMF:{d.get('cmf_20')} MFI:{d.get('mfi_14')} VolRatio:{d.get('volume_ratio')}",
        "BreakoutIndicators": "YES" if d.get("is_breakout") else "NO",
        "IchimokuIndicators": d.get("cloud_trend", ""),
        "PatternIndicators": (
            "BULLISH_ENGULFING" if d.get("bullish_engulfing")
            else "BEARISH_ENGULFING" if d.get("bearish_engulfing")
            else "NONE"
        ),
        "Breakout": "YES" if d.get("is_breakout") else "NO",
        "Pullback": "YES" if d.get("is_pullback") else "NO",
        "score": r.score,
        "probability": r.probability,
        "confidence": r.confidence,
        "ranking": r.ranking,
        "SELL score 00.00": d.get("sell_score"),
        "SELL Confidence 00.00": d.get("sell_decision_confidence"),
        "SELL Passed 0/0": f"{d.get('sell_checks_passed', 0)} of {d.get('sell_checks_total', 0)}",
        "SELL  probability  %": d.get("sell_probability"),
        "BUT score 00.00": d.get("buy_score"),
        "BUT Confidence 00.00": d.get("buy_decision_confidence"),
        "BUT Passed 0/0": f"{d.get('buy_checks_passed', 0)} of {d.get('buy_checks_total', 0)}",
        "BUT  probability  %": d.get("buy_probability"),
        "portfolio_allowed": r.portfolio_allowed,
        "latest_close": d.get("latest_close"),
        "market_regime": d.get("market_regime"),
        "Decision=NO_TRADE/TRADE": d.get("decision"),
        "Grade=ACCEPT/REJECT": "ACCEPT" if r.portfolio_allowed else "REJECT",
        "Rank=0.00": r.ranking,
        "Confidence=0.00": r.confidence,
        "PositionSize": d.get("quantity"),
        "PositionRULE": d.get("portfolio_rule_reason"),
        "StopLoss": d.get("stop_loss"),
        "Target1": d.get("target1"),
        "Target2": d.get("target2"),
        "RiskReward": d.get("risk_reward"),
        "ExpectedHoldDays": d.get("expected_hold_days"),
        # Trade-lifecycle fields: filled in from trades_master.csv when this
        # symbol actually has an open/closed trade on record. MaxProfit and
        # MaxDrawdown come from portfolio.py's running highest/lowest price
        # tracking over the life of the position (see _track_extremes()).
        "HoldingDays": (
            round((time.time() - float(trade["timestamp"])) / 86400.0, 1)
            if trade.get("status") == "OPEN" and trade.get("timestamp")
            else ""
        ),
        "Return": trade.get("realized_pnl_percent", ""),
        "MaxProfit": trade.get("max_profit_percent", ""),
        "MaxDrawdown": trade.get("max_drawdown_percent", ""),
        "TechnicalScore": d.get("buy_technical_score") if r.action == "BUY" else d.get("sell_technical_score"),
        "FundamentalScore": d.get("buy_fundamental_score"),
        "NewsScore": d.get("buy_news_score"),
        "OverallScore": r.score,
        "Status": trade.get("status", "WATCH" if r.action in ("BUY", "SELL") else ""),
        "ExitReason": trade.get("reasons", "") if trade.get("status") == "CLOSED" else "",
        "ExitDate": (
            date.fromtimestamp(float(trade["timestamp"])).isoformat()
            if trade.get("status") == "CLOSED" and trade.get("timestamp")
            else ""
        ),
        "AIComment": (d.get("decision_reasons", "").split(" | ")[-1] if d.get("decision_reasons") else ""),
        "AIVersion": AI_VERSION,
        "ANALYSIS REPORT": "",
        "LEARNIG": "",
        "OPTIMER": "",
        "BACKTESET": "",
        # Explainability
        "BuyTier1Passed": d.get("buy_tier1_passed"),
        "BuyTier1Detail": "; ".join(
            f"{k}={v}" for k, v in (d.get("buy_tier1_checks") or {}).items()
        ),
        # Full per-rule technical checklist (all ~39 rules, not just the
        # smaller Tier1 gate above) — JSON so it round-trips cleanly for
        # the Learning Engine's rule-level correlation analysis.
        "BuyTechnicalChecks": json.dumps(d.get("buy_technical_checks") or {}, default=str),
        "BuyTier2Score": d.get("buy_tier2_score"),
        "BuyTier3Score": d.get("buy_tier3_score"),
        "BuyOverallScore": d.get("buy_overall_score"),
        "BuyThreshold": d.get("buy_qualify_threshold"),
        "SellTier1Passed": d.get("sell_tier1_passed"),
        "SellTier1Detail": "; ".join(
            f"{k}={v}" for k, v in (d.get("sell_tier1_checks") or {}).items()
        ),
        "SellTechnicalChecks": json.dumps(d.get("sell_technical_checks") or {}, default=str),
        "SellTier2Score": d.get("sell_tier2_score"),
        "SellTier3Score": d.get("sell_tier3_score"),
        "SellOverallScore": d.get("sell_overall_score"),
        "SellThreshold": d.get("sell_qualify_threshold"),
        "Tier4Block": (
            d.get("portfolio_rule_reason")
            if d.get("portfolio_rule_reason") not in (None, "OK")
            else (
                "Risk grade: " + str(d.get("risk_grade")) if not d.get("risk_safe", True)
                else _score_threshold_reason(d, r.action)
            )
        ),
    }


def _score_threshold_reason(d: dict, action: str) -> str:
    """Tier4Block previously had NO way to record the single most common
    NO_TRADE cause: the candidate passed Tier1/portfolio/risk cleanly,
    but its overall score simply didn't clear the qualifying threshold.
    Without this, those rows had an empty Tier4Block and fell into an
    unhelpful "Other" bucket with no text to explain why. This directly
    checks the SAME overall-score-vs-threshold values already computed
    and stored in BuyOverallScore/BuyThreshold (or Sell-equivalent)."""
    score_key = "buy_overall_score" if action != "SELL" else "sell_overall_score"
    threshold_key = "buy_qualify_threshold" if action != "SELL" else "sell_qualify_threshold"
    score = d.get(score_key)
    threshold = d.get(threshold_key)
    if score is None or threshold is None:
        return ""
    try:
        if float(score) < float(threshold):
            return f"Decision engine rejected trade: score {round(float(score), 1)} below threshold {round(float(threshold), 1)}."
    except (TypeError, ValueError):
        pass
    return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the NSE-trading-day check (testing only — e.g. to verify a "
        "fix on a market holiday). Normal scheduled runs never pass this.",
    )
    args = parser.parse_args()

    today_date = date.today()
    ist_now = now_ist()

    if not args.force and not is_trading_day(today_date):
        reason = skip_reason(today_date) or "Non-trading day"
        logger.info("Not an NSE trading day — exiting before any scan begins.")
        print("Not an NSE trading day (weekend or holiday). No scan performed.")
        notify(
            event_type="daily_scan_skipped",
            message=(
                f"⏸️ Daily Scan Skipped\n"
                f"Reason: {reason}\n"
                f"No market scan was executed."
            ),
            dedup_key=f"scan_skipped::{today_date.isoformat()}::{ist_now.strftime('%H:%M:%S.%f')}",
        )
        return

    total_symbols = len(WATCHLIST)
    notify(
        event_type="daily_scan_started",
        message=(
            f"🚀 Daily Scan Started\n"
            f"Date: {today_date.isoformat()}\n"
            f"Time (IST): {ist_now.strftime('%H:%M:%S')}\n"
            f"Status: Production Scanner Started\n"
            f"The scan is now analyzing today's market ({total_symbols} symbols)."
        ),
        dedup_key=f"scan_started::{today_date.isoformat()}::{ist_now.strftime('%H:%M:%S.%f')}",
    )

    scanner = MarketScanner()
    trade_lookup = latest_trade_by_symbol(TradeStore())
    portfolio = {
        "equity": 100000.0,
        "total_capital": 100000.0,
        "total_pnl": 0.0,
        "exposure": 0.0,
        "available_capital": 100000.0,
        "open_positions": {},
    }
    broker_status = {"status": "ONLINE", "mode": "SCAN", "connected": True, "order_allowed": True, "available_margin": 100000.0}
    # "vix" used to be entirely absent from this dict, so
    # risk_manager.py's market.get("vix", 20.0) always fell through to
    # its hardcoded default — meaning the vix >= 30 / vix >= 35 risk-off
    # checks could never fire regardless of real market conditions.
    # fetch_india_vix() pulls a live reading once per scan run (falls
    # back to 20.0 itself, with a logged warning, if the fetch fails).
    market_state = {
        "max_trade_candidates": 20,
        "max_watchlist": 50,
        "market_open": True,
        "holiday": False,
        "vix": fetch_india_vix(),
    }

    out_path = "reports/full_report.csv"
    Path("reports").mkdir(exist_ok=True)

    # TradeID must stay unique across runs since we're appending, not
    # overwriting — start counting from how many rows already exist.
    next_id = 1
    if Path(out_path).exists():
        with open(out_path, newline="") as f:
            next_id = sum(1 for _ in csv.DictReader(f)) + 1

    total = len(WATCHLIST)
    rows = []
    pending_candidates = []
    for i, symbol in enumerate(WATCHLIST, start=0):
        logger.info("[%d/%d] Full report scan: %s", i + 1, total, symbol)
        r = scanner.scan_symbol(
            symbol=symbol,
            portfolio=portfolio,
            broker_status=broker_status,
            market_state=market_state,
        )
        if r.action == "ERROR":
            logger.warning("Skipping %s from report: %s", symbol, r.diagnostics.get("error"))
            continue
        rows.append(build_row(next_id + i, r, trade_lookup.get(symbol)))
        if r.action in ("BUY", "SELL") and r.portfolio_allowed:
            pending_candidates.append(r)

    # BHAVCOPY FETCH STATUS — user-requested: know on which day
    # delivery%/liquidity data was NOT counted, plus a trailing few-day
    # audit trail. scanner._delivery_data/_delivery_data_as_of are set
    # once per run (lazy-fetched on the first symbol, see
    # execution/scanner.py's _get_delivery_data()) and reflect exactly
    # what THIS run actually got — read them here, after the scan loop,
    # rather than re-fetching or guessing.
    if total > 0:
        symbols_matched = len(scanner._delivery_data or {})
        bhavcopy_as_of = scanner._delivery_data_as_of
        if bhavcopy_as_of is None:
            bhavcopy_status = bhavcopy_status_log.STATUS_FAILED
        elif bhavcopy_as_of != today_date:
            bhavcopy_status = bhavcopy_status_log.STATUS_STALE
        else:
            bhavcopy_status = bhavcopy_status_log.STATUS_OK

        status_log = bhavcopy_status_log.record_status(
            scan_date=today_date,
            status=bhavcopy_status,
            as_of=bhavcopy_as_of,
            symbols_matched=symbols_matched,
        )

        # Only notify when something's actually wrong — a healthy day
        # stays silent, same as every other notify() call in this file
        # (e.g. "daily_scan_skipped" only fires when actually skipped).
        if bhavcopy_status != bhavcopy_status_log.STATUS_OK:
            recent = bhavcopy_status_log.recent_entries(status_log, days=4)
            status_icon = {
                bhavcopy_status_log.STATUS_OK: "✅",
                bhavcopy_status_log.STATUS_STALE: "🟡",
                bhavcopy_status_log.STATUS_FAILED: "❌",
            }
            trail_lines = [
                f"{status_icon.get(entry['status'], '❓')} {d}: {entry['status']} "
                f"(data as of {entry.get('as_of') or '—'}, {entry['symbols_matched']} symbols matched)"
                for d, entry in recent
            ]

            if bhavcopy_status == bhavcopy_status_log.STATUS_FAILED:
                header = (
                    "❌ Bhavcopy Fetch Failed — No Delivery%/Liquidity Data Today\n"
                    f"Date: {today_date.isoformat()}\n"
                    "NSE bhavcopy could not be fetched (live fetch + cache both "
                    "failed). Delivery% and liquidity (trade-size/Amihud) scoring "
                    "fell back to volume-only for EVERY symbol in today's scan — "
                    "not a crash, just less-informed scoring for today."
                )
                severity = SEVERITY_HIGH
            else:
                header = (
                    "🟡 Bhavcopy Data Stale — Using an Earlier Trading Day\n"
                    f"Date: {today_date.isoformat()}\n"
                    f"Today's bhavcopy wasn't published yet — used "
                    f"{bhavcopy_as_of.isoformat()}'s data instead for "
                    "delivery%/liquidity scoring."
                )
                severity = SEVERITY_MEDIUM

            notify(
                event_type="bhavcopy_status_warning",
                message=header + "\n\nLast 4 scan days:\n" + "\n".join(trail_lines),
                severity=severity,
                dedup_key=f"bhavcopy_status::{today_date.isoformat()}",
            )

    # APPEND mode: this is ONE running file that accumulates a full history
    # (filter by the "Date" column to see any day/month) rather than being
    # overwritten each run. Write the header only the first time the file
    # is created.
    #
    # CRITICAL: the header is written ONCE, ever. If FIELDNAMES has grown
    # since then (adding new columns, as happens whenever a new diagnostic
    # field is introduced), every row written after that point gets
    # POSITIONALLY MISALIGNED when read back with csv.DictReader (which
    # uses the file's original, shorter header) — a later column's VALUE
    # silently lands under an EARLIER column's NAME. This was the actual
    # cause of raw numeric scores appearing where rejection-reason TEXT
    # was expected. Detect a mismatch and rotate to a fresh file (with a
    # header matching the CURRENT FIELDNAMES) instead of silently
    # continuing to misalign data.
    file_exists = Path(out_path).exists()
    header_matches = True
    if file_exists:
        with open(out_path, newline="") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != FIELDNAMES:
            header_matches = False
            archive_path = out_path.replace(
                ".csv", f"_pre_{date.today().isoformat()}_schema_change.csv"
            )
            Path(out_path).rename(archive_path)
            logger.warning(
                "full_report.csv's header no longer matches the current "
                "FIELDNAMES (columns were added/changed since the header "
                "was written) — archived the old file to %s and starting "
                "a fresh one with the correct header, to avoid silently "
                "misaligned reads going forward.", archive_path,
            )

    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists or not header_matches:
            writer.writeheader()
        writer.writerows(rows)

    logger.info("Wrote %d rows to %s", len(rows), out_path)
    print(f"\nWrote {len(rows)} rows to {out_path}")

    buy_count = sum(1 for r in rows if r["Signal"] == "BUY")
    sell_count = sum(1 for r in rows if r["Signal"] == "SELL")
    no_trade_count = sum(1 for r in rows if r["Signal"] == "NO_TRADE")

    # SIDEWAYS is already computed by MarketRegimeEngine (market_regime
    # column) — just counting an existing classification, not a new
    # detector. NOTE: this is a market-regime sub-classification WITHIN
    # NO_TRADE (and occasionally within BUY/SELL), NOT a 4th mutually
    # exclusive bucket alongside BUY/SELL/NO_TRADE — worded below to make
    # that overlap explicit rather than implying the counts are additive.
    sideways_count = sum(1 for r in rows if r.get("market_regime") == "SIDEWAYS")

    # Rejection Summary — built ENTIRELY from fields already computed
    # and stored in the report (BuyTier1Passed, Tier4Block). Uses the
    # SAME shared classifier as Analysis Engine's Rejection Funnel —
    # no duplicate categorization logic.
    no_trade_rows = [r for r in rows if r["Signal"] == "NO_TRADE"]
    trend_filter_count = sum(1 for r in no_trade_rows if r.get("BuyTier1Passed") == "False")
    risk_count = liquidity_count = portfolio_rules_count = score_threshold_count = 0
    for r in no_trade_rows:
        category = classify_tier4_block(r.get("Tier4Block"))
        if category == "risk":
            risk_count += 1
        elif category == "liquidity":
            liquidity_count += 1
        elif category == "portfolio":
            portfolio_rules_count += 1
        elif category == "score_threshold":
            score_threshold_count += 1

    summary_lines = [
        f"Daily Scan completed — {len(rows)} symbols scanned.",
        f"BUY: {buy_count} | SELL: {sell_count} | NO_TRADE: {no_trade_count}"
        + (f" (of which {sideways_count} in SIDEWAYS regime)" if sideways_count else ""),
    ]
    if trend_filter_count or risk_count or liquidity_count or portfolio_rules_count or score_threshold_count:
        summary_lines.append("")
        summary_lines.append("Rejection Summary (NO_TRADE breakdown)")
        if trend_filter_count:
            summary_lines.append(f"Trend Filter: {trend_filter_count}")
        if score_threshold_count:
            summary_lines.append(f"Score Threshold: {score_threshold_count}")
        if risk_count:
            summary_lines.append(f"Risk: {risk_count}")
        if liquidity_count:
            summary_lines.append(f"Liquidity: {liquidity_count}")
        if portfolio_rules_count:
            summary_lines.append(f"Portfolio Rules: {portfolio_rules_count}")

    notify(
        event_type="daily_scan_completed",
        message="\n".join(summary_lines),
        dedup_key=f"scan_completed::{time.strftime('%Y-%m-%d')}::{now_ist().strftime('%H:%M:%S.%f')}",
    )

    # ==========================================================
    # PENDING ORDERS FOR MORNING EXECUTION
    # ==========================================================
    # Writes the top max_trade_candidates (by ranking) to
    # candidates_order.json, using ONLY fields already computed during
    # tonight's scan (no new analysis). A separate morning-executor
    # script (run at market open) reads this file, checks the actual
    # opening price against the stop/target boundaries already set
    # here, and decides execute/skip — see that script for details.
    scan_timestamp = ist_now.isoformat()
    pending_candidates.sort(key=lambda r: r.ranking, reverse=True)
    top_candidates = pending_candidates[: market_state["max_trade_candidates"]]
    pending_orders = []
    for r in top_candidates:
        d = r.diagnostics
        pending_orders.append({
            "symbol": r.symbol,
            "direction": r.action,
            "prev_close": d.get("latest_close"),
            "atr_14": d.get("atr_14"),
            "atr_percent": d.get("atr_percent"),
            "stop_loss": d.get("stop_loss"),
            "target1": d.get("target1"),
            "target2": d.get("target2"),
            "overall_score": round(r.score, 2),
            "ranking": round(r.ranking, 2),
            "sector": d.get("sector"),
            "scan_date": today_date.isoformat(),
        })
    pending_path = Path("reports/candidates_order.json")
    with open(pending_path, "w") as f:
        json.dump({
            "scan_date": today_date.isoformat(),
            "scan_timestamp": scan_timestamp,
            "candidates": pending_orders,
        }, f, indent=2)
    logger.info("Wrote %d pending candidates to %s", len(pending_orders), pending_path)


if __name__ == "__main__":
    main()
