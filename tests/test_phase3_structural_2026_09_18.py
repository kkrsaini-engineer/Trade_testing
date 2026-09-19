"""
Tests for Phase 3's "structural/preventive" fixes from
BUG_AUDIT_2026-09-18.md:

9.  features/indicators/volatility.py: Bollinger Band std-dev used
    sample (ddof=1) instead of population (ddof=0) std-dev, ~2.6% too
    wide.
10. core/utils.py's write_json() (now used by
    paper_trading/virtual_portfolio.py and storage/trades/trade_diary.py)
    was not crash-safe (no temp-file + atomic rename).
11. strategy/sell_scoring.py's gap-up risk penalty used an undocumented,
    softer ratio (5:2:1) than buy_scoring.py's documented gap-down ratio
    (6:2:1) — rebalanced to match.
12. core/trading_calendar.py (+ data/delivery_data.py, data/fii_dii_data.py,
    market/circuit_bands.py) defaulted to the server's local/UTC date
    instead of IST.
13. analytics/learning_engine.py's win-rate helpers didn't exclude
    NaN-pnl trades from the denominator the way _accuracy() already did.
14. Fail-safe blocks in risk/portfolio_rules.py, risk/risk_manager.py,
    risk/position_sizing.py, risk/exit_strategy.py didn't sync their
    `diagnostics` dict to the corrected fail-safe values; and
    MAX_OPEN_POSITIONS/MAX_SECTOR_EXPOSURE/MAX_CORRELATION were
    duplicated between risk/portfolio_rules.py and
    decision/validation_engine.py instead of sharing one source.
"""

import json
import re
from pathlib import Path

import pandas as pd
import pytest

from analytics.learning_engine import LearningEngine
from core.constants import MAX_CORRELATION, MAX_OPEN_POSITIONS, MAX_SECTOR_EXPOSURE
from core.trading_calendar import is_trading_day, next_trading_day, previous_trading_day
from core.utils import write_json
from decision.validation_engine import ValidationEngine
from features.indicators.volatility import VolatilityIndicators
from risk.portfolio_rules import PortfolioRulesEngine
from strategy.buy_scoring import BuyScoringEngine
from strategy.sell_scoring import SellScoringEngine


# ==========================================================
# BUG #9 — Bollinger Band population std-dev
# ==========================================================

def test_bollinger_band_uses_population_not_sample_stddev():
    closes = [100 + i * 0.3 for i in range(30)]
    df_ohlc = pd.DataFrame({
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
    })
    df = VolatilityIndicators().calculate(df_ohlc)

    window = pd.Series(closes[-20:])
    population_std = window.std(ddof=0)
    sample_std = window.std(ddof=1)
    assert population_std != sample_std  # sanity: the two really differ

    sma = window.mean()
    expected_upper = sma + 2 * population_std
    assert df["bb_upper"].iloc[-1] == pytest.approx(expected_upper)

    wrong_upper_with_old_bug = sma + 2 * sample_std
    assert df["bb_upper"].iloc[-1] != pytest.approx(wrong_upper_with_old_bug)


# ==========================================================
# BUG #10 — atomic JSON writes
# ==========================================================

def test_write_json_produces_readable_correct_content(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"a": 1, "b": [1, 2, 3]})
    assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}


def test_write_json_never_leaves_a_temp_file_behind(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"x": 1})
    leftover = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftover == []


def test_write_json_does_not_touch_original_file_if_dump_fails(tmp_path):
    path = tmp_path / "state.json"
    write_json(path, {"good": "data"})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        write_json(path, {"bad": Unserializable()})

    # The original, valid content must be completely untouched -- this
    # is the entire point of write-to-temp-then-rename.
    assert json.loads(path.read_text()) == {"good": "data"}
    # And no stray temp file left behind after the failure either.
    leftover = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftover == []


def test_write_json_creates_parent_directories():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nested" / "dir" / "state.json"
        write_json(path, {"ok": True})
        assert json.loads(path.read_text()) == {"ok": True}


