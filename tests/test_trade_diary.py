"""
Tests for storage/trades/trade_diary.py's TradeDiary — specifically
capture_thesis_baseline() (Point 16, PHASE28_NOTES.md), the thesis-decay
time exit's real data source. Real live entries
(scripts/morning_executor.py) hardcode open_trade()'s buy_confidence to
0.0, so this method captures the first REAL held-direction confidence
value the monitoring loop computes instead, exactly once per trade.
"""

from storage.trades.trade_diary import TradeDiary


def _open(diary, trade_id="t1", **overrides):
    kwargs = dict(
        trade_id=trade_id, symbol="TESTCO.NS", direction="BUY", entry_price=100.0,
        entry_date="2026-01-01", buy_probability=70.0, buy_confidence=0.0,
        entry_reasons=["test entry"],
    )
    kwargs.update(overrides)
    diary.open_trade(**kwargs)


def test_open_trade_starts_with_no_baseline(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    _open(diary)
    record = diary.get_diary("t1")
    assert record["entry_thesis_confidence"] is None


def test_capture_thesis_baseline_writes_first_real_value(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    _open(diary)
    diary.capture_thesis_baseline("t1", 72.5)
    record = diary.get_diary("t1")
    assert record["entry_thesis_confidence"] == 72.5


def test_capture_thesis_baseline_is_a_noop_once_already_set(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    _open(diary)
    diary.capture_thesis_baseline("t1", 72.5)
    diary.capture_thesis_baseline("t1", 10.0)  # later, much lower -- must NOT overwrite
    record = diary.get_diary("t1")
    assert record["entry_thesis_confidence"] == 72.5


def test_capture_thesis_baseline_skips_none_without_permanently_blocking(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    _open(diary)
    diary.capture_thesis_baseline("t1", None)  # transient diagnostics gap
    assert diary.get_diary("t1")["entry_thesis_confidence"] is None
    diary.capture_thesis_baseline("t1", 55.0)  # next cycle has a real value
    assert diary.get_diary("t1")["entry_thesis_confidence"] == 55.0


def test_capture_thesis_baseline_unknown_trade_id_does_not_raise(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    diary.capture_thesis_baseline("does-not-exist", 50.0)  # must not raise


def test_capture_thesis_baseline_mirrors_for_sell_direction(tmp_path):
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    _open(diary, trade_id="t2", direction="SELL")
    diary.capture_thesis_baseline("t2", 40.0)
    record = diary.get_diary("t2")
    assert record["direction"] == "SELL"
    assert record["entry_thesis_confidence"] == 40.0
