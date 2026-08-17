"""
DIAGNOSTIC — Real, engine-computed Tier 3 (fundamentals + news + market
context) snapshot for ONE symbol.

Runs the EXACT SAME production data pipeline the live scan uses —
data/data_engine.py's DataEngine (fundamentals via yfinance .info, news
via yfinance .news) -> strategy/fundamental_scoring.py's
buy_fundamental_evaluation()/sell_fundamental_evaluation() (the real
scoring math, no reimplementation here) -> news/sentiment_engine.py's
SentimentEngine + execution/scanner.py's _signed_news_bias() (the real
news aggregation) -> market/market_regime.py's MarketRegimeEngine
(market_regime / base market_score) — and prints the RAW fundamentals
dict, RAW news items, and the derived Tier 3 scores.

Purpose: unlike price indicators (which you can eyeball on a broker
chart), Tier 3 inputs (PE/PB/ROE/..., news headlines) need to be
cross-checked against a source that actually publishes the SAME raw
numbers — e.g. Yahoo Finance's own "Statistics"/"News" pages for this
symbol (since this data comes directly from yfinance), or screener.in /
moneycontrol for the same fundamentals. This diagnostic prints the RAW
values the engine actually used, so you can verify those specific
numbers are correct BEFORE judging whether fundamental_health/
news_health/tier3_score's WEIGHTING of them is reasonable — those are
two different questions (is the input correct vs is the scoring
methodology reasonable), and only the first one is a "does this match
a real source" accuracy check the way price indicators are.

NOT reproduced here (scan-wide, not per-symbol, needs a full
MarketScanner instance with delivery/FII-DII providers wired in): the
FII/DII institutional-flow nudge and the macro-headline sector-bias
nudge on top of the base market_score. Both are the SAME adjustment
applied to every symbol on a given scan day, not something that varies
per symbol — what's printed here is the BASE, per-symbol market_score
before those two scan-wide nudges, so it may differ slightly from a
live scan's number on a day with active FII/DII or macro-headline
signal.

Usage:
    python scripts/diagnose_fundamental_snapshot.py --symbol HDFCBANK.NS
"""

from __future__ import annotations

import argparse

from data.data_engine import DataEngine
from execution.scanner import _signed_news_bias
from features.feature_engineering import FeatureEngineeringEngine
from market.market_regime import MarketRegimeEngine
from news.news_bias import news_component
from news.sentiment_engine import SentimentEngine
from strategy.fundamental_scoring import (
    buy_fundamental_evaluation,
    sell_fundamental_evaluation,
)


_FUNDAMENTAL_FIELDS = [
    ("pe", "Trailing P/E"),
    ("pb", "Price/Book"),
    ("peg", "PEG Ratio"),
    ("roe", "Return on Equity (%)"),
    ("debt_to_equity", "Debt/Equity"),
    ("earnings_growth", "Earnings Growth"),
    ("revenue_growth", "Revenue Growth"),
    ("operating_cashflow", "Operating Cashflow"),
    ("market_cap", "Market Cap"),
    ("sector", "Sector"),
    ("industry", "Industry"),
]


