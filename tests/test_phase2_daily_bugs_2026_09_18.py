"""
Tests for Phase 2's "medium batch" of daily-live bug fixes from
BUG_AUDIT_2026-09-18.md:

4. market/macro_intelligence.py + market_intelligence/
   market_intelligence_engine.py: keyword matching was plain substring,
   not whole-word ("war" matched inside "software"/"warehouse"/"award").
5. core/notifications.py: a Telegram send failure used to crash the
   caller (output/telegram_alert.py deliberately re-raises); notify()
   must swallow it.
6. scripts/morning_executor.py: "action" was written as the trade
   direction ("BUY"/"SELL") instead of the OPEN/CLOSE lifecycle value
   every reader (analysis_engine.py, learning_engine.py) actually filters
   on.
7. features/indicators/momentum.py: RSI gave NaN instead of 100 when
   avg_loss == 0 (a 14-bar all-green run), silently skipping the BUY-side
   overbought penalty.
8. data/market_data.py: no check that the last fetched row is actually
   fresh, only that enough rows came back — could silently accept
   multi-day-stale data.
"""

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from core.exceptions import DataError
from data.market_data import MarketDataProvider
from features.indicators.momentum import MomentumIndicators
from market import macro_intelligence
from market_intelligence.market_intelligence_engine import MarketIntelligenceEngine


# ==========================================================
# BUG #4 — word-boundary keyword matching
# ==========================================================

def test_war_keyword_does_not_match_inside_unrelated_words():
    # The exact false positives the audit named.
    for headline in [
        "Company XYZ launches new software platform",
        "New warehouse facility inaugurated in Pune",
        "Firm wins prestigious industry award",
    ]:
        assert macro_intelligence.sector_bias([headline], "Defence") == 0.0


def test_war_keyword_still_matches_as_a_real_whole_word():
    bias = macro_intelligence.sector_bias(["Russia declares war on neighboring state"], "Defence")
    assert bias != 0.0


def test_war_plural_and_variant_forms_still_match_after_the_boundary_fix():
    # Whole-word matching stops "war" from matching "wars"/"warfare" via
    # substring -- the fix must add these forms explicitly (same
    # convention as news/sentiment_engine.py's POSITIVE/NEGATIVE sets)
    # so real coverage isn't lost.
    for headline in [
        "Border wars escalate overnight",
        "Cyber warfare concerns rattle markets",
    ]:
        assert macro_intelligence.sector_bias([headline], "Defence") != 0.0


def test_rate_hike_plural_form_still_matches():
    # A plain \b-bounded "rate hike" would NOT match "rate hikes" (old
    # substring matching did) -- the fix must add the plural explicitly.
    bias = macro_intelligence.sector_bias(["Fed signals more rate hikes ahead"], "Banks")
    assert bias != 0.0


def test_tariff_plural_form_still_matches():
    bias = macro_intelligence.sector_bias(["New tariffs imposed on pharma exports"], "Pharma")
    assert bias != 0.0


def test_market_intelligence_engine_uses_the_same_word_boundary_rule():
    from news.sentiment_engine import SentimentEngine

    engine = MarketIntelligenceEngine.__new__(MarketIntelligenceEngine)
    engine.sentiment_engine = SentimentEngine()
    result = engine._analyze_macro(["IT major unveils new software suite"])
    # "software" must NOT be treated as a critical macro event just
    # because it contains "war" as a substring.
    assert result["macro_risk_score"] == 0.0


# ==========================================================
# BUG #5 — Telegram crash-guard
# ==========================================================

class _BoomingTelegram:
    def send(self, message, level="INFO", raw=False):
        raise RuntimeError("simulated Telegram API failure")


def test_notify_does_not_raise_when_telegram_send_fails(monkeypatch, tmp_path):
    import core.notifications as notifications

    monkeypatch.setattr(notifications, "_get_telegram", lambda: _BoomingTelegram())
    monkeypatch.setattr(notifications, "DEDUP_STORE_PATH", str(tmp_path / "dedup.json"))

    # Must not raise, even though the underlying Telegram client always
    # raises -- this is the exact crash the audit flagged.
    result = notifications.notify("test_event", "hello world", dedup_key="unique-key-1")
    assert result is True


def test_notify_swallows_failures_on_every_part_of_a_split_long_message(monkeypatch, tmp_path):
    import core.notifications as notifications

    monkeypatch.setattr(notifications, "_get_telegram", lambda: _BoomingTelegram())
    monkeypatch.setattr(notifications, "DEDUP_STORE_PATH", str(tmp_path / "dedup.json"))

    long_message = "\n".join(f"line {i} of a very long advisory report" for i in range(500))
    result = notifications.notify("test_event_long", long_message, dedup_key="unique-key-2")
    assert result is True


# ==========================================================
# BUG #6 — morning_executor.py "action" schema drift
# ==========================================================

