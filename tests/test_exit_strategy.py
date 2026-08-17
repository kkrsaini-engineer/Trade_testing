"""
Tests for risk/exit_strategy.py's ExitStrategyEngine — the trailing-stop /
break-even-stop / dual-target exit engine wired into paper trading in
Phase 19 (see PHASE19_NOTES.md), replacing risk/exit_engine.py's ExitEngine.

Covers, mirrored across BOTH BUY and SELL per the standing rule:
  - Trailing stop rises (BUY) / falls (SELL) with the position's peak,
    never regresses when price pulls back off the peak.
  - Break-even stop activates once profit clears BREAK_EVEN_TRIGGER.
  - Partial target -> PARTIAL_EXIT (50%); Final target -> FULL_EXIT.
  - Stop-loss hit via CLOSE price -> FULL_EXIT.
  - Intraday touch detection (day_low/day_high) catches a stop/target
    breach even when the close price recovered past it.
  - emergency_exit (risk-unsafe hard override) forces an immediate
    FULL_EXIT with the caller-supplied reason surfaced.
  - `sizing` is genuinely optional (never used internally).
"""

import pandas as pd

from decision.decision_engine import FinalDecision
from risk.exit_strategy import ExitStrategyEngine, HOLD, PARTIAL_EXIT, FULL_EXIT
from risk.risk_manager import RiskResult


def _decision(action):
    return FinalDecision(
        action=action,
        confidence=80.0,
        ranking=70.0,
        buy_score=75.0,
        sell_score=25.0,
        buy_probability=70.0,
        sell_probability=20.0,
        expected_return=5.0,
        expected_drawdown=2.0,
        expected_hold_days=5,
    )


def _risk(safe=True, total_risk=20.0, risk_grade="A"):
    return RiskResult(
        safe=safe,
        total_risk=total_risk,
        risk_grade=risk_grade,
        atr_risk=10.0,
        gap_risk=10.0,
        overnight_risk=10.0,
        news_risk=10.0,
        liquidity_risk=10.0,
        volatility_risk=10.0,
        portfolio_risk=10.0,
        sector_risk=10.0,
        correlation_risk=10.0,
        capital_risk=10.0,
    )


def _dataframe(close, atr=2.0, ema_20=100.0, ema_50=95.0, volatility_state="NORMAL"):
    row = {
        "close": close,
        "atr_14": atr,
        "ema_20": ema_20,
        "ema_50": ema_50,
        "volatility_state": volatility_state,
    }
    return pd.DataFrame([row])


def _position(
    direction,
    entry_price,
    current_price,
    highest_price=None,
    lowest_price=None,
    holding_days=5,
    day_high=None,
    day_low=None,
    emergency_exit=False,
    emergency_exit_reason=None,
    entry_thesis_confidence=None,
    held_thesis_confidence=None,
):
    pos = {
        "direction": direction,
        "entry_price": entry_price,
        "current_price": current_price,
        "holding_days": holding_days,
        "highest_price": highest_price if highest_price is not None else current_price,
        "lowest_price": lowest_price if lowest_price is not None else current_price,
        "day_high": day_high,
        "day_low": day_low,
        "emergency_exit": emergency_exit,
        "entry_thesis_confidence": entry_thesis_confidence,
        "held_thesis_confidence": held_thesis_confidence,
    }
    if emergency_exit_reason is not None:
        pos["emergency_exit_reason"] = emergency_exit_reason
    return pos


ENGINE = ExitStrategyEngine()


# ==========================================================
# TRAILING STOP — never regresses when price pulls back off the peak
# ==========================================================

