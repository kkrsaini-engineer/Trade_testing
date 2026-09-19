"""
Tests for Phase 4's "dead-code cleanup" fixes from BUG_AUDIT_2026-09-18.md's
"Dead code" section (+ MEDIUM item #20). Per an explicit product decision,
dead code is FIXED IN PLACE, never deleted, so it is correct if it is ever
wired into a live pipeline in the future -- these tests exercise that fixed
behavior directly, even though none of these classes have a live caller
today.

Covered:
  - execution/tracker.py: PositionTracker.update()'s SELL pnl_percent used
    a reciprocal formula instead of mirroring BUY's linear one.
  - execution/trade_executor.py: TradeExecutor.execute() called
    broker.place_order() with the wrong signature/argument shape
    (TypeError if ever triggered).
  - features/indicators/moving_average.py: WMA(20)/WMA(50) index-alignment
    bug silently produced 0.0 for every row.
  - news/event_detection.py + news/ai_news.py: plain substring keyword
    matching (same class of bug as market/macro_intelligence.py's Phase 2
    fix), fixed via the shared \\b-bounded word-matching helper.
  - fundamental/fundamental.py: FundamentalEngine.DEFAULT_WEIGHTS had
    drifted from the live strategy/fundamental_scoring.py weights
    (roe 15 vs 20, operating_cashflow 15 vs 10).

risk/exit_engine.py and fundamental/corporate_actions.py were also
investigated in Phase 4 but had no functional bug -- only a clarifying
STATUS docstring was added, so there is nothing behavioral to test there.
"""

import numpy as np
import pandas as pd

from execution.tracker import PositionState, PositionTracker
from execution.trade_executor import TradeExecutor
from execution.broker import OrderRequest
from features.indicators.moving_average import MovingAverageIndicators
from fundamental.fundamental import FundamentalEngine
from news.ai_news import AINewsEngine
from news.event_detection import EventDetectionEngine
from strategy.fundamental_scoring import (
    _CASH_WEIGHT,
    _ROE_WEIGHT,
)


# ==========================================================
# execution/tracker.py — SELL pnl_percent linear-return fix
# ==========================================================

def _tracker_dataframe(close, atr=2.0, high=None, low=None):
    row = {
        "open": close, "high": high if high is not None else close,
        "low": low if low is not None else close, "close": close,
        "volume": 1_000_000, "atr": atr, "atr_14": atr,
        "volume_sma_20": 900_000, "ema_20": 100.0, "ema_50": 95.0,
        "volatility_state": "NORMAL",
    }
    return pd.DataFrame([row])


def test_sell_pnl_percent_uses_linear_not_reciprocal_return():
    tracker = PositionTracker()
    tracker.add_position(
        PositionState(
            symbol="SHORT_TEST", entry_price=100.0, quantity=10,
            direction="SELL", entry_time="2026-01-01",
            current_price=100.0, highest_price=100.0, lowest_price=100.0,
        )
    )
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}

    # Price HALVES (100 -> 50): a genuine +50% short gain.
    # The old reciprocal bug ((entry/close - 1) * 100) gave +100% instead.
    results = tracker.update(
        dataframe_map={"SHORT_TEST": _tracker_dataframe(close=50.0, high=51.0, low=49.0)},
        portfolio=portfolio, market=market,
    )
    assert results[0].pnl_percent == 50.0


def test_sell_pnl_percent_mirrors_buy_formula_symmetrically():
    buy_tracker = PositionTracker()
    buy_tracker.add_position(
        PositionState(
            symbol="B", entry_price=100.0, quantity=10, direction="BUY",
            entry_time="2026-01-01", current_price=100.0,
            highest_price=100.0, lowest_price=100.0,
        )
    )
    sell_tracker = PositionTracker()
    sell_tracker.add_position(
        PositionState(
            symbol="S", entry_price=100.0, quantity=10, direction="SELL",
            entry_time="2026-01-01", current_price=100.0,
            highest_price=100.0, lowest_price=100.0,
        )
    )
    portfolio = {"equity": 100_000, "open_positions": {}}
    market = {"regime": "NORMAL"}

    # BUY gaining from 100 -> 120 should read the same magnitude as
    # SELL gaining from 100 -> 80 (both a +20% favorable move).
    buy_results = buy_tracker.update(
        dataframe_map={"B": _tracker_dataframe(close=120.0, high=121.0, low=119.0)},
        portfolio=portfolio, market=market,
    )
    sell_results = sell_tracker.update(
        dataframe_map={"S": _tracker_dataframe(close=80.0, high=81.0, low=79.0)},
        portfolio=portfolio, market=market,
    )
    assert buy_results[0].pnl_percent == sell_results[0].pnl_percent == 20.0


# ==========================================================
# execution/trade_executor.py — correct place_order() call shape
# ==========================================================

