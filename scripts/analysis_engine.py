"""
MODULE 1 — ANALYSIS ENGINE (CLI)

Thin command-line wrapper around analytics.analysis_engine.AnalysisEngine
(the single canonical implementation — no logic is duplicated here).

Roadmap coverage (Phase 2, Module 1):
    [x] BUY/SELL statistics — signal_counts
    [x] NO_TRADE analysis   — no_trade_stats
    [x] Rule contribution   — tier_contribution
    [x] Tier analysis       — tier1_pass_rate, tier_contribution
    [x] Sector statistics   — sector_stats, top_buy_sectors, weakest_buy_sectors
    [x] Regime statistics   — regime_stats, regime_percentages
    [x] Daily summaries     — daily_summary
    [x] Rejection analysis  — top_rejection_reasons, rejection_funnel

Reads reports/full_report.csv and produces a complete statistical
breakdown: BUY/SELL/NO_TRADE counts, Tier-1/2/3 contribution, Tier-4
rejection reasons, sector-wise stats, regime stats, top rejection reasons.

This module only OBSERVES and REPORTS — it never changes strategy code
or production settings.

Usage:
    python scripts/analysis_engine.py [--csv path]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402
from analytics.analysis_engine import AnalysisEngine  # noqa: E402

logger = get_logger(__name__)

OUTPUT_PATH = "reports/analysis_summary.json"
HISTORY_PATH = "reports/analysis_history.jsonl"


def _load_history() -> list[dict]:
    path = Path(HISTORY_PATH)
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _append_history(entry: dict) -> None:
    Path(HISTORY_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="reports/full_report.csv")
    args = parser.parse_args()

    engine = AnalysisEngine(report_path=args.csv)
    n = engine.load()
    print(f"Loaded {n} rows from {args.csv}")

    engine.print_report()

    result = engine.analyze()
    Path("reports").mkdir(exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2, default=str)
    logger.info("Analysis summary written to %s", OUTPUT_PATH)

    signal_counts = result.get("signal_counts", {})
    sell_gap = result.get("sell_signal_vs_opened", {})
    regime_pct = result.get("regime_percentages", {})
    top_sectors = result.get("top_buy_sectors", [])
    weak_sectors = result.get("weakest_buy_sectors", [])
    funnel = result.get("rejection_funnel", {})
    execution = result.get("execution_summary", {})
    tier_contrib = result.get("tier_contribution", {})

    total_scans = result.get("total_scans", 0)
    buy_n = signal_counts.get("BUY", 0)
    sell_n = signal_counts.get("SELL", 0)
    no_trade_n = signal_counts.get("NO_TRADE", 0)
    tier1 = result.get("tier1_pass_rate", {})

    # ---------------- Historical comparison (load BEFORE appending today) ----------------
    history = _load_history()
    today_str = time.strftime("%Y-%m-%d")
    prior_entries = [h for h in history if h.get("date") != today_str]
    yesterday_entry = prior_entries[-1] if prior_entries else None
    last_7_entries = prior_entries[-7:]

    _append_history({
        "date": today_str, "total_scans": total_scans, "buy": buy_n, "sell": sell_n,
        "no_trade": no_trade_n, "tier1_buy_pct": tier1.get("buy"), "tier1_sell_pct": tier1.get("sell"),
    })

    def _sector_label(sector: str) -> str:
        return "Unknown sector" if sector == "UNKNOWN" else sector

    def _health_emoji(level: str) -> str:
        return {"healthy": "🟢", "watch": "🟡", "investigate": "🔴"}.get(level, "🟡")

    # ---------------- 1. Header ----------------
    message_lines = [
        "📊 Daily Scan Summary",
        "",
        f"Universe Scanned : {total_scans}",
        f"BUY Signals      : {buy_n}",
        f"SELL Signals     : {sell_n}",
        f"NO TRADE         : {no_trade_n}",
        "",
        "Tier-1 Screening (initial trend/volume gate)",
        f"BUY Pass Rate  : {tier1.get('buy', 0)}%",
        f"SELL Pass Rate : {tier1.get('sell', 0)}%",
    ]
    avg_7d_buy_tier1 = [e.get("tier1_buy_pct") for e in last_7_entries if e.get("tier1_buy_pct") is not None]
    if avg_7d_buy_tier1:
        avg_val = round(sum(avg_7d_buy_tier1) / len(avg_7d_buy_tier1), 1)
        direction = "above" if tier1.get("buy", 0) > avg_val else ("below" if tier1.get("buy", 0) < avg_val else "in line with")
        message_lines.append(
            f"Meaning: {direction} the {len(avg_7d_buy_tier1)}-day average ({avg_val}%) — "
            f"{tier1.get('buy', 0)}% of BUY candidates cleared the initial screen today."
        )
    else:
        message_lines.append(
            f"Meaning: {tier1.get('buy', 0)}% of BUY candidates cleared the initial screen today "
            f"(no prior-day history yet to compare against)."
        )

    if tier_contrib:
        message_lines.append("")
        message_lines.append("Rule Contribution (avg score across scanned candidates)")
        message_lines.append("(Tier2 = technical indicators score, Tier3 = fundamentals+news+market score)")

        def _tier_line(label: str, avg_val, n: int) -> str:
            if avg_val is None or n == 0:
                return f"{label}: N/A — no candidates had this score recorded (0 sample)"
            return f"{label}: {avg_val} (based on {n} candidates)"

        message_lines.append(_tier_line(
            "BUY Tier2", tier_contrib.get("buy_tier2_avg"), tier_contrib.get("buy_tier2_n", 0)
        ))
        message_lines.append(_tier_line(
            "BUY Tier3", tier_contrib.get("buy_tier3_avg"), tier_contrib.get("buy_tier3_n", 0)
        ))
        message_lines.append(_tier_line(
            "SELL Tier2", tier_contrib.get("sell_tier2_avg"), tier_contrib.get("sell_tier2_n", 0)
        ))
        message_lines.append(_tier_line(
            "SELL Tier3", tier_contrib.get("sell_tier3_avg"), tier_contrib.get("sell_tier3_n", 0)
        ))
        message_lines.append(
            "So what: a genuinely low average (not N/A) means most scanned candidates "
            "showed weak signals on that dimension today — not a bug, weight, or disabled rule."
        )

    # ---------------- 1b. Historical Comparison ----------------
    if yesterday_entry or last_7_entries:
        message_lines.append("")
        message_lines.append("Historical Comparison")
        if yesterday_entry:
            prev_universe = yesterday_entry.get("total_scans", 0)
            universe_changed = prev_universe and prev_universe != total_scans
            if universe_changed:
                message_lines.append(f"Note: Universe changed: {prev_universe} → {total_scans} — raw counts below aren't directly comparable, so % of universe is shown too.")
            for label, key in (("BUY", "buy"), ("SELL", "sell")):
                prev_val = yesterday_entry.get(key, 0)
                curr_val = buy_n if key == "buy" else sell_n
                if prev_val:
                    pct_change = round((curr_val - prev_val) / prev_val * 100, 1)
                    arrow = "↑" if pct_change > 0 else ("↓" if pct_change < 0 else "→")
                    line = f"{label}: Yesterday {prev_val} → Today {curr_val}  {arrow} {pct_change:+.1f}%"
                    if universe_changed and prev_universe and total_scans:
                        prev_rate = round(prev_val / prev_universe * 100, 1)
                        curr_rate = round(curr_val / total_scans * 100, 1)
                        line += f"  (as % of universe: {prev_rate}% → {curr_rate}%)"
                    message_lines.append(line)
                else:
                    message_lines.append(f"{label}: Yesterday {prev_val} → Today {curr_val}")
        if last_7_entries:
            avg_buy_7d = round(sum(e.get("buy", 0) for e in last_7_entries) / len(last_7_entries), 1)
            comparison = "Above Average" if buy_n > avg_buy_7d else ("Below Average" if buy_n < avg_buy_7d else "In Line")
            message_lines.append(
                f"{len(last_7_entries)}-Day Avg BUY: {avg_buy_7d} — Today: {buy_n} ({comparison})"
            )

    # ---------------- 2. Market Regime (unchanged) ----------------
    if regime_pct:
        message_lines.append("")
        message_lines.append("Market Regime")
        emoji = {"BULL": "🟢", "SIDEWAYS": "🟡", "BEAR": "🔴"}
        for regime in ("BULL", "SIDEWAYS", "BEAR"):
            if regime in regime_pct:
                label = {"BULL": "Bullish", "SIDEWAYS": "Sideways", "BEAR": "Bearish"}[regime]
                message_lines.append(f"{emoji[regime]} {label}: {regime_pct[regime]}%")

    # ---------------- 3. Sector Analysis ----------------
    if top_sectors:
        message_lines.append("")
        message_lines.append("Top BUY Sectors")
        for i, (sector, count) in enumerate(top_sectors, 1):
            if count > 0:
                message_lines.append(f"{i}. {_sector_label(sector)} ({count} BUY)")
    if weak_sectors:
        message_lines.append("")
        message_lines.append("Weakest BUY Sectors")
        for i, (sector, count) in enumerate(weak_sectors, 1):
            message_lines.append(f"{i}. {_sector_label(sector)} ({count} BUY)")
        unknown_symbols = result.get("unknown_sector_symbols", [])
        if unknown_symbols:
            message_lines.append(
                "Note: \"Unknown sector\" means yfinance didn't return a sector "
                "for that stock (data gap, not a strategy issue). Affected:"
            )
            message_lines.append("  " + ", ".join(unknown_symbols[:10]))

    # ---------------- 4. Rejection Funnel ----------------
    # Honest note: these are the categories the classifier can actually
    # tell apart (Trend/Risk/Portfolio/Liquidity/Score-Threshold/Other).
    # A Momentum/News/Fundamental-specific split isn't derivable from
    # current data without new, separate detector work — not fabricated
    # here.
    if funnel:
        message_lines.append("")
        message_lines.append("Rejection Funnel (BUY-side only)")
        message_lines.append(
            f"{funnel.get('buy_side_scanned', 0)} Scanned "
            f"({total_scans} total scanned − {sell_n} SELL, tracked separately below)"
        )
        message_lines.append(f"├── BUY Candidates: {funnel.get('buy_candidates', 0)}")
        message_lines.append(f"├── Rejected — Trend: {funnel.get('rejected_by_trend', 0)}")
        message_lines.append(f"├── Rejected — Risk (signal stage): {funnel.get('rejected_by_risk', 0)}")
        message_lines.append(f"├── Rejected — Portfolio: {funnel.get('rejected_by_portfolio', 0)}")
        message_lines.append(f"├── Rejected — Liquidity: {funnel.get('rejected_by_liquidity', 0)}")
        message_lines.append(f"├── Rejected — Score Threshold: {funnel.get('rejected_by_score_threshold', 0)}")
        message_lines.append(
            f"├── Rejected — Insufficient Historical Candles: {funnel.get('rejected_by_insufficient_history', 0)}"
        )
        other_n = funnel.get("rejected_by_other", 0)
        message_lines.append(f"├── Rejected — Other: {other_n}")
        other_breakdown = funnel.get("other_reasons_breakdown", {})
        if other_breakdown:
            for reason, count in other_breakdown.items():
                message_lines.append(f"│     • {reason}: {count}")
        message_lines.append(f"└── Executed: {funnel.get('executed', 0)}")

    # ---------------- 5. Execution Summary (redesigned) ----------------
    DAILY_CANDIDATE_LIMIT = 100  # matches execution/scanner.py's max_trade_candidates
    buy_attempted = execution.get("buy_generated", 0) if execution else 0
    if execution:
        reasons = dict(execution.get("reasons", {}))
        not_attempted = reasons.pop("Not Attempted (ranked below daily candidate limit)", 0)
        buy_attempted = execution.get("buy_generated", 0) - not_attempted
        # Whatever's left in "Other" here (after risk/portfolio/liquidity/
        # score-threshold/history are already split out) is most likely
        # a capital constraint at the ACTUAL execution moment — which
        # happens later than the Daily Scan and isn't written back to
        # Tier4Block anywhere, so this is a best-explanation relabel,
        # not a verified certainty.
        other_count = reasons.pop("Other", 0)
        if other_count:
            reasons["Insufficient Capital (most likely — see note below)"] = other_count

        executed = execution.get("buy_executed", 0)
        message_lines.append("")
        message_lines.append("BUY Execution")
        message_lines.append(f"Signals Generated     : {execution.get('buy_generated', 0)}")
        message_lines.append(f"Daily Candidate Limit : {DAILY_CANDIDATE_LIMIT}")
        message_lines.append(f"Executed              : {executed}")
        message_lines.append(f"Not Executed          : {execution.get('buy_rejected', 0)}")
        message_lines.append("Reasons")
        if not_attempted:
            message_lines.append(f"• Ranked below daily candidate limit : {not_attempted}")
        for reason, count in reasons.items():
            message_lines.append(f"• {reason} : {count}")
        if other_count:
            message_lines.append(
                "Note: \"Insufficient Capital\" here is inferred, not directly recorded — "
                "these candidates ranked within the daily limit but had no risk/portfolio/"
                "liquidity/score reason logged, and actual capital availability at execution "
                "time (hours after the scan) isn't written back anywhere the scan can see."
            )

    # ---------------- 6. SELL Summary (redesigned, same structure) ----------------
    if sell_gap.get("sell_signals", 0) > 0:
        gap_reasons = dict(sell_gap.get("gap_reasons", {}))
        sell_not_attempted = gap_reasons.pop("Not Attempted (ranked below daily candidate limit)", 0)
        sell_other_count = gap_reasons.pop("Other / Not Recorded", 0)
        if sell_other_count:
            gap_reasons["Insufficient Capital (most likely — see note below)"] = sell_other_count

        message_lines.append("")
        message_lines.append("SELL Execution")
        message_lines.append(f"Signals Generated     : {sell_gap['sell_signals']}")
        message_lines.append(f"Daily Candidate Limit : {DAILY_CANDIDATE_LIMIT}")
        message_lines.append(f"Executed              : {sell_gap['sell_trades_opened']}")
        message_lines.append(f"Not Executed          : {sell_gap.get('gap', 0)}")
        message_lines.append("Reasons")
        if sell_not_attempted:
            message_lines.append(f"• Ranked below daily candidate limit : {sell_not_attempted}")
        for reason, count in gap_reasons.items():
            message_lines.append(f"• {reason} : {count}")
        if sell_other_count:
            message_lines.append(
                "Note: \"Insufficient Capital\" here is inferred, not directly recorded — "
                "same reasoning as the BUY side above."
            )

    # ---------------- 7. AI Observation ----------------
    # Rule-based, derived directly from the numbers above — not a
    # separate LLM call, just plain-language sentences built from real
    # computed values so the report is skimmable at a glance.
    observations = []
    if regime_pct:
        dominant = max(regime_pct, key=regime_pct.get)
        dominant_label = {"BULL": "bullish", "SIDEWAYS": "mixed/sideways", "BEAR": "bearish"}[dominant]
        observations.append(f"Market regime was predominantly {dominant_label} today ({regime_pct[dominant]}%).")
    if top_sectors and top_sectors[0][1] > 0:
        observations.append(f"{_sector_label(top_sectors[0][0])} generated the most BUY opportunities ({top_sectors[0][1]}).")
    if funnel:
        biggest_reason = max(
            [
                ("Trend", funnel.get("rejected_by_trend", 0)),
                ("Risk", funnel.get("rejected_by_risk", 0)),
                ("Portfolio", funnel.get("rejected_by_portfolio", 0)),
                ("Liquidity", funnel.get("rejected_by_liquidity", 0)),
                ("Score Threshold", funnel.get("rejected_by_score_threshold", 0)),
                ("Insufficient Historical Candles", funnel.get("rejected_by_insufficient_history", 0)),
            ],
            key=lambda kv: kv[1],
        )
        if biggest_reason[1] > 0:
            observations.append(f"Most rejected candidates failed on {biggest_reason[0]} ({biggest_reason[1]}).")
    if execution and execution.get("buy_executed", 0) == 0 and execution.get("buy_generated", 0) > 0:
        if buy_attempted == 0:
            observations.append("No BUY trades reached the execution stage today — all were blocked earlier at signal/validation checks.")
        else:
            observations.append(f"{buy_attempted} BUY candidate(s) were within the daily limit but none were executed — worth reviewing capital/liquidity/risk checks.")
    if observations:
        message_lines.append("")
        message_lines.append("AI Observation")
        for obs in observations:
            message_lines.append(f"• {obs}")

    # ---------------- 8. Strategy Health ----------------
    exec_rate = (
        round(execution.get("buy_executed", 0) / execution.get("buy_generated", 1) * 100, 1)
        if execution.get("buy_generated") else 0.0
    )
    buy_opportunities_level = "healthy" if buy_n >= 50 else ("watch" if buy_n >= 15 else "investigate")
    regime_spread = max(regime_pct.values()) - min(regime_pct.values()) if len(regime_pct) > 1 else 100
    trend_level = "healthy" if regime_spread >= 40 else "watch"
    exec_level = "healthy" if exec_rate >= 5 else ("watch" if exec_rate > 0 else "investigate")

    message_lines.append("")
    message_lines.append("Strategy Health")
    message_lines.append(f"{_health_emoji(buy_opportunities_level)} BUY Opportunities : {buy_opportunities_level.title()} ({buy_n})")
    message_lines.append("   Rule: Healthy ≥50, Watch 15-49, Investigate <15")
    message_lines.append(f"{_health_emoji(trend_level)} Market Trend      : {trend_level.title()}")
    message_lines.append("   Rule: Healthy if regime spread ≥40pp (clear dominant regime)")
    message_lines.append(f"{_health_emoji(exec_level)} Execution Rate    : {exec_level.title()} ({exec_rate}%)")
    message_lines.append("   Rule: Healthy ≥5%, Watch 0-5%, Investigate at 0%")
    if exec_level == "investigate":
        message_lines.append("Recommendation")
        message_lines.append(f"Execution stage produced {buy_attempted} candidate(s) within the daily limit.")
        message_lines.append("Verify:")
        message_lines.append("• Capital allocation")
        message_lines.append("• Liquidity filter")
        message_lines.append("• Portfolio rules")
        message_lines.append("• Risk engine thresholds")

    # ---------------- 8b. Scan Efficiency ----------------
    message_lines.append("")
    message_lines.append("⭐ Scan Efficiency")
    message_lines.append(f"{total_scans} Stocks")
    if total_scans:
        message_lines.append(f"↓ {buy_n} BUY  ({round(buy_n / total_scans * 100, 1)}%)")
        message_lines.append(f"↓ {sell_n} SELL  ({round(sell_n / total_scans * 100, 1)}%)")
        message_lines.append(f"↓ {no_trade_n} NO TRADE  ({round(no_trade_n / total_scans * 100, 1)}%)")

    # ---------------- 8c. Pipeline Health ----------------
    # Explicit rules, shown so the classification isn't a black box:
    #  Trend Filter: healthy if it's genuinely discriminating (rejecting
    #    somewhere between 10-90% of candidates) — 0% means it never
    #    rejects anything (suspicious), ~100% means nothing ever passes.
    #  Execution Filter: healthy if at least one BUY actually executed;
    #    "needs investigation" if signals were generated but zero executed.
    #  Portfolio Filter: "idle" if it never had a chance to reject anything
    #    (0 portfolio-rejections) — not necessarily broken, just unused
    #    this run; "working" if it's genuinely rejecting when triggered.
    message_lines.append("")
    message_lines.append("⭐ Pipeline Health")
    buy_side_scanned = funnel.get("buy_side_scanned", 1) or 1
    trend_rejection_rate = funnel.get("rejected_by_trend", 0) / buy_side_scanned * 100
    trend_status = "Working" if 10 <= trend_rejection_rate <= 90 else "Needs Investigation"
    trend_emoji = "🟢" if trend_status == "Working" else "🔴"
    message_lines.append(f"Trend Filter       : {trend_status} {trend_emoji}")

    exec_status = "Working" if execution.get("buy_executed", 0) > 0 else (
        "Needs Investigation" if execution.get("buy_generated", 0) > 0 else "No candidates reached this stage"
    )
    exec_emoji = {"Working": "🟢", "Needs Investigation": "🔴", "No candidates reached this stage": "⚪"}[exec_status]
    message_lines.append(f"Execution Filter   : {exec_status} {exec_emoji}")

    portfolio_status = "Working" if funnel.get("rejected_by_portfolio", 0) > 0 else "No candidates reached this stage"
    portfolio_emoji = "🟢" if portfolio_status == "Working" else "⚪"
    message_lines.append(f"Portfolio Filter   : {portfolio_status} {portfolio_emoji}")

    # ---------------- 9. Numbers Consistency Check ----------------
    # Mandatory reconciliation — verifies the report's own numbers add
    # up before it's presented as trustworthy, instead of silently
    # shipping a report whose totals don't match (which is exactly what
    # was happening before this was added).
    errors = []
    if funnel:
        funnel_sum = (
            funnel.get("buy_candidates", 0) + funnel.get("rejected_by_trend", 0)
            + funnel.get("rejected_by_risk", 0) + funnel.get("rejected_by_portfolio", 0)
            + funnel.get("rejected_by_liquidity", 0) + funnel.get("rejected_by_score_threshold", 0)
            + funnel.get("rejected_by_insufficient_history", 0) + funnel.get("rejected_by_other", 0)
        )
        if funnel_sum != funnel.get("buy_side_scanned", 0):
            errors.append(f"BUY funnel: parts sum to {funnel_sum}, expected {funnel.get('buy_side_scanned', 0)}")
    if execution and execution.get("reasons"):
        reasons_sum = sum(execution["reasons"].values())
        if reasons_sum != execution.get("buy_rejected", 0):
            errors.append(f"BUY execution reasons: sum to {reasons_sum}, expected {execution.get('buy_rejected', 0)}")
    if sell_gap.get("gap_reasons"):
        sell_reasons_sum = sum(sell_gap["gap_reasons"].values())
        if sell_reasons_sum != sell_gap.get("gap", 0):
            errors.append(f"SELL reasons: sum to {sell_reasons_sum}, expected {sell_gap.get('gap', 0)}")

    message_lines.append("")
    message_lines.append("Numbers Consistency Check")
    if errors:
        message_lines.append("❌ ERROR — totals do not reconcile:")
        for e in errors:
            message_lines.append(f"  • {e}")
    else:
        message_lines.append("✓ OK — all totals reconcile.")

    notify(
        event_type="analysis_summary",
        message="\n".join(message_lines),
        dedup_key=f"analysis_summary::{time.strftime('%Y-%m-%d')}::{now_ist().strftime('%H:%M:%S.%f')}",
    )


if __name__ == "__main__":
    main()
