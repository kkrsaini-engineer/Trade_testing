"""
Tests for Phase 5's remaining audit items from BUG_AUDIT_2026-09-18.md
(HIGH #15, #17, #18 and MEDIUM #21, #23 — #22's BUY/SELL volatility_state
asymmetry was raised with the user as a design question, not changed, so
there is nothing behavioral to test for it here):

15. Fundamental data had no missing-rate monitoring for 12 of its 13
    fields (only "sector" was watched) — data/fundamental_data.py now
    tracks a running per-field missing count, and execution/scanner.py's
    scan_symbols() fires a CRITICAL alert when a field is missing across
    almost the whole scanned universe (signature of an upstream schema
    change, not normal per-symbol sparsity).
17. Delivery%/FII-DII/circuit-band disk caches had no max-age limit —
    data/delivery_data.py, data/fii_dii_data.py, market/circuit_bands.py
    now refuse a cache older than 7 calendar days instead of serving it
    forever.
18. Scanner's entry-scan path had no aggregate alert for "all/most
    symbols failing for the same reason" (unlike the monitoring path in
    paper_trading_engine.py) — orchestrator.py's new
    detect_entry_scan_systemic_failure() fills that gap.
21. strategy/sell_scoring.py's result.sector and strategy/sell_strategy.py's
    checks["sector"] + sector_component all used sector_score
    BUY-oriented (un-inverted) — now mirror the market_score inversion
    already applied in both files.
23. strategy/sell_strategy.py's MFI check overlapped BUY's at
    mfi_14 == 50 (both could be True) — SELL's upper bound is now
    exclusive.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from data.fundamental_data import FundamentalDataProvider
from execution.scanner import MarketScanner, ScanResult
from orchestrator import detect_entry_scan_systemic_failure
from strategy.sell_scoring import SellScoringEngine
from strategy.sell_strategy import SellDecision


# ==========================================================
# BUG #15 — fundamental field missing-rate tracking
# ==========================================================

class _FakeTicker:
    def __init__(self, info):
        self.info = info


_COMPLETE_INFO = {
    "marketCap": 1000.0, "trailingPE": 20.0, "priceToBook": 3.0, "pegRatio": 1.5,
    "returnOnEquity": 0.18, "debtToEquity": 0.5, "earningsGrowth": 0.1,
    "revenueGrowth": 0.1, "totalCash": 100.0, "operatingCashflow": 50.0,
    "ebitda": 200.0, "bookValue": 10.0, "sector": "Technology",
}


def test_missing_rate_report_none_below_min_symbols(monkeypatch):
    import data.fundamental_data as fd
    monkeypatch.setattr(fd.yf, "Ticker", lambda symbol: _FakeTicker(dict(_COMPLETE_INFO)))

    provider = FundamentalDataProvider()
    for i in range(3):  # fewer than the default min_symbols=5
        provider.fetch(f"SYM{i}.NS")

    assert provider.missing_rate_report() is None


def test_missing_rate_report_flags_universally_missing_field(monkeypatch):
    import data.fundamental_data as fd

    def fake_ticker(symbol):
        info = dict(_COMPLETE_INFO)
        info["returnOnEquity"] = None  # simulates Yahoo renaming/retiring this field
        return _FakeTicker(info)

    monkeypatch.setattr(fd.yf, "Ticker", fake_ticker)

    provider = FundamentalDataProvider()
    for i in range(6):
        provider.fetch(f"SYM{i}.NS")

    report = provider.missing_rate_report()
    assert report is not None
    assert report["roe"] == 1.0
    assert report["pe"] == 0.0  # a normally-present field stays at 0% missing


def test_missing_rate_report_does_not_flag_normal_sparse_data(monkeypatch):
    import data.fundamental_data as fd

    call_count = {"n": 0}

    def fake_ticker(symbol):
        info = dict(_COMPLETE_INFO)
        call_count["n"] += 1
        if call_count["n"] == 1:
            info["pegRatio"] = None  # only ONE symbol lacks PEG -- normal sparsity
        return _FakeTicker(info)

    monkeypatch.setattr(fd.yf, "Ticker", fake_ticker)

    provider = FundamentalDataProvider()
    for i in range(6):
        provider.fetch(f"SYM{i}.NS")

    report = provider.missing_rate_report()
    assert report["peg"] < 0.95  # 1/6 missing, well under the alert threshold


def test_reset_missing_rate_tracking_clears_counters(monkeypatch):
    import data.fundamental_data as fd
    monkeypatch.setattr(fd.yf, "Ticker", lambda symbol: _FakeTicker(dict(_COMPLETE_INFO)))

    provider = FundamentalDataProvider()
    for i in range(6):
        provider.fetch(f"SYM{i}.NS")
    assert provider.missing_rate_report() is not None

    provider.reset_missing_rate_tracking()
    assert provider.missing_rate_report() is None
    assert provider._fetch_count == 0


class _FakeFundamentalProvider:
    def __init__(self, report):
        self._report = report

    def missing_rate_report(self, min_symbols: int = 5):
        return self._report


class _FakeDataEngineWithFundamentalProvider:
    def __init__(self, fundamental_provider):
        self.fundamental_provider = fundamental_provider
        self.fetch_calls = []

    def fetch(self, symbol, **kwargs):
        self.fetch_calls.append(symbol)
        raise RuntimeError("no real bundle needed for this test")


def test_scan_symbols_fires_alert_when_field_missing_across_whole_universe(monkeypatch):
    import execution.scanner as scanner_module

    notified = []
    monkeypatch.setattr(
        scanner_module, "notify",
        lambda **kwargs: notified.append(kwargs) or True,
    )

    scanner = MarketScanner(disable_live_market_context=True)
    scanner.data_engine = _FakeDataEngineWithFundamentalProvider(
        _FakeFundamentalProvider({"roe": 1.0, "pe": 0.0})
    )
    monkeypatch.setattr(
        scanner, "scan_symbol",
        lambda **kwargs: ScanResult(
            symbol=kwargs["symbol"], action="NO_TRADE", score=0.0, probability=0.0,
            confidence=0.0, ranking=0.0, position_size=0, portfolio_allowed=False,
        ),
    )

    scanner.scan_symbols(
        ["A", "B", "C"], portfolio={}, broker_status={}, market_state={},
    )

    assert len(notified) == 1
    assert notified[0]["event_type"] == "fundamental_field_missing_rate"
    assert "roe" in notified[0]["message"]
    assert "pe" not in notified[0]["message"].split("\n")[1]  # only the broken field listed


def test_scan_symbols_does_not_alert_when_missing_rate_is_healthy(monkeypatch):
    import execution.scanner as scanner_module

    notified = []
    monkeypatch.setattr(
        scanner_module, "notify",
        lambda **kwargs: notified.append(kwargs) or True,
    )

    scanner = MarketScanner(disable_live_market_context=True)
    scanner.data_engine = _FakeDataEngineWithFundamentalProvider(
        _FakeFundamentalProvider({"roe": 0.1, "pe": 0.0})
    )
    monkeypatch.setattr(
        scanner, "scan_symbol",
        lambda **kwargs: ScanResult(
            symbol=kwargs["symbol"], action="NO_TRADE", score=0.0, probability=0.0,
            confidence=0.0, ranking=0.0, position_size=0, portfolio_allowed=False,
        ),
    )

    scanner.scan_symbols(["A", "B", "C"], portfolio={}, broker_status={}, market_state={})

    assert notified == []


# ==========================================================
# BUG #17 — disk-cache max-age limit
# ==========================================================

def test_delivery_data_cache_refuses_data_older_than_max_age(tmp_path, monkeypatch):
    import data.delivery_data as dd

    cache_file = tmp_path / "delivery_cache.json"
    monkeypatch.setattr(dd, "_DELIVERY_CACHE_PATH", str(cache_file))

    frozen_today = date(2026, 9, 18)
    stale_date = frozen_today - timedelta(days=dd._CACHE_MAX_AGE_DAYS + 1)
    monkeypatch.setattr(
        dd, "now_ist",
        lambda: datetime(frozen_today.year, frozen_today.month, frozen_today.day),
    )

    dd.DeliveryDataProvider._write_cache({"RELIANCE": {"delivery_percent": 55.0}}, stale_date)

    data, as_of = dd.DeliveryDataProvider._read_cache()
    assert data == {}
    assert as_of is None


def test_delivery_data_cache_still_used_within_max_age(tmp_path, monkeypatch):
    import data.delivery_data as dd

    cache_file = tmp_path / "delivery_cache.json"
    monkeypatch.setattr(dd, "_DELIVERY_CACHE_PATH", str(cache_file))

    recent_date = dd.now_ist().date() - timedelta(days=1)
    dd.DeliveryDataProvider._write_cache({"RELIANCE": {"delivery_percent": 55.0}}, recent_date)

    data, as_of = dd.DeliveryDataProvider._read_cache()
    assert data.get("RELIANCE", {}).get("delivery_percent") == 55.0
    assert as_of == recent_date


def test_fii_dii_cache_refuses_data_older_than_max_age(tmp_path, monkeypatch):
    import data.fii_dii_data as fdd

    cache_file = tmp_path / "fii_dii_cache.json"
    monkeypatch.setattr(fdd, "_FII_DII_CACHE_PATH", str(cache_file))

    stale_date = fdd.now_ist().date() - timedelta(days=fdd._CACHE_MAX_AGE_DAYS + 1)

    import json
    cache_file.write_text(json.dumps({
        "cached_on": stale_date.isoformat(),
        "data": {"fii_net_cr": 100.0},
    }))

    assert fdd.FiiDiiDataProvider._read_cache() is None


def test_fii_dii_cache_still_used_within_max_age(tmp_path, monkeypatch):
    import data.fii_dii_data as fdd

    cache_file = tmp_path / "fii_dii_cache.json"
    monkeypatch.setattr(fdd, "_FII_DII_CACHE_PATH", str(cache_file))

    fdd.FiiDiiDataProvider._write_cache({"fii_net_cr": 100.0})

    assert fdd.FiiDiiDataProvider._read_cache() == {"fii_net_cr": 100.0}


def test_circuit_bands_cache_refuses_data_older_than_max_age(tmp_path, monkeypatch):
    import market.circuit_bands as cb

    cache_file = tmp_path / "circuit_bands_cache.json"
    monkeypatch.setattr(cb, "_CIRCUIT_BANDS_CACHE_PATH", str(cache_file))

    stale_date = cb.now_ist().date() - timedelta(days=cb._CACHE_MAX_AGE_DAYS + 1)
    import json
    cache_file.write_text(json.dumps({
        "cached_on": stale_date.isoformat(),
        "bands": {"ACC": 20.0},
    }))

    assert cb.CircuitBandsProvider._read_cache() is None


def test_circuit_bands_cache_still_used_within_max_age(tmp_path, monkeypatch):
    import market.circuit_bands as cb

    cache_file = tmp_path / "circuit_bands_cache.json"
    monkeypatch.setattr(cb, "_CIRCUIT_BANDS_CACHE_PATH", str(cache_file))

    cb.CircuitBandsProvider._write_cache({"ACC": 20.0})

    assert cb.CircuitBandsProvider._read_cache() == {"ACC": 20.0}


# ==========================================================
# BUG #18 — entry-scan systemic-failure detection
# ==========================================================

def _error_result(symbol, error_type="DataError", stage="Data Fetch", error="boom"):
    return ScanResult(
        symbol=symbol, action="ERROR", score=0.0, probability=0.0, confidence=0.0,
        ranking=0.0, position_size=0, portfolio_allowed=False,
        diagnostics={"error_type": error_type, "error_stage": stage, "error": error},
    )


def _ok_result(symbol):
    return ScanResult(
        symbol=symbol, action="NO_TRADE", score=0.0, probability=0.0, confidence=0.0,
        ranking=0.0, position_size=0, portfolio_allowed=False,
    )


def test_no_alert_for_a_couple_of_unrelated_errors():
    candidates = [
        _error_result("A", error_type="DataError", stage="Data Fetch"),
        _error_result("B", error_type="ConnectionError", stage="Evaluation"),
        _ok_result("C"), _ok_result("D"), _ok_result("E"),
    ]
    assert detect_entry_scan_systemic_failure(candidates) is None


def test_alert_when_entire_scan_fails():
    candidates = [_error_result("A"), _error_result("B"), _error_result("C")]
    alert = detect_entry_scan_systemic_failure(candidates)
    assert alert is not None
    assert "3/3" in alert


def test_alert_when_all_errors_share_the_identical_cause_even_if_others_succeeded():
    candidates = [
        _error_result("A", error_type="SchemaError", stage="Data Fetch", error="field renamed"),
        _error_result("B", error_type="SchemaError", stage="Data Fetch", error="field renamed"),
        _error_result("C", error_type="SchemaError", stage="Data Fetch", error="field renamed"),
        _ok_result("D"), _ok_result("E"),
    ]
    alert = detect_entry_scan_systemic_failure(candidates)
    assert alert is not None
    assert "3/5" in alert
    assert "SchemaError" in alert


def test_no_alert_for_two_errors_with_the_same_cause_below_the_count_floor():
    # Only 2 errors, both same cause -- deliberately below the >=3 floor
    # so an isolated coincidence of 2 doesn't over-trigger.
    candidates = [
        _error_result("A", error_type="DataError", stage="Data Fetch"),
        _error_result("B", error_type="DataError", stage="Data Fetch"),
        _ok_result("C"), _ok_result("D"), _ok_result("E"),
    ]
    assert detect_entry_scan_systemic_failure(candidates) is None


def test_no_alert_when_there_are_no_errors_at_all():
    candidates = [_ok_result("A"), _ok_result("B")]
    assert detect_entry_scan_systemic_failure(candidates) is None


# ==========================================================
# BUG #21 — sell-side sector_score inversion
# ==========================================================

def _sell_score_row(**overrides):
    row = {
        "close": 100.0, "volume": 1_000_000, "volume_sma_20": 900_000,
        "atr_14": 2.0, "bb_width": 0.15, "gap_up": False, "rsi_14": 50.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_sell_scoring_sector_is_inverted_relative_to_raw_sector_score():
    engine = SellScoringEngine()
    decision = SellDecision(action="SELL", passed=True, confidence=50.0, tier2_score=60.0)

    strong_sector_result = engine.score(
        dataframe=_sell_score_row(), fundamentals={}, news_score=None,
        market_score=50.0, sector_score=90.0, sell_decision=decision,
    )
    weak_sector_result = engine.score(
        dataframe=_sell_score_row(), fundamentals={}, news_score=None,
        market_score=50.0, sector_score=10.0, sell_decision=decision,
    )

    # A STRONG sector (high raw sector_score) must score LOW here (bad
    # for a bearish/SELL setup); a WEAK sector must score HIGH.
    assert strong_sector_result.sector == pytest.approx(10.0)
    assert weak_sector_result.sector == pytest.approx(90.0)
    assert weak_sector_result.sector > strong_sector_result.sector


def test_sell_strategy_sector_gate_and_blend_are_inverted_in_source():
    # Same "source-guard regression test" technique already used for
    # Phase 3's fail-safe diagnostics-sync fixes -- checks["sector"] and
    # sector_component both live deep inside SellStrategyEngine.evaluate(),
    # a single large method requiring a full REQUIRED_COLUMNS dataframe
    # (EMA/RSI/ADX/ATR/MFI/CMF/breadth/... every technical column at
    # once) to integration-test even one branch of; asserting the exact
    # inverted expressions this fix introduced is precise and durable
    # without needing to reverse-engineer that whole fixture.
    from pathlib import Path
    source = Path("strategy/sell_strategy.py").read_text()

    assert 'checks["sector"] = sector_score is not None and sector_score <= 30' in source
    assert '(100.0 - min(max(sector_score, 0.0), 100.0)) * sector_weight' in source
    # Regression guard: the old un-inverted forms must be gone.
    assert 'checks["sector"] = sector_score is not None and sector_score >= 70' not in source


# ==========================================================
# BUG #23 — MFI boundary tie-break (source-guard, same reasoning as above)
# ==========================================================

def test_sell_strategy_mfi_upper_bound_is_now_exclusive():
    from pathlib import Path
    source = Path("strategy/sell_strategy.py").read_text()

    assert 'checks["mfi"] = 20 <= row["mfi_14"] < 50' in source
    assert 'checks["mfi"] = 20 <= row["mfi_14"] <= 50' not in source