# ==========================================================
# BUG #11 — BUY/SELL gap-penalty symmetry
# ==========================================================

def test_buy_and_sell_gap_penalties_are_now_symmetric():
    buy_row = pd.Series({"gap_down": True, "rsi_14": 50, "volume": 1_000_000, "volume_sma_20": 500_000})
    sell_row = pd.Series({"gap_up": True, "rsi_14": 50, "volume": 1_000_000, "volume_sma_20": 500_000})

    buy_score = BuyScoringEngine()
    sell_score = SellScoringEngine()

    buy_risk = buy_score._risk_score(buy_row)
    sell_risk = sell_score._risk_score(sell_row)

    # Both sides penalize their mirrored gap event by the SAME amount
    # now (previously BUY's gap-down penalty was stricter than SELL's
    # gap-up penalty with no documented reason).
    assert buy_risk == pytest.approx(sell_risk)
    assert buy_risk == pytest.approx(100.0 - 66.67)


# ==========================================================
# BUG #12 — IST vs UTC date-boundary defaults
# ==========================================================

def test_is_trading_day_defaults_to_ist_today_not_utc(monkeypatch):
    import core.trading_calendar as tc
    # Freeze "now" so is_trading_day()'s default argument is
    # deterministic regardless of when this test actually runs.
    monkeypatch.setattr(tc, "now_ist", lambda: __import__("datetime").datetime(2026, 9, 16, 3, 0))
    assert is_trading_day() == tc.is_trading_day(tc.now_ist().date())


def test_previous_and_next_trading_day_default_to_ist_today(monkeypatch):
    import core.trading_calendar as tc
    from datetime import datetime
    monkeypatch.setattr(tc, "now_ist", lambda: datetime(2026, 9, 16, 3, 0))
    assert previous_trading_day() == tc.previous_trading_day(tc.now_ist().date())
    assert next_trading_day() == tc.next_trading_day(tc.now_ist().date())


def test_delivery_data_fii_dii_circuit_bands_use_now_ist_not_date_today():
    # Regression guard: the whole point of this fix is that these
    # modules must derive "today" from now_ist(), never from
    # date.today()/datetime.now() directly.
    for path in [
        "data/delivery_data.py",
        "data/fii_dii_data.py",
        "market/circuit_bands.py",
    ]:
        source = Path(path).read_text()
        assert "now_ist" in source, f"{path} should use now_ist()"
        # Ignore comment/docstring/prose mentions (e.g. explanatory
        # "used to be date.today()" BUGFIX notes) -- only flag an
        # actual assignment/live call of the form "x = date.today()".
        live_calls = re.findall(r"=\s*date\.today\(\)", source)
        assert live_calls == [], f"{path} should not call date.today(): {live_calls}"


# ==========================================================
# BUG #13 — learning-engine NaN-exclusion consistency
# ==========================================================

def test_win_rate_excludes_nan_pnls_from_numerator_and_denominator():
    engine = LearningEngine.__new__(LearningEngine)
    pnls = [100.0, -50.0, float("nan"), 200.0]
    # Old buggy behavior would have been 2/4 = 50.0 (NaN counted as a
    # loss in the denominator without ever counting as a win).
    # Correct: NaN excluded entirely -> 2 wins out of 3 classifiable.
    win_rate = engine._win_rate(pnls)
    assert win_rate == pytest.approx(2 / 3 * 100, abs=0.01)


def test_win_rate_returns_none_when_all_pnls_are_nan():
    engine = LearningEngine.__new__(LearningEngine)
    assert engine._win_rate([float("nan"), float("nan")]) is None


