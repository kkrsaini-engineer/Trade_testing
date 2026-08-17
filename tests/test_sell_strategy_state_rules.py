"""
Integration tests for strategy/sell_strategy.py's Point 15 wiring
(PHASE29_NOTES.md) — mirrors test_buy_strategy_state_rules.py exactly,
proving the real SellStrategyEngine.evaluate() gets the same fix on the
SELL side. See that file's module docstring for the fixture-verification
methodology.
"""

import pandas as pd

from strategy.sell_strategy import SellStrategyEngine

WEAK_FUNDAMENTALS = dict(
    revenue_growth=-5.0, earnings_growth=-8.0, roe=5.0, pe=45.0,
    pb=8.0, peg=3.0, debt_to_equity=2.5, operating_cashflow=-100.0,
)

HEALTHY_FUNDAMENTALS = dict(
    revenue_growth=15.0, earnings_growth=15.0, roe=20.0, pe=18.0,
    pb=2.0, peg=1.0, debt_to_equity=0.3, operating_cashflow=500.0,
)

BASE_ROW = dict(
    open=102.0, close=100.0, high=103.0, low=99.0,
    ema_20=103.0, ema_50=105.0, ema_200=110.0,
    sma_20=103.0, sma_50=105.0, sma_200=110.0,
    rsi_14=40.0, macd=-1.0, macd_signal=-0.5, macd_histogram=-0.5,
    adx_14=30.0, supertrend=0, vwap=102.0,
    volume=200000, volume_sma_20=100000,
    obv=-1000, cmf_20=-0.1, mfi_14=40.0,
    market_regime="BEAR", volatility_state="NORMAL",
    is_breakdown=False, is_pullback=False,
    bb_upper=107.0, bb_middle=101.0, bb_lower=97.0, bb_width=0.03,
    kc_middle=103.0, atr_14=2.0, roc_12=-2.0, cci_20=-120.0,
    stochastic_k=40.0, stochastic_d=45.0, williams_r_14=-60.0,
    dc_middle=103.0, pivot=103.0, gap_up=False, gap_down=False, gap_day=False,
    delivery_percentage=None, breadth="WEAK",
)


def _dataframe(n=10, **overrides):
    row = dict(BASE_ROW)
    row.update(overrides)
    df = pd.DataFrame([dict(row) for _ in range(n)])
    return df


ENGINE = SellStrategyEngine()


def _evaluate(df, fundamentals=WEAK_FUNDAMENTALS, news_score=30.0, market_score=30.0, sector_score=30.0):
    return ENGINE.evaluate(
        dataframe=df, fundamentals=fundamentals, news_score=news_score,
        market_score=market_score, sector_score=sector_score,
    )


def test_clean_setup_triggers_sell():
    result = _evaluate(_dataframe())
    assert result.action == "SELL"
    assert result.passed is True
    assert "EntryState=TRIGGERED" in result.state_narrative
    assert "TrendState=DOWNTREND" in result.state_narrative


def test_trend_state_contradiction_now_hard_rejects():
    # tier1_passed=True (all 3 of ema_alignment/market_trend/
    # sma_alignment pass, bearish direction) but close > ema_20 ->
    # price_below_ema20=False -> TrendState=UPTREND. Score is above
    # threshold (66.36). BEFORE Point 15, this went through as
    # action=SELL/TRIGGERED.
    df = _dataframe(close=105.0, high=106.0)
    result = _evaluate(df)
    assert result.overall_score >= 58.0
    assert bool(result.tier1_passed) is True
    assert "TrendState=UPTREND" in result.state_narrative
    assert result.action == "NO_TRADE"
    assert result.passed is False
    assert "EntryState=REJECTED_TREND" in result.state_narrative
    assert any("TrendState=UPTREND contradicts the trade direction" in r for r in result.reasons)


def test_stale_entry_hard_rejects():
    df = _dataframe(is_breakdown=True, volume=105000, volume_sma_20=100000)
    result = _evaluate(df)
    assert result.action == "NO_TRADE"
    assert "SetupState=STALE_BREAKDOWN" in result.state_narrative
    assert "EntryState=REJECTED_STALE_ENTRY" in result.state_narrative
    assert any("stale entry" in r for r in result.reasons)


def test_overextended_hard_rejects_even_with_a_fresh_trigger():
    df = _dataframe(close=90.0, low=89.0)
    result = _evaluate(df)
    assert result.action == "NO_TRADE"
    assert "EntryState=REJECTED_OVEREXTENDED" in result.state_narrative
    assert any("overextension cap breached" in r for r in result.reasons)


def test_below_threshold_waits_not_hard_rejected():
    # Fundamentally HEALTHY company (unfavorable for a SELL, unlike the
    # buy-side "no data" case -- missing data reads as maximally bearish
    # here, see PHASE29_NOTES.md) drags the score under 58, but every
    # hard-gate check passes.
    result = _evaluate(
        _dataframe(breadth="NEUTRAL"), fundamentals=HEALTHY_FUNDAMENTALS,
        news_score=70.0, market_score=70.0, sector_score=70.0,
    )
    assert result.overall_score < 58.0
    assert result.action == "NO_TRADE"
    assert "EntryState=WAITING" in result.state_narrative
    assert not any(r.startswith("Rejected:") for r in result.reasons)
