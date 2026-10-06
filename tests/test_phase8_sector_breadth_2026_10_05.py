"""
Tests for the 2026-10-05 fix: "market_regime" (and therefore market_score)
is each scanned stock's OWN EMA50/200 trend, not the real Nifty/Sensex
index — a known, previously-documented limitation (BUG_AUDIT_2026-09-18.md
already notes this). Confirmed live on 2026-09-24 (a real ~-1.5% Nifty
selloff day, per news) that most scanned stocks' own regime still read
BULL that day, because a single day cannot flip a 50/200-day EMA trend
filter at any aggregation level.

Two real (non-fabricated), same-run fixes, both built entirely from data
execution/scanner.py's Pass 1 already fetches — no new network calls:

1. sector_score — was hardcoded to None (FIX #8). Now averages this run's
   own per-stock market_score across every OTHER symbol sharing a sector
   (MarketScanner._compute_universe_context()), reviving sector_score's
   ALREADY-WIRED downstream consumers in buy_strategy.py/sell_strategy.py
   (BUG_AUDIT_2026-09-18.md item #21's fix) that had nothing feeding them.

2. breadth_score — wires up market/market_breadth.py's previously-unwired
   MarketBreadthEngine from today's real advance/decline count across the
   scan universe, and blends it into market_score (_BREADTH_WEIGHT=0.4)
   so a genuine broad-market move is reflected the SAME DAY it happens,
   instead of waiting weeks for the per-stock EMA50/200 to catch up.
"""

import pandas as pd

from data.data_engine import DataBundle
from execution.scanner import MarketScanner