def _aggregate_news_score(scored_news: list[dict]) -> float | None:
    """Exact same aggregation as execution/scanner.py's scan path —
    signed per-item bias, averaged, rescaled to 0-100 around 50."""
    if not scored_news:
        return None
    avg_bias = sum(_signed_news_bias(item) for item in scored_news) / len(scored_news)
    return max(0.0, min(100.0, 50.0 + avg_bias * 50.0))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="e.g. HDFCBANK.NS")
    parser.add_argument("--news-limit", type=int, default=20)
    args = parser.parse_args()

    print(f"Fetching {args.symbol} fundamentals + news via the real DataEngine ...")
    bundle = DataEngine().fetch(symbol=args.symbol, news_limit=args.news_limit)

    print("\n" + "=" * 70)
    print("RAW FUNDAMENTALS (from yfinance .info, unmodified)")
    print("=" * 70)
    print("Cross-check these against Yahoo Finance's 'Statistics' page for")
    print(f"{args.symbol}, or screener.in / moneycontrol.\n")

    for key, label in _FUNDAMENTAL_FIELDS:
        value = bundle.fundamentals.get(key)
        shown = value if value is not None else "MISSING (not returned by yfinance for this symbol)"
        print(f"{label}: {shown}")

    buy_evidence = buy_fundamental_evaluation(bundle.fundamentals)
    sell_evidence = sell_fundamental_evaluation(bundle.fundamentals)

    print("\n" + "=" * 70)
    print("FUNDAMENTAL_HEALTH (buy_strategy.py / sell_strategy.py's real scoring)")
    print("=" * 70)
    print(f"BUY-direction score: {buy_evidence.score:.2f}/100")
    print(f"SELL-direction score: {sell_evidence.score:.2f}/100  (mirror: 100 - BUY score)")
    print(
        f"Coverage: {buy_evidence.available_metrics}/{buy_evidence.total_metrics} metrics "
        f"present ({buy_evidence.coverage:.0%}) — low coverage means this score is built "
        f"from fewer real data points, not that the score itself is 'wrong'."
    )

    print("\n" + "=" * 70)
    print(f"RAW NEWS ({len(bundle.news)} item(s) fetched, most recent first)")
    print("=" * 70)
    print(f"Cross-check headlines/dates against Yahoo Finance's News tab for {args.symbol}.\n")

    scored_news = SentimentEngine().evaluate(bundle.news)
    if not scored_news:
        print("(no news returned by yfinance for this symbol)")
    else:
        for item in scored_news:
            print(
                f"- [{item.get('sentiment')}] impact={item.get('impact_score'):.1f} "
                f"decay={item.get('decay_factor')} published={item.get('published_at')} "
                f":: {item.get('title')}"
            )

    news_score = _aggregate_news_score(scored_news)

    print("\n" + "=" * 70)
    print("NEWS_HEALTH (buy_strategy.py / sell_strategy.py's real scoring)")
    print("=" * 70)
    if news_score is None:
        print("No news -> news is NEUTRAL, does not affect the score (has_news=False).")
        news_health = None
    else:
        news_health = news_component(news_score)
        print(f"Aggregated news_score: {news_score:.2f}/100")
        print(f"news_health (as used in tier3_score): {news_health:.2f}/100")

    print("\n" + "=" * 70)
    print("MARKET CONTEXT (base, per-symbol only — see NOT-reproduced note above)")
    print("=" * 70)
    features = FeatureEngineeringEngine().generate(bundle.market)
    features = MarketRegimeEngine().evaluate(features)
    latest_regime = str(features.iloc[-1].get("market_regime", "UNKNOWN"))
    base_market_score = {"BULL": 75.0, "SIDEWAYS": 50.0, "BEAR": 25.0}.get(latest_regime, 50.0)
    print(f"market_regime: {latest_regime}  ->  base market_score: {base_market_score:.2f}/100")
    print("(production also nudges this with FII/DII flow + macro-headline sector")
    print(" bias — both scan-wide, same adjustment for every symbol that day, not")
    print(" reproduced here since they need a full MarketScanner instance.)")

    print("\n" + "=" * 70)
    print("TIER 3 SCORE (buy_strategy.py's real weighted formula, BUY direction)")
    print("=" * 70)
    fundamental_health = buy_evidence.score
    if news_health is not None:
        tier3_score = fundamental_health * 0.55 + news_health * 0.30 + base_market_score * 0.15
        print("tier3_score = fundamental_health*0.55 + news_health*0.30 + market_context*0.15")
    else:
        tier3_score = fundamental_health * (0.55 / 0.70) + base_market_score * (0.15 / 0.70)
        print(
            "tier3_score = fundamental_health*(0.55/0.70) + market_context*(0.15/0.70)"
            "  (no news -> weight redistributed)"
        )
    print(f"  = {tier3_score:.2f}/100")
    print("(uses the BASE market_score above, not the FII/DII/macro-nudged one — a")
    print(" live scan's tier3_score may differ slightly on a day with active FII/DII")
    print(" or macro-headline signal.)")

    print("\n" + "=" * 70)
    print("These are the exact fundamentals/news/market values strategy/")
    print("buy_strategy.py and strategy/sell_strategy.py's Tier 3 would see for")
    print("this symbol. Cross-check the RAW fundamentals and news items above")
    print("against an external source — the scores below them are a")
    print("deterministic weighted formula over those raw inputs, not a separate")
    print("thing to independently verify.")
    print("=" * 70)


if __name__ == "__main__":
    main()
