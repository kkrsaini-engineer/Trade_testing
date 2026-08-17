"""
Tests for scripts/diagnose_fundamental_snapshot.py's own aggregation
helper (`_aggregate_news_score`) — the one piece of logic this
diagnostic script owns itself. Everything else is a direct pass-through
of real production functions (DataEngine, SentimentEngine,
buy_fundamental_evaluation, MarketRegimeEngine, _signed_news_bias,
news_component), already covered by their own test suites — nothing to
re-test here.
"""

from scripts.diagnose_fundamental_snapshot import _aggregate_news_score


def test_no_news_returns_none():
    assert _aggregate_news_score([]) is None


def test_all_positive_news_scores_above_50():
    scored = [
        {"sentiment": "POSITIVE", "impact_score": 80.0},
        {"sentiment": "POSITIVE", "impact_score": 65.0},
    ]
    result = _aggregate_news_score(scored)
    assert result > 50.0


def test_all_negative_news_scores_below_50():
    scored = [
        {"sentiment": "NEGATIVE", "impact_score": 80.0},
        {"sentiment": "NEGATIVE", "impact_score": 65.0},
    ]
    result = _aggregate_news_score(scored)
    assert result < 50.0


def test_all_neutral_news_scores_exactly_50():
    scored = [
        {"sentiment": "NEUTRAL", "impact_score": 50.0},
        {"sentiment": "NEUTRAL", "impact_score": 50.0},
    ]
    result = _aggregate_news_score(scored)
    assert result == 50.0


def test_mixed_news_stays_within_0_100():
    scored = [
        {"sentiment": "POSITIVE", "impact_score": 100.0},
        {"sentiment": "NEGATIVE", "impact_score": 100.0},
    ]
    result = _aggregate_news_score(scored)
    assert 0.0 <= result <= 100.0