class _FakeRiskManager:
    def evaluate_order(self, order, portfolio):
        return {"approved": True}


class _CapturingBroker:
    def __init__(self):
        self.calls = []

    def place_order(self, order, market_price, market_state):
        self.calls.append(
            {"order": order, "market_price": market_price, "market_state": market_state}
        )
        return "FAKE_RESULT"


def test_trade_executor_calls_place_order_with_correct_signature():
    broker = _CapturingBroker()
    executor = TradeExecutor(broker=broker, risk_manager=_FakeRiskManager())

    order = {
        "id": "ORD1", "symbol": "RELIANCE", "action": "BUY", "quantity": 10,
        "market_price": 2500.0, "market_state": {"market_open": True},
    }
    results = executor.execute(orders=[order], portfolio={})

    assert len(broker.calls) == 1
    call = broker.calls[0]
    # Must be a real OrderRequest with attribute access, not the raw dict.
    assert isinstance(call["order"], OrderRequest)
    assert call["order"].symbol == "RELIANCE"
    assert call["order"].action == "BUY"
    assert call["order"].quantity == 10
    assert call["market_price"] == 2500.0
    assert call["market_state"] == {"market_open": True}

    assert len(results) == 1
    assert results[0]["status"] == "EXECUTED"
    assert results[0]["result"] == "FAKE_RESULT"
    assert "ORD1" in executor.executed_ids


def test_trade_executor_defaults_market_price_and_state_when_absent():
    broker = _CapturingBroker()
    executor = TradeExecutor(broker=broker, risk_manager=_FakeRiskManager())

    order = {"id": "ORD2", "symbol": "TCS", "action": "SELL", "quantity": 5}
    executor.execute(orders=[order], portfolio={})

    call = broker.calls[0]
    assert call["market_price"] == 0.0
    assert call["market_state"] == {}


# ==========================================================
# features/indicators/moving_average.py — WMA index-alignment fix
# ==========================================================

def test_wma_is_not_silently_zero():
    closes = [100 + i for i in range(30)]
    df_ohlc = pd.DataFrame({"close": closes, "volume": [1_000_000] * 30})
    df = MovingAverageIndicators().calculate(df_ohlc)

    last_wma_20 = df["wma_20"].iloc[-1]
    assert last_wma_20 != 0.0

    # Manually compute the expected WMA(20) for the last row.
    window = np.array(closes[-20:], dtype="float64")
    weights = np.arange(1, 21, dtype="float64")
    expected = (window * weights).sum() / weights.sum()
    assert last_wma_20 == expected


def test_wma_50_matches_manual_weighted_average():
    closes = [200 - i * 0.5 for i in range(60)]
    df_ohlc = pd.DataFrame({"close": closes, "volume": [500_000] * 60})
    df = MovingAverageIndicators().calculate(df_ohlc)

    window = np.array(closes[-50:], dtype="float64")
    weights = np.arange(1, 51, dtype="float64")
    expected = (window * weights).sum() / weights.sum()
    assert df["wma_50"].iloc[-1] == expected


# ==========================================================
# news/event_detection.py + news/ai_news.py — word-boundary fix
# ==========================================================

def test_event_detection_does_not_false_positive_on_substrings():
    engine = EventDetectionEngine()
    # "reorder" contains "order" as a substring but is not an ORDER event;
    # "emerges" contains "merge" but is not a MERGER event.
    events = engine.detect([
        {"title": "Company plans to reorder its supply chain priorities"},
        {"title": "New competitor emerges in the smartphone market"},
    ])
    assert events[0]["event_type"] == "GENERAL"
    assert events[1]["event_type"] == "GENERAL"


def test_event_detection_still_matches_real_whole_word_events():
    engine = EventDetectionEngine()
    events = engine.detect([{"title": "Company announces merger with rival firm"}])
    assert events[0]["event_type"] == "MERGER"


def test_ai_news_does_not_false_positive_on_substrings():
    engine = AINewsEngine()
    processed = engine.process([
        {"title": "Staff praised for professional courtesy during the visit"},
    ])
    # "courtesy" contains "court" as a substring; must not be LITIGATION.
    assert processed[0]["event"] == "GENERAL"


def test_ai_news_still_matches_real_whole_word_events():
    engine = AINewsEngine()
    processed = engine.process([{"title": "XYZ Ltd declares special dividend"}])
    assert processed[0]["event"] == "DIVIDEND"


# ==========================================================
# fundamental/fundamental.py — weight realignment with the live
# strategy/fundamental_scoring.py weights
# ==========================================================

def test_fundamental_engine_weights_match_live_scoring_weights():
    assert FundamentalEngine.DEFAULT_WEIGHTS["roe"] == _ROE_WEIGHT
    assert FundamentalEngine.DEFAULT_WEIGHTS["operating_cashflow"] == _CASH_WEIGHT