def test_sector_performance_excludes_nan_from_win_rate_but_not_from_trade_count():
    engine = LearningEngine.__new__(LearningEngine)
    engine.trade_store = None
    closed = [
        {"symbol": "A", "realized_pnl": 100.0},
        {"symbol": "A", "realized_pnl": -50.0},
        {"symbol": "A", "realized_pnl": float("nan")},
    ]
    report_by_symbol = {"A": {"Sector": "IT"}}
    result = engine._sector_performance(closed, report_by_symbol)
    assert result["IT"]["trades"] == 3  # total, NaN included (matches _accuracy()'s convention)
    assert result["IT"]["wins"] == 1
    assert result["IT"]["win_rate"] == pytest.approx(50.0)  # 1 win / 2 classifiable, NOT 1/3


def test_regime_performance_excludes_nan_from_win_rate_but_not_from_trade_count():
    engine = LearningEngine.__new__(LearningEngine)
    closed = [
        {"regime": "BULL", "realized_pnl": 100.0},
        {"regime": "BULL", "realized_pnl": float("nan")},
    ]
    result = engine._regime_performance(closed, {})
    assert result["BULL"]["trades"] == 2
    assert result["BULL"]["wins"] == 1
    assert result["BULL"]["win_rate"] == pytest.approx(100.0)  # 1/1 classifiable, not 1/2


# ==========================================================
# BUG #14a — fail-safe diagnostics sync (source-guard regression tests)
# ==========================================================
#
# These fail-safe branches exist specifically to catch "should never
# happen" internal invariant violations — by design there is no
# legitimate external input that reliably reaches them, so an
# integration-style test would have to reverse-engineer internal
# scoring-formula edge cases across 4 separate risk engines just to
# force entry. Instead, these assert precisely what the fix changed:
# that each fail-safe branch also re-writes the specific diagnostics
# keys that were already written earlier in the same method (the exact
# staleness the audit found).

@pytest.mark.parametrize(
    "path,fail_safe_diagnostics_keys",
    [
        ("risk/portfolio_rules.py", ["allowed", "portfolio_status", "allocation_allowed", "rejection_reason", "portfolio_score"]),
        ("risk/risk_manager.py", ["safe", "risk_grade"]),
        ("risk/position_sizing.py", ["quantity", "capital_to_use", "final_allocation_percent", "position_value"]),
        ("risk/exit_strategy.py", ["exit_percent"]),
    ],
)
def test_fail_safe_block_resyncs_diagnostics(path, fail_safe_diagnostics_keys):
    source = Path(path).read_text()
    fail_safe_block_match = re.search(
        r'diagnostics\["fail_safe"\] = True(.*?)(?:\n\s*else:)',
        source,
        re.DOTALL,
    )
    assert fail_safe_block_match is not None, f"Could not locate fail-safe block in {path}"
    block = fail_safe_block_match.group(1)
    for key in fail_safe_diagnostics_keys:
        assert f'diagnostics["{key}"]' in block, (
            f'{path}: diagnostics["{key}"] is not re-synced inside the fail-safe branch'
        )


# ==========================================================
# BUG #14b — duplicate constants consolidated
# ==========================================================

def test_portfolio_rules_and_validation_engine_share_the_same_constants():
    assert PortfolioRulesEngine.MAX_OPEN_POSITIONS == MAX_OPEN_POSITIONS
    assert PortfolioRulesEngine.MAX_SECTOR_EXPOSURE == MAX_SECTOR_EXPOSURE
    assert PortfolioRulesEngine.MAX_CORRELATION == MAX_CORRELATION

    assert ValidationEngine.MAX_OPEN_POSITIONS == MAX_OPEN_POSITIONS
    assert ValidationEngine.MAX_SECTOR_EXPOSURE == MAX_SECTOR_EXPOSURE
    assert ValidationEngine.MAX_CORRELATION == MAX_CORRELATION

    assert PortfolioRulesEngine.MAX_OPEN_POSITIONS is ValidationEngine.MAX_OPEN_POSITIONS
    assert PortfolioRulesEngine.MAX_SECTOR_EXPOSURE is ValidationEngine.MAX_SECTOR_EXPOSURE
    assert PortfolioRulesEngine.MAX_CORRELATION is ValidationEngine.MAX_CORRELATION