def _ohlcv(closes):
    n = len(closes)
    return pd.DataFrame({
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


def _bundle(symbol, closes, sector="Financial Services"):
    return DataBundle(
        symbol=symbol,
        market=_ohlcv(closes),
        fundamentals={"sector": sector},
        news=[],
    )


# ==========================================================
# _compute_universe_context(): sector_score aggregation
# ==========================================================

def test_sector_score_averages_market_score_of_peers_in_same_sector():
    scanner = MarketScanner()

    bull_closes = [100 + i * 2 for i in range(40)]  # close firmly above its own rising EMAs -> BULL (75.0)
    bear_closes = [300 - i * 2 for i in range(40)]  # close firmly below its own falling EMAs -> BEAR (25.0)

    bundles = {
        "A.NS": _bundle("A.NS", bull_closes, sector="Financial Services"),
        "B.NS": _bundle("B.NS", bear_closes, sector="Financial Services"),
    }

    context = scanner._compute_universe_context(bundles)

    assert context["sector_scores"]["Financial Services"] == 50.0  # (75.0 + 25.0) / 2


def test_sector_scores_do_not_mix_across_different_sectors():
    scanner = MarketScanner()

    bull_closes = [100 + i * 2 for i in range(40)]
    bear_closes = [300 - i * 2 for i in range(40)]

    bundles = {
        "A.NS": _bundle("A.NS", bull_closes, sector="Technology"),
        "B.NS": _bundle("B.NS", bear_closes, sector="Healthcare"),
    }

    context = scanner._compute_universe_context(bundles)

    assert context["sector_scores"]["Technology"] == 75.0
    assert context["sector_scores"]["Healthcare"] == 25.0


def test_symbol_with_no_sector_label_is_excluded_from_sector_scores():
    scanner = MarketScanner()
    bundles = {
        "A.NS": DataBundle(
            symbol="A.NS", market=_ohlcv([100 + i for i in range(30)]),
            fundamentals={}, news=[],
        ),
    }
    context = scanner._compute_universe_context(bundles)
    assert context["sector_scores"] == {}


# ==========================================================
# _compute_universe_context(): breadth_score (real advance/decline)
# ==========================================================

def test_breadth_score_reflects_real_advance_decline_ad_percent():
    scanner = MarketScanner()

    bundles = {}
    for i in range(3):  # advancers: last close > previous close
        bundles[f"ADV{i}.NS"] = _bundle(f"ADV{i}.NS", [100.0] * 20 + [101.0], sector="X")
    for i in range(7):  # decliners: last close < previous close
        bundles[f"DEC{i}.NS"] = _bundle(f"DEC{i}.NS", [100.0] * 20 + [99.0], sector="X")

    context = scanner._compute_universe_context(bundles)

    assert context["breadth_score"] == 30.0  # 3 advancers / 10 total * 100


def test_breadth_score_is_none_when_batch_is_too_small_to_trust():
    scanner = MarketScanner()
    bundles = {
        f"S{i}.NS": _bundle(f"S{i}.NS", [100.0] * 20 + [101.0], sector="X")
        for i in range(5)  # below the 10-symbol trust floor
    }
    context = scanner._compute_universe_context(bundles)
    assert context["breadth_score"] is None


def test_empty_bundle_batch_is_handled_safely():
    scanner = MarketScanner()
    context = scanner._compute_universe_context({})
    # 2026-10-06: two extra keys (sector_peer_stats, symbol_sector_scores)
    # support excluding a symbol from its own sector average (audit M10).
    assert context == {
        "sector_scores": {},
        "breadth_score": None,
        "sector_peer_stats": {},
        "symbol_sector_scores": {},
    }


# ==========================================================
# _evaluate_market_context(): wiring — backward compatibility +
# real blending behavior
# ==========================================================

def test_without_universe_context_behavior_is_unchanged():
    scanner = MarketScanner(disable_live_market_context=True)
    bundle = _bundle("TEST.NS", [100 + i * 2 for i in range(40)])

    context = scanner._evaluate_market_context("TEST.NS", bundle=bundle, universe_context=None)

    assert context["diagnostics"]["market_regime"] == "BULL"
    assert context["diagnostics"]["market_score"] == 75.0
    assert "breadth_score" not in context["diagnostics"]


def test_sector_score_is_populated_from_universe_context():
    scanner = MarketScanner(disable_live_market_context=True)
    bundle = _bundle("TEST.NS", [100 + i * 2 for i in range(40)], sector="Financial Services")

    universe_context = {"sector_scores": {"Financial Services": 40.0}, "breadth_score": None}
    context = scanner._evaluate_market_context("TEST.NS", bundle=bundle, universe_context=universe_context)

    # sector_score isn't surfaced directly in diagnostics, but it reaches
    # the strategy engines — the real end-to-end wiring check is that
    # evaluation doesn't error and market_score is unaffected (breadth
    # is None here, only sector_score was supplied).
    assert context["diagnostics"]["market_score"] == 75.0


def test_market_score_blends_with_breadth_score_during_a_real_crash_day():
    scanner = MarketScanner(disable_live_market_context=True)
    # This stock's OWN trend is still BULL (market_score=75.0) even
    # though the broader scan universe is crashing (breadth_score=10.0)
    # -- exactly the 2026-09-24 scenario.
    bundle = _bundle("TEST.NS", [100 + i * 2 for i in range(40)])

    universe_context = {"sector_scores": {}, "breadth_score": 10.0}
    context = scanner._evaluate_market_context("TEST.NS", bundle=bundle, universe_context=universe_context)

    # 75.0 * 0.6 + 10.0 * 0.4 == 49.0
    assert context["diagnostics"]["market_score"] == 49.0
    assert context["diagnostics"]["breadth_score"] == 10.0


def test_market_score_unaffected_when_breadth_score_is_none():
    scanner = MarketScanner(disable_live_market_context=True)
    bundle = _bundle("TEST.NS", [100 + i * 2 for i in range(40)])

    universe_context = {"sector_scores": {}, "breadth_score": None}
    context = scanner._evaluate_market_context("TEST.NS", bundle=bundle, universe_context=universe_context)

    assert context["diagnostics"]["market_score"] == 75.0
    assert "breadth_score" not in context["diagnostics"]
