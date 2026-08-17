"""
Integration tests for strategy/buy_strategy.py's Point 15 wiring
(PHASE29_NOTES.md) — proves the real BuyStrategyEngine.evaluate(), not
just decision/state_rules.py in isolation, produces the right
qualified/action/state_narrative for each branch of the rule table.

Every fixture row below was empirically verified against the real
engine (not hand-derived from reading the source) before being locked
in as a test — see PHASE29_NOTES.md for the exact probe methodology.

The headline case (test_trend_state_contradiction_now_hard_rejects) is
the actual bug this phase fixes: BEFORE this change, this exact input
qualified as a live BUY (action=BUY, EntryState=TRIGGERED) despite its
own printed TrendState=DOWNTREND — see the module docstring in
decision/state_rules.py for why.
"""

import pandas as pd

from strategy.buy_strategy import BuyStrategyEngine

STRONG_FUNDAMENTALS = dict(
    revenue_growth=10.0, earnings_growth=12.0, roe=18.0, pe=20.0,
    pb=3.0, peg=1.2, debt_to_equity=0.5, operating_cashflow=1000.0,
)

BASE_ROW = dict(
    open=98.0, close=100.0, high=101.0, low=97.0,
    ema_20=97.0, ema_50=95.0, ema_200=90.0,
    sma_20=97.0, sma_50=95.0, sma_200=90.0,
    rsi_14=60.0, macd=1.0, macd_signal=0.5,
    adx_14=30.0, supertrend=1, vwap=98.0,
    volume=200000, volume_sma_20=100000,
    obv=1000, cmf_20=0.1, mfi_14=60.0,
    market_regime="BULL", volatility_state="NORMAL",
    is_breakout=False, is_pullback=False,
    bb_upper=103.0, bb_middle=99.0, bb_lower=95.0, bb_width=0.03,
    atr_14=2.0, roc_12=2.0, cci_20=120.0,
    stoch_k=60.0, stoch_d=55.0, williams_r_14=-40.0,
    dc_middle=97.0, pivot=97.0, gap_day=False,
    delivery_percentage=None, breadth="STRONG",
)


def _dataframe(n=10, **overrides):
    row = dict(BASE_ROW)
    row.update(overrides)
    df = pd.DataFrame([dict(row) for _ in range(n)])
    return df


ENGINE = BuyStrategyEngine()


def _evaluate(df, fundamentals=STRONG_FUNDAMENTALS, news_score=70.0, market_score=70.0, sector_score=70.0):
    return ENGINE.evaluate(
        dataframe=df, fundamentals=fundamentals, news_score=news_score,
        market_score=market_score, sector_score=sector_score,
    )


def test_clean_setup_triggers_buy():
    result = _evaluate(_dataframe())
    assert result.action == "BUY"
    assert result.passed is True
    assert "EntryState=TRIGGERED" in result.state_narrative
    assert "TrendState=UPTREND" in result.state_narrative


def test_trend_state_contradiction_now_hard_rejects():
    # tier1_passed=True (all 3 of ema_alignment/market_trend/
    # sma_alignment pass) but close < ema_20 -> price_above_ema20=False
    # -> TrendState=DOWNTREND. Score is well above threshold (74.75).
    # BEFORE Point 15, this went through as action=BUY/TRIGGERED.
    df = _dataframe(
        open=96.0, close=95.0, high=96.5, low=94.0,
        ema_20=97.0, ema_50=90.0, ema_200=85.0,
        sma_20=97.0, sma_50=90.0, sma_200=85.0,
        vwap=95.5, bb_upper=99.0, bb_middle=96.0, bb_lower=93.0,
        dc_middle=95.0, pivot=94.0,
    )
    result = _evaluate(df)
    assert result.overall_score >= 58.0  # score alone would have qualified
    assert bool(result.tier1_passed) is True   # majority vote alone would have qualified
    assert "TrendState=DOWNTREND" in result.state_narrative
    assert result.action == "NO_TRADE"
    assert result.passed is False
    assert "EntryState=REJECTED_TREND" in result.state_narrative
    assert any("TrendState=DOWNTREND contradicts the trade direction" in r for r in result.reasons)


def test_stale_entry_hard_rejects():
    # is_breakout running, no fresh trigger today (volume_spike killed
    # by raising volume_sma_20 to just under volume).
    df = _dataframe(is_breakout=True, volume=105000, volume_sma_20=100000)
    result = _evaluate(df)
    assert result.action == "NO_TRADE"
    assert "SetupState=STALE_BREAKOUT" in result.state_narrative
    assert "EntryState=REJECTED_STALE_ENTRY" in result.state_narrative
    assert any("stale entry" in r for r in result.reasons)


def test_overextended_hard_rejects_even_with_a_fresh_trigger():
    # close far enough above ema_20 to breach the ATR-normalized cap.
    # Also happens to trip squeeze_breakout (a fresh trigger) -- proves
    # rule ORDER: overextension still wins even when SetupState would
    # otherwise read as fresh, not stale.
    df = _dataframe(close=110.0, high=111.0)
    result = _evaluate(df)
    assert result.action == "NO_TRADE"
    assert "EntryState=REJECTED_OVEREXTENDED" in result.state_narrative
    assert any("overextension cap breached" in r for r in result.reasons)


def test_below_threshold_waits_not_hard_rejected():
    # No fundamentals data at all -> fundamental/tier3 drags score
    # under 58, but every hard-gate check passes.
    result = _evaluate(_dataframe(), fundamentals={})
    assert result.overall_score < 58.0
    assert result.action == "NO_TRADE"
    assert "EntryState=WAITING" in result.state_narrative
    # WAITING must not append a "Rejected: ..." hard-reject reason line.
    assert not any(r.startswith("Rejected:") for r in result.reasons)