def test_buy_trailing_stop_rises_with_peak_and_holds_on_pullback():
    # Day 1: price runs up to a peak of 130 (ATR=2 -> trailing = 130 - 3*2 = 124)
    decision = _decision("BUY")
    risk = _risk()
    df_peak = _dataframe(close=130.0, atr=2.0)
    pos_peak = _position(
        "BUY", entry_price=100.0, current_price=130.0, highest_price=130.0, lowest_price=100.0
    )
    result_peak = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df_peak, position=pos_peak)
    assert result_peak.trailing_stop == 124.0

    # Day 2: price pulls back to 120, but highest_price (from the caller,
    # sourced from VirtualPortfolio's monotonic peak tracking) still
    # reflects 130 — the trailing stop must NOT regress just because
    # today's close is lower.
    df_pullback = _dataframe(close=120.0, atr=2.0)
    pos_pullback = _position(
        "BUY", entry_price=100.0, current_price=120.0, highest_price=130.0, lowest_price=100.0
    )
    result_pullback = ENGINE.evaluate(
        decision=decision, risk=risk, dataframe=df_pullback, position=pos_pullback
    )
    assert result_pullback.trailing_stop == 124.0
    assert result_pullback.trailing_stop == result_peak.trailing_stop


def test_sell_trailing_stop_falls_with_trough_and_holds_on_bounce():
    decision = _decision("SELL")
    risk = _risk()
    df_trough = _dataframe(close=70.0, atr=2.0)
    pos_trough = _position(
        "SELL", entry_price=100.0, current_price=70.0, highest_price=100.0, lowest_price=70.0
    )
    result_trough = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df_trough, position=pos_trough)
    assert result_trough.trailing_stop == 76.0  # 70 + 3*2

    df_bounce = _dataframe(close=80.0, atr=2.0)
    pos_bounce = _position(
        "SELL", entry_price=100.0, current_price=80.0, highest_price=100.0, lowest_price=70.0
    )
    result_bounce = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df_bounce, position=pos_bounce)
    assert result_bounce.trailing_stop == 76.0


# ==========================================================
# BREAK-EVEN STOP
# ==========================================================