def test_morning_executor_writes_action_open_not_direction():
    # main() is a monolithic script entrypoint (market data / portfolio /
    # news all wired together) that isn't practically unit-testable in
    # isolation; this is a targeted regression guard on the exact
    # save_trade() call the audit identified (scripts/morning_executor.py
    # L416-420), asserting it writes the OPEN/CLOSE lifecycle convention
    # analysis_engine.py/learning_engine.py actually read, not the raw
    # BUY/SELL direction a second time under a different key.
    source = Path("scripts/morning_executor.py").read_text()
    match = re.search(
        r'trade_store\.save_trade\(\{\s*'
        r'"id":\s*trade_id,\s*"symbol":\s*symbol,\s*"direction":\s*direction,\s*'
        r'"action":\s*("[^"]*"|direction)',
        source,
    )
    assert match is not None, "Could not locate the morning_executor.py save_trade() call"
    assert match.group(1) == '"OPEN"'


# ==========================================================
# BUG #7 — RSI NaN-instead-of-100
# ==========================================================

def _flat_then_moving_closes(moves: list[float]) -> pd.DataFrame:
    """20 flat warm-up closes (so the 14-bar window is always full) then
    the given sequence of day-over-day price moves applied on top."""
    closes = [100.0] * 6
    for m in moves:
        closes.append(closes[-1] + m)
    n = len(closes)
    return pd.DataFrame({
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
    })


def test_rsi_is_100_not_nan_when_avg_loss_is_zero():
    # 14 straight up-or-flat days -> avg_loss == 0.
    df_ohlc = _flat_then_moving_closes([1.0] * 14)
    df = MomentumIndicators().calculate(df_ohlc)
    assert df["rsi_14"].iloc[-1] == 100.0
    assert not df["rsi_14"].iloc[-5:].isna().any()


def test_rsi_is_0_when_avg_gain_is_zero_unaffected_by_the_fix():
    # 14 straight down-or-flat days -> avg_gain == 0. This side already
    # worked correctly before the fix; must still be exactly 0, not
    # accidentally touched by the new avg_loss==0 handling.
    df_ohlc = _flat_then_moving_closes([-1.0] * 14)
    df = MomentumIndicators().calculate(df_ohlc)
    assert df["rsi_14"].iloc[-1] == 0.0


def test_rsi_is_50_when_price_is_completely_flat_for_the_whole_window():
    df_ohlc = _flat_then_moving_closes([0.0] * 14)
    df = MomentumIndicators().calculate(df_ohlc)
    assert df["rsi_14"].iloc[-1] == 50.0


def test_rsi_warmup_period_still_nan():
    df_ohlc = _flat_then_moving_closes([1.0] * 14)
    df = MomentumIndicators().calculate(df_ohlc)
    assert df["rsi_14"].iloc[:13].isna().all()


# ==========================================================
# BUG #8 — market_data.py last-row staleness check
# ==========================================================

def _ohlc_df(last_date: date) -> pd.DataFrame:
    dates = pd.date_range(end=pd.Timestamp(last_date), periods=5, freq="B")
    return pd.DataFrame({
        "timestamp": dates,
        "open": [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5,
        "close": [100.0] * 5, "volume": [1000] * 5,
    })


def test_latest_expected_trading_day_is_today_after_close():
    provider = MarketDataProvider()
    # Wednesday 2026-09-16 is a trading day; checked at 23:30 (after the
    # 15:30 close) -> today's own session should already be available.
    now = datetime(2026, 9, 16, 23, 30)
    assert provider._latest_expected_trading_day(now) == date(2026, 9, 16)


def test_latest_expected_trading_day_is_yesterday_before_close():
    provider = MarketDataProvider()
    # Same Wednesday, but checked at 10:00 -- market is open/today's
    # session hasn't closed yet, so today's row can't be expected.
    now = datetime(2026, 9, 16, 10, 0)
    assert provider._latest_expected_trading_day(now) == date(2026, 9, 15)


def test_fresh_last_row_passes_the_staleness_check(monkeypatch):
    # A Wednesday close checked from Wednesday night (after market close)
    # -- today's own session, the freshest possible last row.
    monkeypatch.setattr(
        "data.market_data.now_ist", lambda: datetime(2026, 9, 16, 23, 30)
    )
    provider = MarketDataProvider()
    df = _ohlc_df(date(2026, 9, 16))
    provider._check_last_row_freshness(df, "TESTSYM")  # must not raise


def test_stale_last_row_is_rejected(monkeypatch):
    # "now" is the following Monday night, but the last row is still
    # from the PREVIOUS Wednesday -- several real sessions (Thu, Fri,
    # Mon) behind. This is the actual "frozen for days" incident shape
    # the audit describes.
    monkeypatch.setattr(
        "data.market_data.now_ist", lambda: datetime(2026, 9, 21, 23, 30)
    )
    provider = MarketDataProvider()
    df = _ohlc_df(date(2026, 9, 16))  # Wednesday

    with pytest.raises(DataError):
        provider._check_last_row_freshness(df, "TESTSYM")


def test_one_session_of_staleness_tolerance_is_allowed(monkeypatch):
    # Yahoo publishing the very latest close a little late right after
    # market close must NOT be rejected -- only genuinely multi-session
    # stale data should be. Wed evening "now", Tuesday's last row: 1
    # trading session behind, within tolerance.
    monkeypatch.setattr(
        "data.market_data.now_ist", lambda: datetime(2026, 9, 16, 23, 30)
    )
    provider = MarketDataProvider()
    df = _ohlc_df(date(2026, 9, 15))  # Tuesday
    provider._check_last_row_freshness(df, "TESTSYM")  # must not raise
