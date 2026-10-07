"""
2026-10-06 — Daily Scan runtime fix.

Found while verifying uploads: the nightly scan (scripts/generate_full_
report.py) had not completed on a single trading day since 2026-09-22.
It ran 4h40m-5h54m through 2026-09-21 on a 2,395-symbol watchlist
(3 network calls per symbol) and GitHub Actions kills jobs at 6 hours —
so nothing was committed, reports/candidates_order.json stayed at the
2026-09-21 scan, and all 112 entries 2026-09-22..10-06 were that same
30-symbol list re-traded every morning.

Fix (user-approved, universe unchanged):
  1. scan the most liquid symbols first (real NSE turnover history);
  2. a time budget — stop starting new symbols after N minutes and still
     write the report + candidates for everything scanned;
  3. fundamentals (quarterly data) cached up to 7 days between runs.
"""

import json
import time
from pathlib import Path

import pytest
import yaml

import data.fundamental_data as fd
from data.fundamental_data import FundamentalDataProvider
from scripts.generate_full_report import order_by_liquidity


class _FakeTicker:
    calls = 0

    def __init__(self, symbol):
        self.symbol = symbol

    @property
    def info(self):
        _FakeTicker.calls += 1
        return {"trailingPE": 20.0, "sector": "Technology", "industry": "Software"}


@pytest.fixture
def fake_yf(monkeypatch):
    _FakeTicker.calls = 0
    monkeypatch.setattr(fd.yf, "Ticker", _FakeTicker)
    return _FakeTicker


# ==========================================================
# Fundamentals cache
# ==========================================================

def test_cache_is_off_by_default(fake_yf):
    provider = FundamentalDataProvider()
    provider.fetch("ABC.NS")
    provider.fetch("ABC.NS")
    assert fake_yf.calls == 2


def test_second_fetch_within_max_age_uses_the_cache(fake_yf, tmp_path):
    provider = FundamentalDataProvider()
    provider.enable_cache(str(tmp_path), max_age_days=7)

    first = provider.fetch("ABC.NS")
    second = FundamentalDataProvider()
    second.enable_cache(str(tmp_path), max_age_days=7)
    again = second.fetch("ABC.NS")

    assert fake_yf.calls == 1
    assert again == first
    assert second.cache_hits == 1


def test_expired_cache_is_refetched(fake_yf, tmp_path):
    provider = FundamentalDataProvider()
    provider.enable_cache(str(tmp_path), max_age_days=7)
    provider.fetch("ABC.NS")

    path = Path(tmp_path) / "ABC.NS.json"
    payload = json.loads(path.read_text())
    payload["fetched_at"] = time.time() - 8 * 86400
    path.write_text(json.dumps(payload))

    provider.fetch("ABC.NS")
    assert fake_yf.calls == 2


def test_corrupt_cache_file_falls_back_to_network(fake_yf, tmp_path):
    provider = FundamentalDataProvider()
    provider.enable_cache(str(tmp_path), max_age_days=7)
    (Path(tmp_path) / "ABC.NS.json").write_text("{not json")
    provider.fetch("ABC.NS")
    assert fake_yf.calls == 1


def test_cached_fetches_still_count_toward_missing_rate(fake_yf, tmp_path):
    provider = FundamentalDataProvider()
    provider.enable_cache(str(tmp_path), max_age_days=7)
    for _ in range(5):
        provider.fetch("ABC.NS")  # 1 network + 4 cache hits
    report = provider.missing_rate_report(min_symbols=5)
    assert report is not None
    assert report["pe"] == 0.0      # present every time
    assert report["roe"] == 1.0     # missing every time


# ==========================================================
# Liquidity-first ordering
# ==========================================================

def _hist(*turnovers):
    return [{"turnover_lacs": t} for t in turnovers]


def test_most_liquid_symbols_scan_first():
    history = {"AAA": _hist(10, 10), "BBB": _hist(500, 700), "CCC": _hist(50)}
    assert order_by_liquidity(["AAA.NS", "BBB.NS", "CCC.NS"], history) == ["BBB.NS", "CCC.NS", "AAA.NS"]


def test_symbols_without_history_keep_order_at_the_end():
    history = {"BBB": _hist(5)}
    assert order_by_liquidity(["ZZZ.NS", "BBB.NS", "AAA.NS"], history) == ["BBB.NS", "ZZZ.NS", "AAA.NS"]


def test_only_the_recent_window_counts():
    # AAA was liquid long ago, illiquid recently; BBB steady.
    history = {"AAA": _hist(*([10_000] * 30 + [1] * 20)), "BBB": _hist(*([100] * 50))}
    assert order_by_liquidity(["AAA.NS", "BBB.NS"], history) == ["BBB.NS", "AAA.NS"]


def test_ordering_never_adds_or_drops_symbols():
    symbols = [f"S{i}.NS" for i in range(50)]
    assert sorted(order_by_liquidity(symbols, {"S7": _hist(1)})) == sorted(symbols)


# ==========================================================
# Time budget + workflow cache (source guards — main() needs live data)
# ==========================================================

def test_scan_has_a_time_budget_and_records_truncation():
    source = Path("scripts/generate_full_report.py").read_text()
    assert 'SCAN_TIME_BUDGET_MINUTES = float(os.getenv("SCAN_TIME_BUDGET_MINUTES", "300"))' in source
    assert "if elapsed_minutes > SCAN_TIME_BUDGET_MINUTES:" in source
    assert '"scan_truncated": scan_truncated,' in source
    assert "scan_order = order_by_liquidity(list(WATCHLIST))" in source
    assert "fundamental_provider.enable_cache(FUNDAMENTALS_CACHE_DIR, FUNDAMENTALS_CACHE_MAX_AGE_DAYS)" in source


def test_daily_scan_workflow_restores_the_fundamentals_cache():
    workflow = yaml.safe_load(Path(".github/workflows/daily_scan.yml").read_text())
    steps = workflow["jobs"]["scan"]["steps"]
    cache_steps = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache@")]
    assert cache_steps and cache_steps[0]["with"]["path"] == "storage/cache/fundamentals"
    names = [s.get("name", s.get("uses")) for s in steps]
    assert names.index("Restore fundamentals cache") < names.index("Run Full Report Scan")
