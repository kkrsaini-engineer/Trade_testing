"""
RUN MARKET INTELLIGENCE (daily research pass)

Independent from the trading pipeline. Reads the currently open Virtual
Portfolio positions (read-only — never modifies them), researches
news/macro context for each, and sends advisory Telegram notifications
for anything significant. Never generates a trading signal.

HOLIDAY-GATE FIX (2026-08-31): this used to skip entirely via
`is_trading_day()` — copied from the trading-pipeline scripts without
re-checking whether it fit THIS script. It doesn't: real company/macro
news does not wait for NSE to be open (weekend/holiday earnings calls,
regulatory actions, global events), and this script never places or
touches a trade either way, so gating it on the trading calendar only
created a blind spot on exactly the days someone might want a heads-up
most. The GitHub Actions cron (6x/day) already fires 365 days/year
regardless — this script now genuinely researches every time it fires,
matching that schedule's intent, instead of silently no-op'ing on
non-trading days.

Usage:
    python scripts/run_market_intelligence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.notifications import notify  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402
from market_intelligence.market_intelligence_engine import MarketIntelligenceEngine  # noqa: E402
from paper_trading.virtual_portfolio import VirtualPortfolio  # noqa: E402

logger = get_logger(__name__)


def main() -> None:
    portfolio = VirtualPortfolio()
    open_positions = [
        {
            "symbol": symbol,
            "direction": pos.direction,
            "sector": portfolio.sector_for(symbol),
        }
        for symbol, pos in portfolio.engine.state.open_positions.items()
    ]

    if not open_positions:
        logger.info("No open positions — nothing for Market Intelligence to research today.")
        print("No open positions to research.")
        notify(
            event_type="market_intelligence_summary",
            message=(
                "📊 Market Intelligence\n\n"
                "✅ Completed\n\n"
                "Open Positions: 0\n\n"
                "Nothing to research today."
            ),
            dedup_key=f"mi_summary_empty::{now_ist().strftime('%Y-%m-%d %H:%M:%S.%f')}",
        )
        return

    engine = MarketIntelligenceEngine()
    result = engine.run(open_positions)

    print(f"\n=== MARKET INTELLIGENCE — {len(open_positions)} open position(s) researched ===")
    print(f"Overall market sentiment : {result['macro']['overall_market_sentiment_score']}")
    print(f"Macro risk score         : {result['macro']['macro_risk_score']}")
    print(f"Global risk level        : {result['macro']['global_risk_level']}")
    if result["macro"]["critical_events"]:
        print(f"Critical events detected : {result['macro']['critical_events']}")
    print(f"Alerts sent              : {len(result['alerts_sent'])}")
    for msg in result["alerts_sent"]:
        print("  ---")
        print(" ", msg.replace("\n", "\n  "))


if __name__ == "__main__":
    main()