def test_buy_break_even_activates_after_trigger():
    decision = _decision("BUY")
    risk = _risk()
    # reward_multiple = (current - entry) / atr = (100 + 1.5*2 - 100) / 2 = 1.5 = trigger
    df = _dataframe(close=103.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=103.0, highest_price=103.0, lowest_price=100.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert "Break-even stop activated." in result.reasons
    assert result.diagnostics["stop_after_break_even"] >= 100.0


def test_sell_break_even_activates_after_trigger():
    decision = _decision("SELL")
    risk = _risk()
    df = _dataframe(close=97.0, atr=2.0)
    pos = _position(
        "SELL", entry_price=100.0, current_price=97.0, highest_price=100.0, lowest_price=97.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert "Break-even stop activated." in result.reasons
    assert result.diagnostics["stop_after_break_even"] <= 100.0


# ==========================================================
# DUAL TARGETS (partial / final) — close-price triggered
# ==========================================================

def test_buy_partial_target_triggers_partial_exit():
    decision = _decision("BUY")
    risk = _risk()
    # partial_target = entry + 2*atr = 100 + 4 = 104
    df = _dataframe(close=105.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=105.0, highest_price=105.0, lowest_price=100.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.exit_percent == 50.0


def test_sell_final_target_triggers_full_exit():
    decision = _decision("SELL")
    risk = _risk()
    # final_target = entry - 3.5*atr = 100 - 7 = 93
    df = _dataframe(close=92.0, atr=2.0)
    pos = _position(
        "SELL", entry_price=100.0, current_price=92.0, highest_price=100.0, lowest_price=92.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert result.exit_percent == 100.0
    assert "Final target achieved." in result.reasons


# ==========================================================
# STOP-LOSS HIT (close-price)
# ==========================================================

def test_buy_stop_hit_via_close_triggers_full_exit():
    decision = _decision("BUY")
    risk = _risk()
    # initial stop = entry - 2*atr = 100 - 4 = 96; trailing won't be
    # tighter than that here since price never ran up.
    df = _dataframe(close=95.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=95.0, highest_price=100.0, lowest_price=95.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert result.diagnostics["stop_hit"] is True


# ==========================================================
# INTRADAY TOUCH DETECTION — the "Port both" feature added in Phase 19
# ==========================================================

def test_buy_stop_touch_detected_intraday_even_if_close_recovered():
    decision = _decision("BUY")
    risk = _risk()
    # Close (98) is ABOVE the initial stop (96) — a close-only check
    # would HOLD. But the day's low (94) breached it intraday.
    df = _dataframe(close=98.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=98.0, highest_price=100.0, lowest_price=94.0,
        day_high=99.0, day_low=94.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert result.diagnostics["stop_hit"] is True
    assert result.suggested_exit_price == 94.0


def test_buy_stop_not_falsely_triggered_without_intraday_breach():
    decision = _decision("BUY")
    risk = _risk()
    df = _dataframe(close=98.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=98.0, highest_price=100.0, lowest_price=98.0,
        day_high=99.0, day_low=97.0,  # never touched 96
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == HOLD
    assert result.suggested_exit_price is None


def test_sell_stop_touch_detected_intraday_even_if_close_recovered():
    decision = _decision("SELL")
    risk = _risk()
    # initial stop = entry + 2*atr = 104. Close (102) is below it, but
    # the day's high (105) breached it intraday.
    df = _dataframe(close=102.0, atr=2.0)
    pos = _position(
        "SELL", entry_price=100.0, current_price=102.0, highest_price=105.0, lowest_price=100.0,
        day_high=105.0, day_low=101.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert result.diagnostics["stop_hit"] is True
    assert result.suggested_exit_price == 105.0


def test_buy_partial_target_touch_detected_intraday():
    decision = _decision("BUY")
    risk = _risk()
    # partial_target = 104. Close (103) never reached it, but the day's
    # high (106) touched it intraday.
    df = _dataframe(close=103.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=103.0, highest_price=106.0, lowest_price=100.0,
        day_high=106.0, day_low=101.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.suggested_exit_price == 106.0


# ==========================================================
# EMERGENCY EXIT (risk-unsafe hard override — the second "Port both" item)
# ==========================================================

def test_emergency_exit_forces_full_exit_with_custom_reason():
    decision = _decision("BUY")
    risk = _risk(safe=False, total_risk=90.0, risk_grade="D")
    df = _dataframe(close=101.0, atr=2.0)  # otherwise a plain HOLD
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        emergency_exit=True,
        emergency_exit_reason="Risk engine flagged this symbol as unsafe (grade: D).",
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert result.exit_percent == 100.0
    assert "Risk engine flagged this symbol as unsafe (grade: D)." in result.reasons


def test_emergency_exit_falls_back_to_generic_reason_when_unset():
    decision = _decision("BUY")
    risk = _risk()
    df = _dataframe(close=101.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        emergency_exit=True,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == FULL_EXIT
    assert "Emergency exit activated." in result.reasons


# ==========================================================
# sizing IS genuinely optional
# ==========================================================

def test_sizing_parameter_is_optional():
    decision = _decision("BUY")
    risk = _risk()
    df = _dataframe(close=101.0, atr=2.0)
    pos = _position("BUY", entry_price=100.0, current_price=101.0)
    # No `sizing` kwarg at all — must not raise.
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == HOLD


# ==========================================================
# HOLD case — plain, uneventful position
# ==========================================================

def test_hold_when_nothing_triggers():
    decision = _decision("BUY")
    risk = _risk()
    df = _dataframe(close=101.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=df, position=pos)
    assert result.action == HOLD
    assert result.exit_percent == 0.0


# ==========================================================
# CONFIDENCE REFINEMENT — Phase 21 point 17: decision.confidence (the
# entry-time decision engine's stale output) no longer feeds into the
# exit-time confidence number. See PHASE21_NOTES.md.
# ==========================================================

def _decision_with_confidence(action, confidence):
    return FinalDecision(
        action=action,
        confidence=confidence,
        ranking=70.0,
        buy_score=75.0,
        sell_score=25.0,
        buy_probability=70.0,
        sell_probability=20.0,
        expected_return=5.0,
        expected_drawdown=2.0,
        expected_hold_days=5,
    )


def test_buy_exit_confidence_unaffected_by_entry_decision_confidence():
    risk = _risk(total_risk=20.0)
    df = _dataframe(close=101.0, atr=2.0)
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0
    )
    result_low = ENGINE.evaluate(
        decision=_decision_with_confidence("BUY", 5.0), risk=risk, dataframe=df, position=pos
    )
    result_high = ENGINE.evaluate(
        decision=_decision_with_confidence("BUY", 99.0), risk=risk, dataframe=df, position=pos
    )
    assert result_low.confidence == result_high.confidence
    # HOLD base confidence (50.0) * 0.75 + (100 - 20.0) * 0.25 = 57.5
    assert result_low.confidence == 57.5


def test_sell_exit_confidence_unaffected_by_entry_decision_confidence():
    risk = _risk(total_risk=40.0)
    # ema_20 < ema_50 so trend_reversal (which checks ema_20 > ema_50 for
    # SELL) doesn't fire and this stays a plain HOLD, same as the BUY case.
    df = _dataframe(close=99.0, atr=2.0, ema_20=90.0, ema_50=95.0)
    pos = _position(
        "SELL", entry_price=100.0, current_price=99.0, highest_price=100.0, lowest_price=99.0
    )
    result_low = ENGINE.evaluate(
        decision=_decision_with_confidence("SELL", 5.0), risk=risk, dataframe=df, position=pos
    )
    result_high = ENGINE.evaluate(
        decision=_decision_with_confidence("SELL", 99.0), risk=risk, dataframe=df, position=pos
    )
    assert result_low.confidence == result_high.confidence
    # HOLD base confidence (50.0) * 0.75 + (100 - 40.0) * 0.25 = 52.5
    assert result_low.confidence == 52.5


# ==========================================================
# THESIS-DECAY TIME EXIT — Point 16, PHASE28_NOTES.md. Holding past
# MAX_HOLD_DAYS (30) is no longer, by itself, a forced-exit trigger —
# only genuine thesis decay (confidence dropped
# THESIS_DECAY_THRESHOLD=20+ points from its captured baseline) forces
# it, with HARD_CEILING_DAYS=150 as an unconditional safety net, and
# the OLD flat "holding_days >= 30" behavior preserved as a fallback
# for positions with no captured baseline (predate this fix).
# Every case below uses an otherwise-uneventful close price (no stop/
# target/trend/volatility/news trigger) so the TIME EXIT branch is
# isolated. Mirrored across BUY and SELL per the standing rule.
# ==========================================================

def _buy_hold_df():
    return _dataframe(close=101.0, atr=2.0)  # same as test_hold_when_nothing_triggers


def _sell_hold_df():
    # Same shape as test_sell_exit_confidence_unaffected...'s SELL HOLD case.
    return _dataframe(close=99.0, atr=2.0, ema_20=90.0, ema_50=95.0)


def test_buy_no_baseline_falls_back_to_old_flat_30_day_behavior():
    # Predates thesis-decay tracking: no baseline captured -> old flat
    # "holding_days >= MAX_HOLD_DAYS" rule still applies.
    decision = _decision("BUY")
    risk = _risk()
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        holding_days=30,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_buy_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.exit_percent == 50.0
    assert result.diagnostics["time_exit"] is True
    assert result.diagnostics["thesis_decayed"] is False
    assert result.diagnostics["hard_ceiling_reached"] is False
    assert result.diagnostics["exit_reason"] == "Maximum holding period."


def test_sell_no_baseline_falls_back_to_old_flat_30_day_behavior():
    decision = _decision("SELL")
    risk = _risk()
    pos = _position(
        "SELL", entry_price=100.0, current_price=99.0, highest_price=100.0, lowest_price=99.0,
        holding_days=30,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_sell_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.diagnostics["exit_reason"] == "Maximum holding period."


def test_buy_holds_past_30_days_when_thesis_has_not_decayed():
    # Baseline present, held confidence dropped only 10 points (< the
    # 20-point threshold) -> must NOT force an exit even past day 30.
    decision = _decision("BUY")
    risk = _risk()
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        holding_days=45, entry_thesis_confidence=70.0, held_thesis_confidence=60.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_buy_hold_df(), position=pos)
    assert result.action == HOLD
    assert result.diagnostics["time_exit"] is False
    assert result.diagnostics["thesis_decayed"] is False
    assert result.diagnostics["thesis_decay_points"] == 10.0


def test_buy_forces_exit_past_30_days_when_thesis_decays_20_points():
    decision = _decision("BUY")
    risk = _risk()
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        holding_days=30, entry_thesis_confidence=70.0, held_thesis_confidence=45.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_buy_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.exit_percent == 50.0
    assert result.diagnostics["thesis_decayed"] is True
    assert result.diagnostics["thesis_decay_points"] == 25.0
    assert result.diagnostics["exit_reason"] == "Thesis decayed beyond threshold."
    assert any("Thesis decayed" in r for r in result.reasons)


def test_sell_forces_exit_past_30_days_when_thesis_decays_20_points():
    decision = _decision("SELL")
    risk = _risk()
    pos = _position(
        "SELL", entry_price=100.0, current_price=99.0, highest_price=100.0, lowest_price=99.0,
        holding_days=30, entry_thesis_confidence=65.0, held_thesis_confidence=40.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_sell_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.diagnostics["thesis_decayed"] is True
    assert result.diagnostics["exit_reason"] == "Thesis decayed beyond threshold."


def test_buy_does_not_exit_before_30_days_even_with_large_decay():
    # Decay-checking only starts once MAX_HOLD_DAYS is reached, exactly
    # as before this change — a big confidence drop on day 10 shouldn't
    # force an early exit.
    decision = _decision("BUY")
    risk = _risk()
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        holding_days=10, entry_thesis_confidence=70.0, held_thesis_confidence=10.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_buy_hold_df(), position=pos)
    assert result.action == HOLD
    assert result.diagnostics["time_exit"] is False


def test_buy_hard_ceiling_forces_exit_even_without_thesis_decay():
    # Confidence unchanged (no decay at all) -- the hard ceiling still
    # unconditionally forces the exit once HARD_CEILING_DAYS is reached.
    decision = _decision("BUY")
    risk = _risk()
    pos = _position(
        "BUY", entry_price=100.0, current_price=101.0, highest_price=101.0, lowest_price=100.0,
        holding_days=150, entry_thesis_confidence=70.0, held_thesis_confidence=70.0,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_buy_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.diagnostics["thesis_decayed"] is False
    assert result.diagnostics["hard_ceiling_reached"] is True
    assert result.diagnostics["exit_reason"] == "Hard ceiling reached (150 days held)."


def test_sell_hard_ceiling_forces_exit_even_without_baseline():
    # No baseline captured at all (None/None) AND holding_days is well
    # past MAX_HOLD_DAYS -- both the flat fallback and the hard ceiling
    # agree here, but this specifically pins the hard-ceiling diagnostics
    # firing correctly even in the no-baseline path.
    decision = _decision("SELL")
    risk = _risk()
    pos = _position(
        "SELL", entry_price=100.0, current_price=99.0, highest_price=100.0, lowest_price=99.0,
        holding_days=150,
    )
    result = ENGINE.evaluate(decision=decision, risk=risk, dataframe=_sell_hold_df(), position=pos)
    assert result.action == PARTIAL_EXIT
    assert result.diagnostics["hard_ceiling_reached"] is True
    assert result.diagnostics["exit_reason"] == "Hard ceiling reached (150 days held)."
