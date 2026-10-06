"""
Exit Strategy Engine

Institutional Production Version

Responsibilities
----------------
Manage every open position until exit.

This engine NEVER generates BUY or SELL.

It only decides

HOLD
PARTIAL_EXIT
FULL_EXIT

Exit Methods

• Initial Stop Loss
• ATR Stop
• Trailing Stop
• Break-even Stop
• Time Exit
• Volatility Exit
• Profit Target
• Risk Exit
• Emergency Exit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from decision.decision_engine import FinalDecision
from risk.risk_manager import RiskResult
from risk.position_sizing import PositionSizingResult
from risk import stop_target

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# ACTIONS
# ==========================================================

HOLD = "HOLD"

PARTIAL_EXIT = "PARTIAL_EXIT"

FULL_EXIT = "FULL_EXIT"


# ==========================================================
# RESULT
# ==========================================================


@dataclass(slots=True)
class ExitDecision:

    action: str

    exit_percent: float

    stop_loss: float

    trailing_stop: float

    take_profit: float

    break_even_price: float

    expected_rr: float

    confidence: float

    reasons: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)

    # Set ONLY when a stop-loss or target was breached INTRADAY (via
    # day_low/day_high) — a real stop/target order fills at that touch
    # price, not at whatever price is current by the time this daily
    # check runs. When set, the caller should use THIS price to close
    # (or partially close) the position instead of current_price, so
    # realized P&L reflects what actually would have happened. Mirrors
    # risk/exit_engine.py's ExitEvaluation.suggested_exit_price.
    suggested_exit_price: float | None = None


# ==========================================================
# ENGINE
# ==========================================================


class ExitStrategyEngine:
    """
    Institutional Exit Engine
    """

    # Initial stop-loss / profit-target multipliers now live in
    # risk/stop_target.py's shared canonical formula (see
    # PHASE20_NOTES.md — previously duplicated 3 ways with no test to
    # catch drift). Aliased here so existing `self.ATR_STOP`-style
    # references and any external code that reads these class attributes
    # keep working unchanged.
    ATR_STOP = stop_target.ATR_STOP

    PARTIAL_TARGET = stop_target.PARTIAL_TARGET

    FINAL_TARGET = stop_target.FINAL_TARGET

    # Trailing-stop / break-even / time-exit behavior is genuinely UNIQUE
    # to this live exit engine (no equivalent in execution/scanner.py's
    # static display or risk/position_sizing.py's entry sizing) — these
    # stay local, not part of the shared module.
    ATR_TRAILING = 3.0

    BREAK_EVEN_TRIGGER = 1.5

    MAX_HOLD_DAYS = 30

    # Thesis-decay time exit (Point 16, PHASE28_NOTES.md). Holding past
    # MAX_HOLD_DAYS is no longer, by itself, a reason to force an exit —
    # a position can be held longer AS LONG AS the original thesis
    # (held-direction confidence) hasn't meaningfully weakened. Once
    # MAX_HOLD_DAYS is reached, we only force the exit if confidence has
    # dropped THESIS_DECAY_THRESHOLD points or more from the baseline
    # captured on the position's first monitoring cycle (see
    # storage/trades/trade_diary.py's capture_thesis_baseline()).
    THESIS_DECAY_THRESHOLD = 20.0

    # Absolute safety net — force the exit regardless of thesis strength
    # once a position has been held this many days, so a thesis that
    # never meaningfully decays can't keep a position open forever.
    HARD_CEILING_DAYS = 150

    @staticmethod
    def _realistic_fill_price(
        direction: str,
        kind: str,
        level: float,
        day_open: float | None,
        touched_intraday: bool,
        current_price: float,
    ) -> float:
        """
        BUGFIX (2026-10-06, BUG_AUDIT_2026-10-05_PROFITABILITY.md H2):
        the price a real resting stop / target order would have filled
        at. This used to be the bar's extreme — stops at the day's LOW,
        targets at the day's HIGH — which no real order gets (30 of 31
        real stop exits were filled worse than the market price at that
        moment; targets were booked above their own level).

        - Gapped THROUGH the level at the open (stop: open already past
          it; target: open already past it): fills at the OPEN.
        - Otherwise, touched intraday (day_low/day_high known): fills AT
          the level itself.
        - Only the close is known (no intraday high/low supplied):
          fills at the current price, the best available estimate —
          same as the old close-only behavior.

        kind is "stop" or "target"; direction is the HELD direction.
        """
        is_buy = str(direction).upper() == "BUY"

        if kind == "stop":
            gapped_through = day_open is not None and (
                day_open <= level if is_buy else day_open >= level
            )
        else:
            gapped_through = day_open is not None and (
                day_open >= level if is_buy else day_open <= level
            )

        if gapped_through:
            return float(day_open)

        if touched_intraday:
            return float(level)

        return float(current_price)

    def evaluate(
        self,
        decision: FinalDecision,
        risk: RiskResult,
        dataframe: pd.DataFrame,
        position: dict[str, Any],
        sizing: PositionSizingResult | None = None,
    ) -> ExitDecision:
        # `sizing` is accepted for interface parity with the entry-side
        # engines (and to leave room for a future sizing-aware exit rule,
        # e.g. scaling exit_percent by original position size) but is NOT
        # currently used anywhere in this method's body — monitoring an
        # already-open position never recomputes a fresh sizing result
        # (PositionSizingEngine is an entry-only concern; see
        # execution/scanner.py's evaluate_position() docstring). Optional
        # so callers that only monitor never need to fabricate one.

        latest = dataframe.iloc[-1]

        reasons = []

        diagnostics = {}

        logger.info("Starting Exit Strategy.")

        entry_price = float(
            position.get(
                "entry_price",
                latest["close"],
            )
        )

        current_price = float(latest["close"])

        atr = float(
            latest.get(
                "atr_14",
                0.0,
            )
        )

        holding_days = int(
            position.get(
                "holding_days",
                0,
            )
        )

        # Thesis-decay time exit inputs (Point 16, PHASE28_NOTES.md).
        # Both are Optional[float] — None when no real baseline has been
        # captured yet for this position (e.g. it predates this fix, or
        # this is its very first monitoring cycle). See the TIME EXIT
        # section below for how a missing value is handled.
        entry_thesis_confidence = position.get("entry_thesis_confidence")

        held_thesis_confidence = position.get("held_thesis_confidence")

        highest_price = float(
            position.get(
                "highest_price",
                current_price,
            )
        )

        lowest_price = float(
            position.get(
                "lowest_price",
                current_price,
            )
        )

        # Intraday high/low for the CURRENT bar — optional. When supplied,
        # stop-loss / target checks below use these to catch a genuine
        # intraday touch even if the close price later recovered, same
        # rationale as risk/exit_engine.py's day_high/day_low handling.
        # None (not supplied) falls back to close-only comparison.
        day_high = position.get("day_high")
        day_low = position.get("day_low")
        day_high = float(day_high) if day_high is not None else None
        day_low = float(day_low) if day_low is not None else None

        # 2026-10-06 (BUG_AUDIT_2026-10-05_PROFITABILITY.md H2): today's
        # OPEN, used only to price a realistic fill when a stop/target is
        # gapped through (see _realistic_fill_price()). None = unknown.
        day_open = position.get("day_open")
        day_open = float(day_open) if day_open is not None else None

        # 2026-10-06 (audit H3): True once target1 has already been booked
        # for this position (persisted on PortfolioPosition.partial_taken).
        partial_taken = bool(position.get("partial_taken", False))

        # 2026-10-06 (audit H4/H5): the tightest stop this position has
        # ever had (persisted on PortfolioPosition.stop_level). None for a
        # position that has no recorded stop yet.
        persisted_stop = position.get("stop_level")
        persisted_stop = float(persisted_stop) if persisted_stop is not None else None

        # 2026-10-06 (audit H1): the PREVIOUS full session's range, supplied
        # only when the position was already held during that session
        # (see paper_trading_engine.py). Monitoring runs once a day at
        # ~9:20, so without this everything that happened after 9:20
        # yesterday was never checked against the stop/targets.
        prev_day_open = position.get("prev_day_open")
        prev_day_high = position.get("prev_day_high")
        prev_day_low = position.get("prev_day_low")
        prev_day_open = float(prev_day_open) if prev_day_open is not None else None
        prev_day_high = float(prev_day_high) if prev_day_high is not None else None
        prev_day_low = float(prev_day_low) if prev_day_low is not None else None

        # ==========================================================
        # INITIAL STOP LOSS + PROFIT TARGETS
        # ==========================================================
        # Both computed together via risk/stop_target.py's shared
        # canonical formula — SAME function execution/scanner.py's
        # static display and risk/position_sizing.py's sizing use, so
        # this live engine's initial levels always match what those
        # show/assume. (Trailing stop / break-even below are this
        # engine's own genuinely separate behavior, computed locally.)
        stop_loss, partial_target, final_target = stop_target.compute_stop_loss_targets(
            direction=decision.action, close_price=entry_price, atr=atr,
        )

        # Kept before break-even/trailing adjust stop_loss below.
        initial_stop = stop_loss

        diagnostics["initial_stop_loss"] = round(
            stop_loss,
            2,
        )

        # ==========================================================
        # TRAILING STOP
        # ==========================================================

        if decision.action == "BUY":

            trailing_stop = max(stop_loss, highest_price - (atr * self.ATR_TRAILING))

        else:

            trailing_stop = min(stop_loss, lowest_price + (atr * self.ATR_TRAILING))

        diagnostics["trailing_stop"] = round(
            trailing_stop,
            2,
        )

        # ==========================================================
        # BREAK-EVEN STOP
        # ==========================================================

        if decision.action == "BUY":

            reward_multiple = current_price - entry_price

            reward_multiple /= max(
                atr,
                0.01,
            )

            if reward_multiple >= self.BREAK_EVEN_TRIGGER:

                stop_loss = max(
                    stop_loss,
                    entry_price,
                )

                reasons.append("Break-even stop activated.")

        else:

            reward_multiple = entry_price - current_price

            reward_multiple /= max(
                atr,
                0.01,
            )

            if reward_multiple >= self.BREAK_EVEN_TRIGGER:

                stop_loss = min(
                    stop_loss,
                    entry_price,
                )

                reasons.append("Break-even stop activated.")

        diagnostics["reward_multiple"] = round(
            reward_multiple,
            2,
        )

        diagnostics["stop_after_break_even"] = round(
            stop_loss,
            2,
        )

        # ==========================================================
        # ACTIVE STOP
        # ==========================================================

        if decision.action == "BUY":

            active_stop = max(
                stop_loss,
                trailing_stop,
            )

        else:

            active_stop = min(
                stop_loss,
                trailing_stop,
            )

        # BUGFIX (2026-10-06, audit H4/H5): the stop may only TIGHTEN.
        # Everything above is recomputed from scratch each run with
        # today's ATR — so a volatility spike widened the stop, and a
        # break-even stop disappeared the moment price dipped back below
        # the trigger (real data: MCX 5.5% MFE closed at +0.39%, GRANULES
        # 5.29% MFE closed at +0.10%). Flooring by the persisted level
        # fixes both: once break-even (or a trailing level) has been
        # reached, the stop never goes back below it.
        diagnostics["computed_stop_before_floor"] = round(active_stop, 2)

        if persisted_stop is not None:

            if decision.action == "BUY":

                active_stop = max(active_stop, persisted_stop)

            else:

                active_stop = min(active_stop, persisted_stop)

        # Not rounded: the caller persists this exact value back to
        # PortfolioPosition.stop_level, and rounding a BUY stop down / a
        # SELL stop up would let it drift looser by a fraction each day.
        diagnostics["active_stop"] = active_stop
        # ==========================================================
        # PROFIT TARGETS (diagnostics only — values computed above
        # alongside the initial stop-loss, same shared-formula call)
        # ==========================================================

        diagnostics["partial_target"] = round(
            partial_target,
            2,
        )

        diagnostics["final_target"] = round(
            final_target,
            2,
        )

        # ==========================================================
        # PARTIAL EXIT
        # ==========================================================

        partial_exit = False

        # Use the day's HIGH (BUY) / LOW (SELL) when available — catches a
        # genuine intraday touch of the target even if the close later
        # pulled back below/above it, same day_high/day_low convention as
        # risk/exit_engine.py. Falls back to close-only when not supplied.
        partial_touch_price = current_price

        if decision.action == "BUY":

            partial_touch_price = day_high if day_high is not None else current_price

            if partial_touch_price >= partial_target:

                partial_exit = True

        else:

            partial_touch_price = day_low if day_low is not None else current_price

            if partial_touch_price <= partial_target:

                partial_exit = True

        # BUGFIX (2026-10-06, audit H3): target1 is a ONE-TIME booking.
        # Without this, every later day the price stayed past target1
        # sold another 50% of what was left.
        diagnostics["partial_already_taken"] = partial_taken

        if partial_taken:

            partial_exit = False

        diagnostics["partial_exit"] = partial_exit

        if partial_exit:

            reasons.append("Partial profit target reached.")

        # ==========================================================
        # FINAL EXIT
        # ==========================================================

        final_exit = False

        final_touch_price = current_price

        if decision.action == "BUY":

            final_touch_price = day_high if day_high is not None else current_price

            if final_touch_price >= final_target:

                final_exit = True

        else:

            final_touch_price = day_low if day_low is not None else current_price

            if final_touch_price <= final_target:

                final_exit = True

        diagnostics["final_exit"] = final_exit

        if final_exit:

            reasons.append("Final profit target reached.")

        # ==========================================================
        # RISK / REWARD
        # ==========================================================

        risk_distance = abs(entry_price - active_stop)

        reward_distance = abs(final_target - entry_price)

        expected_rr = reward_distance / max(
            risk_distance,
            0.01,
        )

        expected_rr = round(
            expected_rr,
            2,
        )

        diagnostics["risk_distance"] = round(
            risk_distance,
            2,
        )

        diagnostics["reward_distance"] = round(
            reward_distance,
            2,
        )

        diagnostics["expected_rr"] = expected_rr

        if expected_rr >= 3.0:

            reasons.append("Excellent risk/reward profile.")

        elif expected_rr >= 2.0:

            reasons.append("Healthy risk/reward profile.")

        else:

            reasons.append("Below preferred risk/reward profile.")
        # ==========================================================
        # TIME EXIT — thesis-decay based (Point 16, PHASE28_NOTES.md)
        # ==========================================================
        # MAX_HOLD_DAYS is no longer, by itself, a forced-exit trigger.
        # Once reached, we only force the exit if the held-direction
        # thesis has genuinely decayed (confidence dropped
        # THESIS_DECAY_THRESHOLD+ points from its baseline). If no real
        # baseline is available for this position (predates thesis-decay
        # tracking, or this is literally its first monitoring cycle),
        # we fall back to the OLD flat "holding_days >= MAX_HOLD_DAYS"
        # behavior — a thesis-strength check with no thesis data to
        # check against would be meaningless, and silently never
        # forcing an exit would leave a stale position open forever.
        # HARD_CEILING_DAYS applies unconditionally either way, as an
        # absolute safety net.

        thesis_decay_points = None

        thesis_decayed = False

        if entry_thesis_confidence is not None and held_thesis_confidence is not None:

            thesis_decay_points = entry_thesis_confidence - held_thesis_confidence

            thesis_decayed = thesis_decay_points >= self.THESIS_DECAY_THRESHOLD

        hard_ceiling_reached = holding_days >= self.HARD_CEILING_DAYS

        has_baseline = entry_thesis_confidence is not None and held_thesis_confidence is not None

        if has_baseline:

            time_exit = (holding_days >= self.MAX_HOLD_DAYS and thesis_decayed) or hard_ceiling_reached

        else:

            time_exit = holding_days >= self.MAX_HOLD_DAYS or hard_ceiling_reached

        diagnostics["time_exit"] = time_exit

        diagnostics["thesis_decay_points"] = (
            round(thesis_decay_points, 2) if thesis_decay_points is not None else None
        )

        diagnostics["thesis_decayed"] = thesis_decayed

        diagnostics["hard_ceiling_reached"] = hard_ceiling_reached

        if time_exit:

            if hard_ceiling_reached:

                reasons.append(f"Hard ceiling reached ({self.HARD_CEILING_DAYS} days held).")

            elif thesis_decayed:

                reasons.append(
                    f"Thesis decayed {thesis_decay_points:.1f} points "
                    f"after {holding_days} days held."
                )

            else:

                reasons.append("Maximum holding period reached.")

        # ==========================================================
        # VOLATILITY EXIT
        # ==========================================================

        volatility_state = str(
            latest.get(
                "volatility_state",
                "NORMAL",
            )
        ).upper()

        volatility_exit = volatility_state == "HIGH"

        diagnostics["volatility_exit"] = volatility_exit

        if volatility_exit:

            reasons.append("High market volatility.")

        # ==========================================================
        # NEWS EXIT
        # ==========================================================

        news_exit = bool(
            position.get(
                "negative_news",
                False,
            )
        )

        diagnostics["news_exit"] = news_exit

        if news_exit:

            reasons.append("Negative news detected.")

        # ==========================================================
        # EMERGENCY EXIT
        # ==========================================================

        emergency_exit = bool(
            position.get(
                "emergency_exit",
                False,
            )
        )

        # Caller-supplied reason for the emergency flag (e.g. "Risk engine
        # flagged this symbol as unsafe (grade: D)."), so the FULL_EXIT
        # this triggers below carries the SAME specific explanation the
        # old risk/exit_engine.py's hard-risk override gave, instead of a
        # generic label. Falls back to a generic message if the caller
        # didn't supply one.
        emergency_exit_reason = position.get(
            "emergency_exit_reason",
            "Emergency exit activated.",
        )

        diagnostics["emergency_exit"] = emergency_exit

        if emergency_exit:

            reasons.append(emergency_exit_reason)

        # ==========================================================
        # TREND REVERSAL EXIT
        # ==========================================================

        trend_reversal = False

        if decision.action == "BUY":

            if latest["ema_20"] < latest["ema_50"]:

                trend_reversal = True

        else:

            if latest["ema_20"] > latest["ema_50"]:

                trend_reversal = True

        diagnostics["trend_reversal"] = trend_reversal

        if trend_reversal:

            reasons.append("Trend reversal detected.")

        # ==========================================================
        # STOP HIT
        # ==========================================================

        stop_hit = False

        # Same intraday-touch convention as the target checks above — the
        # day's LOW (BUY) / HIGH (SELL) catches a genuine stop breach even
        # if price recovered by the close.
        stop_touch_price = current_price

        if decision.action == "BUY":

            stop_touch_price = day_low if day_low is not None else current_price

            stop_hit = stop_touch_price <= active_stop

        else:

            stop_touch_price = day_high if day_high is not None else current_price

            stop_hit = stop_touch_price >= active_stop

        diagnostics["stop_hit"] = stop_hit

        if stop_hit:

            reasons.append("Active stop-loss triggered.")

        # ==========================================================
        # PREVIOUS-SESSION TOUCHES (2026-10-06, audit H1)
        # ==========================================================
        # A resting stop / target order would have filled DURING the
        # previous session if its range crossed the level. The stop that
        # was live then is the persisted one (yesterday's active stop) —
        # not today's, which may already trail yesterday's high (using
        # it would be look-ahead within that bar). Falls back to the
        # initial stop for a position with no persisted stop yet.
        prev_stop_level = persisted_stop if persisted_stop is not None else initial_stop
        is_buy = decision.action == "BUY"

        if is_buy:
            prev_stop_hit = prev_day_low is not None and prev_day_low <= prev_stop_level
            prev_final_hit = prev_day_high is not None and prev_day_high >= final_target
            prev_partial_hit = prev_day_high is not None and prev_day_high >= partial_target
        else:
            prev_stop_hit = prev_day_high is not None and prev_day_high >= prev_stop_level
            prev_final_hit = prev_day_low is not None and prev_day_low <= final_target
            prev_partial_hit = prev_day_low is not None and prev_day_low <= partial_target

        prev_partial_hit = prev_partial_hit and not partial_taken

        diagnostics["prev_session_stop_hit"] = prev_stop_hit
        diagnostics["prev_session_final_hit"] = prev_final_hit
        diagnostics["prev_session_partial_hit"] = prev_partial_hit

        if prev_stop_hit:
            diagnostics["stop_hit"] = True
            reasons.append("Stop-loss was touched in the previous session.")
        # ==========================================================
        # EXIT PRIORITY ENGINE
        # ==========================================================

        action = HOLD

        exit_percent = 0.0

        confidence = 50.0

        exit_reason = "Continue holding position."

        # Only set for a stop/target branch below, where an actual
        # intraday touch price (rather than just the close) is known —
        # carried into the final ExitDecision.suggested_exit_price.
        resolved_exit_price: float | None = None

        # ==========================================================
        # EMERGENCY EXIT
        # ==========================================================

        if emergency_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 100.0

            exit_reason = emergency_exit_reason

        # ==========================================================
        # PREVIOUS SESSION (2026-10-06, audit H1) — these happened
        # BEFORE anything in today's bar, so they are resolved first.
        # If both the stop and a target fall inside the same previous
        # range, the order within that bar is unknown: the stop is
        # assumed (conservative), same as today's priority below.
        # ==========================================================

        elif prev_stop_hit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 99.0

            exit_reason = "Stop-loss triggered (previous session)."

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="stop", level=prev_stop_level,
                day_open=prev_day_open, touched_intraday=True, current_price=current_price,
            )

        elif prev_final_hit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 95.0

            exit_reason = "Final target achieved (previous session)."

            diagnostics["final_exit"] = True

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="target", level=final_target,
                day_open=prev_day_open, touched_intraday=True, current_price=current_price,
            )

        elif prev_partial_hit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 85.0

            exit_reason = "Partial target achieved (previous session)."

            diagnostics["partial_exit"] = True

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="target", level=partial_target,
                day_open=prev_day_open, touched_intraday=True, current_price=current_price,
            )

        # ==========================================================
        # STOP LOSS EXIT
        # ==========================================================

        elif stop_hit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 99.0

            exit_reason = "Stop-loss triggered."

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="stop", level=active_stop,
                day_open=day_open,
                touched_intraday=(day_low if decision.action == "BUY" else day_high) is not None,
                current_price=current_price,
            )

        # ==========================================================
        # FINAL TARGET
        # ==========================================================
        # Profit-target checks were moved ABOVE trend_reversal/news/
        # volatility/time exits (they used to sit below all of them).
        # Previously, a noisy single-bar trend_reversal (ema_20 <
        # ema_50 for BUY) could FULL_EXIT a position the moment it
        # crossed, even on the exact bar it also reached final_target —
        # capping the win at whatever trend_reversal's confidence/label
        # was instead of recording it as a target hit. Stop-loss still
        # protects capital first (unchanged above); once capital is
        # safe, "did we already win" is checked before "is the trend
        # getting shaky", so a trade that reached its target is never
        # silently reclassified as a reversal-exit.

        elif final_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 95.0

            exit_reason = "Final target achieved."

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="target", level=final_target,
                day_open=day_open,
                touched_intraday=(day_high if decision.action == "BUY" else day_low) is not None,
                current_price=current_price,
            )

        # ==========================================================
        # PARTIAL TARGET
        # ==========================================================

        elif partial_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 85.0

            exit_reason = "Partial target achieved."

            resolved_exit_price = self._realistic_fill_price(
                direction=decision.action, kind="target", level=partial_target,
                day_open=day_open,
                touched_intraday=(day_high if decision.action == "BUY" else day_low) is not None,
                current_price=current_price,
            )

        # ==========================================================
        # TREND REVERSAL
        # ==========================================================
        # Still fires BEFORE news/volatility/time exits — a confirmed
        # reversal that hasn't yet reached a profit target is real risk
        # and should exit ahead of softer signals, just not ahead of a
        # target that's already been hit (see above).

        elif trend_reversal:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 90.0

            exit_reason = "Trend reversal confirmed."

        # ==========================================================
        # NEWS EXIT
        # ==========================================================

        elif news_exit:

            action = FULL_EXIT

            exit_percent = 100.0

            confidence = 90.0

            exit_reason = "Negative news risk."

        # ==========================================================
        # VOLATILITY EXIT
        # ==========================================================

        elif volatility_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 80.0

            exit_reason = "High market volatility."

        # ==========================================================
        # TIME EXIT
        # ==========================================================

        elif time_exit:

            action = PARTIAL_EXIT

            exit_percent = 50.0

            confidence = 75.0

            if hard_ceiling_reached:

                exit_reason = f"Hard ceiling reached ({self.HARD_CEILING_DAYS} days held)."

            elif thesis_decayed:

                exit_reason = "Thesis decayed beyond threshold."

            else:

                exit_reason = "Maximum holding period."

        # ==========================================================
        # HOLD
        # ==========================================================

        reasons.append(exit_reason)

        diagnostics["exit_action"] = action

        diagnostics["exit_percent"] = exit_percent

        diagnostics["exit_confidence"] = confidence

        diagnostics["exit_reason"] = exit_reason
        # ==========================================================
        # DYNAMIC STOP ADJUSTMENT
        # ==========================================================

        if action == HOLD:

            if decision.action == "BUY":

                dynamic_stop = max(
                    active_stop,
                    current_price - (atr * 2.0),
                )

            else:

                dynamic_stop = min(
                    active_stop,
                    current_price + (atr * 2.0),
                )

        else:

            dynamic_stop = active_stop

        diagnostics["dynamic_stop"] = round(
            dynamic_stop,
            2,
        )

        # ==========================================================
        # PROFIT LOCK
        # ==========================================================

        unrealized_return = (
            (current_price - entry_price)
            / max(
                entry_price,
                0.01,
            )
        ) * 100

        if decision.action != "BUY":

            unrealized_return *= -1

        diagnostics["unrealized_return"] = round(
            unrealized_return,
            2,
        )

        profit_locked = False

        if unrealized_return >= 10:

            profit_locked = True

            reasons.append("Profit protection activated.")

        diagnostics["profit_locked"] = profit_locked

        # ==========================================================
        # CONFIDENCE REFINEMENT
        # ==========================================================
        # Phase 21 (see PHASE21_NOTES.md, point 17): previously blended in
        # `decision.confidence` (the ENTRY-time decision engine's output,
        # computed potentially hours/days earlier) at a 0.20 weight. This
        # re-injected stale entry-time information into a live exit-time
        # confidence number — action (EXIT/HOLD/etc.) was never affected
        # by it (that's decided by the priority engine above, independent
        # of this blend), but the displayed confidence percentage was.
        # Removed; the freed weight goes to the two genuinely exit-time
        # signals (the priority-engine's own confidence, and the current
        # live risk score).

        confidence = (
            confidence * 0.75
            + (100.0 - risk.total_risk) * 0.25
        )

        confidence = round(
            min(
                confidence,
                100.0,
            ),
            2,
        )

        diagnostics["final_confidence"] = confidence

        # ==========================================================
        # EXIT QUALITY
        # ==========================================================

        exit_quality = (
            expected_rr * 20.0 + confidence * 0.40 + (100.0 - risk.total_risk) * 0.20
        )

        exit_quality = min(
            exit_quality,
            100.0,
        )

        diagnostics["exit_quality"] = round(
            exit_quality,
            2,
        )

        # ==========================================================
        # EXIT ANALYTICS
        # ==========================================================

        diagnostics["holding_days"] = holding_days

        diagnostics["entry_price"] = round(
            entry_price,
            2,
        )

        diagnostics["current_price"] = round(
            current_price,
            2,
        )

        diagnostics["highest_price"] = round(
            highest_price,
            2,
        )

        diagnostics["lowest_price"] = round(
            lowest_price,
            2,
        )

        logger.info("Exit analytics generated.")
        # ==========================================================
        # POSITION HEALTH
        # ==========================================================

        if unrealized_return >= 15:

            position_health = "EXCELLENT"

        elif unrealized_return >= 8:

            position_health = "STRONG"

        elif unrealized_return >= 3:

            position_health = "HEALTHY"

        elif unrealized_return >= 0:

            position_health = "NEUTRAL"

        elif unrealized_return >= -5:

            position_health = "WEAK"

        else:

            position_health = "CRITICAL"

        diagnostics["position_health"] = position_health

        # ==========================================================
        # EXIT GRADE
        # ==========================================================

        if exit_quality >= 95:

            exit_grade = "A+"

        elif exit_quality >= 90:

            exit_grade = "A"

        elif exit_quality >= 80:

            exit_grade = "B+"

        elif exit_quality >= 70:

            exit_grade = "B"

        elif exit_quality >= 60:

            exit_grade = "C"

        else:

            exit_grade = "D"

        diagnostics["exit_grade"] = exit_grade

        # ==========================================================
        # AI RECOMMENDATIONS
        # ==========================================================

        recommendations: list[str] = []

        if action == HOLD:

            recommendations.append("Continue following the exit plan.")

        elif action == PARTIAL_EXIT:

            recommendations.append(
                "Book partial profits and trail the remaining position."
            )

        else:

            recommendations.append("Exit the position completely.")

        if profit_locked:

            recommendations.append("Keep stop-loss above break-even.")

        if expected_rr >= 3.0:

            recommendations.append("Maintain disciplined risk/reward execution.")

        if risk.total_risk >= 40:

            recommendations.append("Elevated portfolio risk detected.")

        diagnostics["recommendations"] = recommendations

        # ==========================================================
        # TRADE LIFECYCLE SUMMARY
        # ==========================================================

        diagnostics["trade_lifecycle"] = {
            "entry_price": round(
                entry_price,
                2,
            ),
            "current_price": round(
                current_price,
                2,
            ),
            "holding_days": holding_days,
            "unrealized_return": round(
                unrealized_return,
                2,
            ),
            "position_health": position_health,
            "exit_action": action,
            "exit_grade": exit_grade,
        }

        # ==========================================================
        # EXIT SCORECARD
        # ==========================================================

        diagnostics["scorecard"] = {
            "confidence": confidence,
            "exit_quality": round(
                exit_quality,
                2,
            ),
            "expected_rr": expected_rr,
            "risk_grade": risk.risk_grade,
        }

        logger.info("Exit recommendations prepared.")
        # ==========================================================
        # EXIT DIAGNOSTICS
        # ==========================================================

        diagnostics["exit_summary"] = {
            "action": action,
            "exit_percent": exit_percent,
            "confidence": confidence,
            "active_stop": round(
                active_stop,
                2,
            ),
            "dynamic_stop": round(
                dynamic_stop,
                2,
            ),
            "take_profit": round(
                final_target,
                2,
            ),
            "break_even": round(
                entry_price,
                2,
            ),
        }

        # ==========================================================
        # CONSISTENCY VALIDATION
        # ==========================================================

        validation_errors = []

        if action not in (
            HOLD,
            PARTIAL_EXIT,
            FULL_EXIT,
        ):

            validation_errors.append("Invalid exit action.")

        if not (0.0 <= exit_percent <= 100.0):

            validation_errors.append("Exit percent out of range.")

        if stop_loss <= 0:

            validation_errors.append("Invalid stop-loss.")

        if trailing_stop <= 0:

            validation_errors.append("Invalid trailing stop.")

        if final_target <= 0:

            validation_errors.append("Invalid take-profit.")

        if confidence < 0 or confidence > 100:

            validation_errors.append("Confidence out of range.")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE
        # ==========================================================

        if validation_errors:

            logger.error(
                "Exit strategy validation failed: %s",
                validation_errors,
            )

            action = HOLD

            exit_percent = 0.0

            confidence = 0.0

            resolved_exit_price = None

            warnings = ["Exit strategy entered fail-safe mode."]

            diagnostics["fail_safe"] = True

            # BUGFIX (2026-09-18, Phase 3 — see BUG_AUDIT_2026-09-18.md
            # item #12): diagnostics["exit_percent"] was written EARLIER,
            # before this fail-safe block runs, and never updated here —
            # so `diagnostics` kept showing the PRE-fail-safe exit
            # percent even though the top-level ExitDecision below
            # correctly reports 0.0. Sync so diagnostics never
            # contradicts the actual result during debugging.
            diagnostics["exit_percent"] = exit_percent

        else:

            warnings = []

            diagnostics["fail_safe"] = False

        # Surfaced in diagnostics (was previously computed but silently
        # dropped — never persisted anywhere) so a fail-safe event is
        # actually visible to anything inspecting this ExitDecision.
        diagnostics["warnings"] = warnings

        # ==========================================================
        # BUILD RESULT
        # ==========================================================

        result = ExitDecision(
            action=action,
            exit_percent=round(
                exit_percent,
                2,
            ),
            stop_loss=round(
                dynamic_stop,
                2,
            ),
            trailing_stop=round(
                trailing_stop,
                2,
            ),
            take_profit=round(
                final_target,
                2,
            ),
            break_even_price=round(
                entry_price,
                2,
            ),
            expected_rr=round(
                expected_rr,
                2,
            ),
            confidence=round(
                confidence,
                2,
            ),
            reasons=reasons,
            diagnostics=diagnostics,
            suggested_exit_price=(
                round(resolved_exit_price, 2) if resolved_exit_price is not None else None
            ),
        )

        logger.info("ExitDecision created successfully.")

        return result

    # ==========================================================
    # EXPORT
    # ==========================================================

    @staticmethod
    def to_dict(
        result: ExitDecision,
    ) -> dict[str, Any]:

        return {
            "action": result.action,
            "exit_percent": result.exit_percent,
            "stop_loss": result.stop_loss,
            "trailing_stop": result.trailing_stop,
            "take_profit": result.take_profit,
            "break_even_price": result.break_even_price,
            "expected_rr": result.expected_rr,
            "confidence": result.confidence,
            "reasons": result.reasons,
            "diagnostics": result.diagnostics,
        }

    # ==========================================================
    # SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        result: ExitDecision,
    ) -> str:

        return (
            f"{result.action}"
            f" | Exit={result.exit_percent:.0f}%"
            f" | Stop={result.stop_loss:.2f}"
            f" | Target={result.take_profit:.2f}"
            f" | RR={result.expected_rr:.2f}"
            f" | Confidence={result.confidence:.2f}%"
        )

    # ==========================================================
    # SHORT REPORT
    # ==========================================================

    @staticmethod
    def short_report(
        result: ExitDecision,
    ) -> str:

        lines = []

        lines.append(f"Action : {result.action}")

        lines.append(f"Exit : {result.exit_percent:.0f}%")

        lines.append(f"Stop : {result.stop_loss:.2f}")

        lines.append(f"Target : {result.take_profit:.2f}")

        lines.append(f"RR : {result.expected_rr:.2f}")

        lines.append(f"Confidence : {result.confidence:.2f}%")

        return "\n".join(lines)

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        result: ExitDecision,
    ) -> str:

        report: list[str] = []

        report.append("=" * 70)
        report.append("EXIT STRATEGY REPORT")
        report.append("=" * 70)
        report.append("")

        report.append(f"Action               : {result.action}")

        report.append(f"Exit Percent         : {result.exit_percent:.2f}%")

        report.append(f"Stop Loss            : {result.stop_loss:.2f}")

        report.append(f"Trailing Stop        : {result.trailing_stop:.2f}")

        report.append(f"Take Profit          : {result.take_profit:.2f}")

        report.append(f"Break-even Price     : {result.break_even_price:.2f}")

        report.append(f"Expected R:R         : {result.expected_rr:.2f}")

        report.append(f"Confidence           : {result.confidence:.2f}%")

        report.append("")

        report.append("Reasons")
        report.append("-" * 70)

        if result.reasons:

            for reason in result.reasons:

                report.append(f"• {reason}")

        else:

            report.append("None")

        report.append("")
        report.append("Diagnostics")
        report.append("-" * 70)

        for key, value in sorted(result.diagnostics.items()):

            report.append(f"{key:<30} : {value}")

        report.append("")
        report.append("=" * 70)

        return "\n".join(report)


# ==========================================================
# END OF FILE
# ==========================================================
