"""
Tests for execution/scanner.py's scan_symbols() two-pass restructuring
(2026-09-18, STRUCTURAL BUY BIAS FIX — see strategy/fundamental_scoring.py's
module docstring for the full bug writeup).

Old behaviour: scan_symbols() looped once per symbol, and each symbol's
bundle was fetched lazily inside _evaluate_market_context() as that symbol
was evaluated. There was no point where "every symbol scanned today"'s
fundamentals existed all at once, so relative/percentile fundamental
scoring was impossible.

New behaviour: scan_symbols() now fetches every symbol's bundle FIRST
(pass 1, reusing anything the caller already supplied in `bundles`),
builds `universe_buy_fundamental_scores` from every fetched bundle's
`.fundamentals`, and only THEN runs the original per-symbol evaluation
loop (pass 2), passing that population down into every scan_symbol()
call and reusing the pass-1 bundle (no double fetch).

These tests isolate scan_symbols()'s own orchestration logic by faking
both `scanner.data_engine` (so no real network/provider calls happen)
and `scanner.scan_symbol` (so we don't need to scaffold the entire
feature/strategy/risk pipeline just to prove the pre-fetch/population/
reuse wiring is correct) -- following the same monkeypatch-scanner-
internals pattern used in tests/test_scanner_circuit_lock.py.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from execution.scanner import MarketScanner


@dataclass
class FakeBundle:
    symbol: str
    fundamentals: dict = field(default_factory=dict)


class FakeDataEngine:
    """Records fetch() calls; returns a canned bundle or raises."""

    def __init__(self, bundles: dict[str, FakeBundle], fail_symbols: set[str] | None = None):
        self._bundles = bundles
        self._fail_symbols = fail_symbols or set()
        self.fetch_calls: list[str] = []

    def fetch(self, symbol: str, **kwargs):
        self.fetch_calls.append(symbol)
        if symbol in self._fail_symbols:
            raise RuntimeError(f"simulated fetch failure for {symbol}")
        return self._bundles[symbol]


@pytest.fixture
def scanner():
    # disable_live_market_context avoids any incidental live fetches from
    # unrelated helper methods; scan_symbol itself is monkeypatched below
    # in every test so the real evaluation pipeline never runs.
    return MarketScanner(disable_live_market_context=True)


def _capture_scan_symbol_calls(scanner, monkeypatch):
    """Replace scanner.scan_symbol with a recorder; return the call list."""
    calls: list[dict[str, Any]] = []

    def fake_scan_symbol(self=None, **kwargs):
        # scan_symbol is a bound method, so when monkeypatched onto the
        # instance it is called without an explicit `self`.
        calls.append(kwargs)
        result = type("Result", (), {})()
        result.action = "NO_TRADE"
        result.portfolio_allowed = False
        return result

    monkeypatch.setattr(scanner, "scan_symbol", fake_scan_symbol)
    return calls


def test_pass_one_fetches_every_symbol_not_already_in_bundles(scanner, monkeypatch):
    bundles = {
        "AAA": FakeBundle("AAA", fundamentals={"pe": 20.0}),
        "BBB": FakeBundle("BBB", fundamentals={"pe": 25.0}),
    }
    fake_engine = FakeDataEngine(bundles)
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB"], portfolio={}, broker_status={}, market_state={})

    assert sorted(fake_engine.fetch_calls) == ["AAA", "BBB"]


def test_pass_one_skips_symbols_the_caller_already_supplied(scanner, monkeypatch):
    # AAA is pre-supplied by the caller -> pass 1 must NOT re-fetch it.
    # BBB is not supplied -> pass 1 must fetch it.
    preexisting = FakeBundle("AAA", fundamentals={"pe": 20.0})
    fake_engine = FakeDataEngine({"BBB": FakeBundle("BBB", fundamentals={"pe": 25.0})})
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(
        ["AAA", "BBB"],
        portfolio={},
        broker_status={},
        market_state={},
        bundles={"AAA": preexisting},
    )

    assert fake_engine.fetch_calls == ["BBB"]
    # Pass 2 must still receive AAA's caller-supplied bundle (reused, not
    # dropped) alongside BBB's freshly-fetched one.
    by_symbol = {c["symbol"]: c for c in calls}
    assert by_symbol["AAA"]["bundle"] is preexisting
    assert by_symbol["BBB"]["bundle"].symbol == "BBB"


def test_universe_buy_fundamental_scores_built_from_every_fetched_bundle(scanner, monkeypatch):
    from strategy.fundamental_scoring import buy_fundamental_score

    strong = {"revenue_growth": 10.0, "earnings_growth": 12.0, "roe": 18.0, "pe": 20.0}
    weak = {"revenue_growth": -5.0, "earnings_growth": -8.0, "roe": 2.0, "pe": 45.0}
    bundles = {
        "AAA": FakeBundle("AAA", fundamentals=strong),
        "BBB": FakeBundle("BBB", fundamentals=weak),
    }
    fake_engine = FakeDataEngine(bundles)
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB"], portfolio={}, broker_status={}, market_state={})

    expected = sorted([buy_fundamental_score(strong), buy_fundamental_score(weak)])
    for call in calls:
        assert sorted(call["universe_buy_fundamental_scores"]) == expected


def test_symbols_with_no_fundamentals_are_excluded_from_the_population(scanner, monkeypatch):
    bundles = {
        "AAA": FakeBundle("AAA", fundamentals={"pe": 20.0}),
        "BBB": FakeBundle("BBB", fundamentals={}),  # no fundamentals at all
    }
    fake_engine = FakeDataEngine(bundles)
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB"], portfolio={}, broker_status={}, market_state={})

    # Only AAA's score should be in the population; BBB contributed nothing.
    assert len(calls[0]["universe_buy_fundamental_scores"]) == 1


def test_a_symbol_whose_pass_one_fetch_fails_is_left_out_of_the_population(scanner, monkeypatch):
    bundles = {"AAA": FakeBundle("AAA", fundamentals={"pe": 20.0})}
    fake_engine = FakeDataEngine(bundles, fail_symbols={"BBB"})
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB"], portfolio={}, broker_status={}, market_state={})

    # Population only has AAA's score (BBB's fetch failed in pass 1).
    assert len(calls[0]["universe_buy_fundamental_scores"]) == 1
    # BBB must still be attempted again in pass 2 via scan_symbol's own
    # normal fetch-retry/error-reporting path -- i.e. scan_symbol() is
    # still called for BBB, just with no pre-fetched bundle.
    by_symbol = {c["symbol"]: c for c in calls}
    assert "BBB" in by_symbol
    assert by_symbol["BBB"]["bundle"] is None
    # And BBB's failed fetch must have been attempted exactly once in
    # pass 1 (no silent retry loop, no crash propagating out of
    # scan_symbols()).
    assert fake_engine.fetch_calls.count("BBB") == 1


def test_pass_two_reuses_pass_one_bundles_without_double_fetching(scanner, monkeypatch):
    bundles = {
        "AAA": FakeBundle("AAA", fundamentals={"pe": 20.0}),
        "BBB": FakeBundle("BBB", fundamentals={"pe": 25.0}),
    }
    fake_engine = FakeDataEngine(bundles)
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB"], portfolio={}, broker_status={}, market_state={})

    # Each symbol fetched exactly once overall (pass 1 only; pass 2 must
    # not trigger scan_symbol to fetch again since it is monkeypatched
    # here, but the real point is fetch_calls has no duplicates).
    assert fake_engine.fetch_calls.count("AAA") == 1
    assert fake_engine.fetch_calls.count("BBB") == 1


def test_scan_symbols_runs_with_no_data_engine_at_all(scanner, monkeypatch):
    # DataEngine import can fail in constrained environments -> data_engine
    # is None. scan_symbols() must not crash; it should just skip pass 1's
    # pre-fetch and build an empty population, exactly like the old
    # single-pass code would have with no bundles supplied.
    monkeypatch.setattr(scanner, "data_engine", None)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    # scan_symbols() only RETURNS executable (BUY/SELL, portfolio_allowed)
    # results -- our fake scan_symbol reports NO_TRADE/not-allowed, so the
    # return value is legitimately empty here. What matters for this test
    # is that scan_symbol() was still invoked once for the symbol (full
    # per-symbol result list is always cached on
    # scanner._last_full_scan_results regardless of executability).
    scanner.scan_symbols(["AAA"], portfolio={}, broker_status={}, market_state={})

    assert len(scanner._last_full_scan_results) == 1
    assert len(calls) == 1
    assert calls[0]["bundle"] is None
    assert calls[0]["universe_buy_fundamental_scores"] == []


def test_scan_symbols_return_value_and_call_count_unchanged_by_the_restructuring(scanner, monkeypatch):
    # Sanity: scan_symbols() must still call scan_symbol() exactly once
    # per input symbol, regardless of how many failed to fetch in pass 1
    # -- the two-pass rewrite must not change the fundamental
    # one-result-per-symbol contract. (The full per-symbol result list,
    # independent of executability, is what's asserted here; the
    # filtered/ranked return value is covered by pre-existing scanner
    # tests and is untouched by this fix.)
    bundles = {"AAA": FakeBundle("AAA", fundamentals={"pe": 20.0})}
    fake_engine = FakeDataEngine(bundles, fail_symbols={"BBB", "CCC"})
    monkeypatch.setattr(scanner, "data_engine", fake_engine)
    calls = _capture_scan_symbol_calls(scanner, monkeypatch)

    scanner.scan_symbols(["AAA", "BBB", "CCC"], portfolio={}, broker_status={}, market_state={})

    assert len(scanner._last_full_scan_results) == 3
    assert len(calls) == 3
