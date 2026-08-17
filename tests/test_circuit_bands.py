"""
Tests for market/circuit_bands.py (Phase 25, see PHASE25_NOTES.md).

detect_circuit_lock() is direction-agnostic by construction (it takes
raw OHLC + prev_close, not a BUY/SELL action) but is tested for BOTH the
upper-circuit (up-move) and lower-circuit (down-move) case per the
standing BUY/SELL-mirroring rule, since those are this function's two
symmetric code paths.

CircuitBandsProvider's network fetch itself is NOT tested here (this
sandbox has no outbound network access at all — confirmed separately,
not NSE-specific). _parse_csv() (pure, no network) and the cache
read/write round-trip ARE tested.
"""

import json

from market.circuit_bands import (
    CircuitBandsProvider,
    DEFAULT_TOLERANCE_PERCENT,
    STANDARD_BANDS_PERCENT,
    detect_circuit_lock,
)


# ==========================================================
# detect_circuit_lock() — UPPER circuit (up-move)
# ==========================================================

def test_upper_circuit_full_day_freeze_detected():
    # +20% move, open==high==low==close at the band -> textbook freeze.
    result = detect_circuit_lock(
        open_price=120.0, high=120.0, low=120.0, close=120.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is True
    assert result["direction"] == "upper"
    assert result["full_day_freeze"] is True
    assert result["move_percent"] == 20.0


def test_upper_circuit_touched_but_not_full_day_freeze():
    # Closed at the day's high, +20% move, but open/low differ from
    # close -> circuit_likely True, full_day_freeze False (traded
    # during the day before locking, or an intraday-only touch).
    result = detect_circuit_lock(
        open_price=101.0, high=120.0, low=99.0, close=120.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is True
    assert result["direction"] == "upper"
    assert result["full_day_freeze"] is False


# ==========================================================
# detect_circuit_lock() — LOWER circuit (down-move) — mirror of above
# ==========================================================

def test_lower_circuit_full_day_freeze_detected():
    # -20% move, open==high==low==close at the band -> textbook freeze.
    result = detect_circuit_lock(
        open_price=80.0, high=80.0, low=80.0, close=80.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is True
    assert result["direction"] == "lower"
    assert result["full_day_freeze"] is True
    assert result["move_percent"] == -20.0


def test_lower_circuit_touched_but_not_full_day_freeze():
    result = detect_circuit_lock(
        open_price=99.0, high=101.0, low=80.0, close=80.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is True
    assert result["direction"] == "lower"
    assert result["full_day_freeze"] is False


# ==========================================================
# NO circuit — move not near any band, or closed away from the extreme
# ==========================================================

def test_no_circuit_when_move_not_near_any_band():
    # +8% move -> not within tolerance of 2/5/10/20.
    result = detect_circuit_lock(
        open_price=101.0, high=108.0, low=100.0, close=108.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is False
    assert result["direction"] is None


def test_no_circuit_when_near_band_but_not_closed_at_extreme():
    # +20% move but closed BELOW the day's high (e.g. spiked to a
    # circuit-adjacent level intraday and pulled back) -> not a lock.
    result = detect_circuit_lock(
        open_price=101.0, high=125.0, low=99.0, close=120.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is False


# ==========================================================
# band_percent=None -> falls back to checking ALL standard bands
# ==========================================================

def test_band_percent_none_falls_back_to_standard_bands():
    # Symbol's actual band is unknown (band_percent=None) but the move
    # (+5%) matches one of the 4 standard bands -> still detected.
    result = detect_circuit_lock(
        open_price=104.0, high=105.0, low=104.0, close=105.0, prev_close=100.0,
        band_percent=None,
    )
    assert result["circuit_likely"] is True
    assert result["band_used"] is None


def test_band_percent_provided_used_over_standard_bands():
    # Symbol's real assigned band is 10% (e.g. from sec_list.csv) and
    # the move (+10%) matches it precisely -> detected, band_used
    # reflects what was actually passed in.
    result = detect_circuit_lock(
        open_price=109.0, high=110.0, low=109.0, close=110.0, prev_close=100.0,
        band_percent=10.0,
    )
    assert result["circuit_likely"] is True
    assert result["band_used"] == 10.0


def test_band_percent_provided_move_does_not_match_it():
    # Symbol's assigned band is 5%, but the move is a +10% (would match
    # a STANDARD band, but that's not this symbol's real band) ->
    # correctly NOT flagged, since band_percent overrides the standard
    # fallback list entirely rather than adding to it.
    result = detect_circuit_lock(
        open_price=109.0, high=110.0, low=109.0, close=110.0, prev_close=100.0,
        band_percent=5.0,
    )
    assert result["circuit_likely"] is False


# ==========================================================
# Missing / invalid inputs — never raises, never fabricates a result
# ==========================================================

def test_missing_prev_close_returns_safe_default():
    result = detect_circuit_lock(
        open_price=100.0, high=100.0, low=100.0, close=100.0, prev_close=None,
    )
    assert result["circuit_likely"] is False
    assert result["move_percent"] == 0.0


def test_zero_prev_close_returns_safe_default_no_zero_division():
    result = detect_circuit_lock(
        open_price=100.0, high=100.0, low=100.0, close=100.0, prev_close=0.0,
    )
    assert result["circuit_likely"] is False


def test_missing_close_returns_safe_default():
    result = detect_circuit_lock(
        open_price=100.0, high=100.0, low=100.0, close=None, prev_close=100.0,
    )
    assert result["circuit_likely"] is False


def test_missing_high_low_does_not_crash_and_reports_no_circuit():
    # move matches a standard band, but high/low absent -> can't confirm
    # "closed at the extreme", must NOT guess.
    result = detect_circuit_lock(
        open_price=None, high=None, low=None, close=110.0, prev_close=100.0,
    )
    assert result["circuit_likely"] is False
    assert result["move_percent"] == 10.0


# ==========================================================
# module constants sanity (guards against silent accidental edits)
# ==========================================================

def test_standard_bands_are_the_four_nse_cash_market_bands():
    assert STANDARD_BANDS_PERCENT == (2.0, 5.0, 10.0, 20.0)


def test_default_tolerance_is_positive_and_small():
    assert 0.0 < DEFAULT_TOLERANCE_PERCENT < 2.0


# ==========================================================
# CircuitBandsProvider._parse_csv() — pure, no network
# ==========================================================

def test_parse_csv_valid_rows():
    text = (
        "SYMBOL,SERIES,SECURITY NAME,BAND,REMARKS\n"
        "ACC,EQ,ACC LIMITED,20,\n"
        "RELIANCE,EQ,RELIANCE INDUSTRIES,20,\n"
        "IDEA,EQ,VODAFONE IDEA,5,\n"
    )
    bands = CircuitBandsProvider._parse_csv(text)
    assert bands == {"ACC": 20.0, "RELIANCE": 20.0, "IDEA": 5.0}


def test_parse_csv_missing_expected_columns_returns_empty():
    text = "FOO,BAR\nX,Y\n"
    bands = CircuitBandsProvider._parse_csv(text)
    assert bands == {}


def test_parse_csv_skips_malformed_rows_without_crashing():
    text = (
        "SYMBOL,SERIES,SECURITY NAME,BAND,REMARKS\n"
        "ACC,EQ,ACC LIMITED,20,\n"
        "BROKENROW\n"  # too few columns -> skipped
        "IDEA,EQ,VODAFONE IDEA,NOTANUMBER,\n"  # bad band -> skipped
        "TCS,EQ,TATA CONSULTANCY,10,\n"
    )
    bands = CircuitBandsProvider._parse_csv(text)
    assert bands == {"ACC": 20.0, "TCS": 10.0}


def test_parse_csv_empty_text_returns_empty():
    assert CircuitBandsProvider._parse_csv("") == {}


# ==========================================================
# CircuitBandsProvider cache round-trip (disk, no network)
# ==========================================================

def test_cache_write_then_read_round_trip(tmp_path, monkeypatch):
    cache_file = tmp_path / "circuit_bands_cache.json"
    monkeypatch.setattr(
        "market.circuit_bands._CIRCUIT_BANDS_CACHE_PATH", str(cache_file)
    )

    bands = {"ACC": 20.0, "IDEA": 5.0}
    CircuitBandsProvider._write_cache(bands)

    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert payload["bands"] == bands
    assert "cached_on" in payload

    read_back = CircuitBandsProvider._read_cache()
    assert read_back == bands


def test_read_cache_missing_file_returns_none(tmp_path, monkeypatch):
    cache_file = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(
        "market.circuit_bands._CIRCUIT_BANDS_CACHE_PATH", str(cache_file)
    )
    assert CircuitBandsProvider._read_cache() is None


def test_read_cache_corrupt_file_returns_none_not_raise(tmp_path, monkeypatch):
    cache_file = tmp_path / "corrupt.json"
    cache_file.write_text("{not valid json")
    monkeypatch.setattr(
        "market.circuit_bands._CIRCUIT_BANDS_CACHE_PATH", str(cache_file)
    )
    assert CircuitBandsProvider._read_cache() is None


# ==========================================================
# CircuitBandsProvider.fetch_bands() — network mocked, exercises the
# retry -> failure -> cache-fallback path end to end.
# ==========================================================

class _FakeHeaders:
    def update(self, *_args, **_kwargs):
        pass


class _AlwaysFailingSession:
    """Stand-in for requests.Session() whose every .get() raises —
    simulates this sandbox's total network egress block (and, in real
    deployment, a genuine NSE-fetch outage)."""

    def __init__(self):
        self.headers = _FakeHeaders()

    def get(self, *_args, **_kwargs):
        raise ConnectionError("no network available")


def test_fetch_bands_falls_back_to_cache_when_network_unavailable(
    tmp_path, monkeypatch
):
    cache_file = tmp_path / "circuit_bands_cache.json"
    monkeypatch.setattr(
        "market.circuit_bands._CIRCUIT_BANDS_CACHE_PATH", str(cache_file)
    )
    CircuitBandsProvider._write_cache({"ACC": 20.0})

    provider = CircuitBandsProvider()

    monkeypatch.setattr(
        "market.circuit_bands.requests.Session", _AlwaysFailingSession
    )
    monkeypatch.setattr("market.circuit_bands.time.sleep", lambda *_a, **_k: None)

    result = provider.fetch_bands()
    assert result == {"ACC": 20.0}


def test_fetch_bands_returns_empty_when_network_unavailable_and_no_cache(
    tmp_path, monkeypatch
):
    cache_file = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(
        "market.circuit_bands._CIRCUIT_BANDS_CACHE_PATH", str(cache_file)
    )

    provider = CircuitBandsProvider()

    monkeypatch.setattr(
        "market.circuit_bands.requests.Session", _AlwaysFailingSession
    )
    monkeypatch.setattr("market.circuit_bands.time.sleep", lambda *_a, **_k: None)

    result = provider.fetch_bands()
    assert result == {}
