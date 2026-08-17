"""
Tests for execution/scanner.py's Phase 25 wiring (see PHASE25_NOTES.md):
MarketScanner._get_circuit_bands() (lazy-cached fetch, respects
_disable_live_market_context) and MarketScanner._detect_circuit_lock()
(per-symbol heuristic using the last two OHLC rows + the fetched band
table).

detect_circuit_lock() itself (the pure heuristic math) is already
covered exhaustively in tests/test_circuit_bands.py — these tests only
check the WIRING: that scanner.py calls it correctly, caches the band
fetch, and honors the backtest opt-out flag. Both a BUY-side (up-move)
and SELL-side (down-move) dataframe are exercised per the standing
BUY/SELL-mirroring rule, since evaluate_position()/scan_symbol() serve
both directions through the same code path.
"""

import pandas as pd

from execution.scanner import MarketScanner


def _ohlc_dataframe(prev_close, open_, high, low, close):
    return pd.DataFrame(
        [
            {"open": prev_close, "high": prev_close, "low": prev_close, "close": prev_close},
            {"open": open_, "high": high, "low": low, "close": close},
        ]
    )


# ==========================================================
# _get_circuit_bands() — lazy cache + _disable_live_market_context gate
# ==========================================================

def test_get_circuit_bands_fetches_once_and_caches(monkeypatch):
    scanner = MarketScanner()
    call_count = {"n": 0}

    def _fake_fetch():
        call_count["n"] += 1
        return {"ACC": 20.0}

    monkeypatch.setattr(scanner._circuit_bands_provider, "fetch_bands", _fake_fetch)

    first = scanner._get_circuit_bands()
    second = scanner._get_circuit_bands()

    assert first == {"ACC": 20.0}
    assert second == {"ACC": 20.0}
    assert call_count["n"] == 1  # only fetched once, second call used the cache


def test_get_circuit_bands_returns_empty_on_fetch_failure(monkeypatch):
    scanner = MarketScanner()

    def _boom():
        raise ConnectionError("no network")

    monkeypatch.setattr(scanner._circuit_bands_provider, "fetch_bands", _boom)

    assert scanner._get_circuit_bands() == {}


def test_get_circuit_bands_short_circuits_when_live_context_disabled(monkeypatch):
    # Backtest mode (analytics/backtest_engine.py) — must NEVER hit the
    # network, same as _get_fii_dii_data()/_get_delivery_data().
    scanner = MarketScanner(disable_live_market_context=True)

    def _should_not_be_called():
        raise AssertionError("fetch_bands() must not be called when disabled")

    monkeypatch.setattr(
        scanner._circuit_bands_provider, "fetch_bands", _should_not_be_called
    )

    assert scanner._get_circuit_bands() == {}


# ==========================================================
# _detect_circuit_lock() — BUY-side (upper circuit / up-move)
# ==========================================================

def test_detect_circuit_lock_buy_side_upper_circuit(monkeypatch):
    scanner = MarketScanner()
    monkeypatch.setattr(scanner, "_get_circuit_bands", lambda: {"RELIANCE": 20.0})

    dataframe = _ohlc_dataframe(
        prev_close=100.0, open_=119.0, high=120.0, low=119.0, close=120.0,
    )

    result = scanner._detect_circuit_lock("RELIANCE.NS", dataframe)

    assert result["circuit_likely"] is True
    assert result["direction"] == "upper"
    assert result["band_used"] == 20.0


# ==========================================================
# _detect_circuit_lock() — SELL-side (lower circuit / down-move)
# ==========================================================

def test_detect_circuit_lock_sell_side_lower_circuit(monkeypatch):
    scanner = MarketScanner()
    monkeypatch.setattr(scanner, "_get_circuit_bands", lambda: {"RELIANCE": 20.0})

    dataframe = _ohlc_dataframe(
        prev_close=100.0, open_=81.0, high=81.0, low=80.0, close=80.0,
    )

    result = scanner._detect_circuit_lock("RELIANCE.NS", dataframe)

    assert result["circuit_likely"] is True
    assert result["direction"] == "lower"
    assert result["band_used"] == 20.0


def test_detect_circuit_lock_strips_ns_suffix_for_band_lookup(monkeypatch):
    seen = {}

    def _fake_bands():
        return {"RELIANCE": 10.0}

    scanner = MarketScanner()
    monkeypatch.setattr(scanner, "_get_circuit_bands", _fake_bands)

    dataframe = _ohlc_dataframe(
        prev_close=100.0, open_=109.0, high=110.0, low=109.0, close=110.0,
    )
    result = scanner._detect_circuit_lock("RELIANCE.NS", dataframe)
    seen["band_used"] = result["band_used"]

    # Confirms the ".NS" suffix was stripped before the dict lookup —
    # if it hadn't been, band_used would be None (key "RELIANCE.NS" not
    # found) and this symbol's real 10% band would be missed.
    assert seen["band_used"] == 10.0


def test_detect_circuit_lock_unknown_symbol_falls_back_to_standard_bands(
    monkeypatch,
):
    scanner = MarketScanner()
    monkeypatch.setattr(scanner, "_get_circuit_bands", lambda: {})  # no band data

    dataframe = _ohlc_dataframe(
        prev_close=100.0, open_=104.0, high=105.0, low=104.0, close=105.0,
    )
    result = scanner._detect_circuit_lock("UNKNOWNSTOCK.NS", dataframe)

    assert result["circuit_likely"] is True  # +5% matches a standard band
    assert result["band_used"] is None


def test_detect_circuit_lock_disabled_in_backtest_mode():
    scanner = MarketScanner(disable_live_market_context=True)

    dataframe = _ohlc_dataframe(
        prev_close=100.0, open_=120.0, high=120.0, low=120.0, close=120.0,
    )
    result = scanner._detect_circuit_lock("RELIANCE.NS", dataframe)

    # Must return the same "no data" safe default as detect_circuit_lock()
    # with all-None inputs — never fabricate a signal during a backtest.
    assert result["circuit_likely"] is False


def test_detect_circuit_lock_insufficient_history_returns_safe_default():
    scanner = MarketScanner()
    dataframe = pd.DataFrame(
        [{"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}]
    )  # only 1 row -> no prev_close available

    result = scanner._detect_circuit_lock("RELIANCE.NS", dataframe)

    assert result["circuit_likely"] is False
