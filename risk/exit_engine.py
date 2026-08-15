"""
EXIT ENGINE

Answers one question, separately from entry logic: "Should this position
still be held?" — NOT a mirror of the entry (buy/sell) engines.

Architecture (as specified):
    Technical Exit
    Fundamental Exit
    News Exit
    Risk Exit
    Institutional Exit (FII/DII market-wide flow)
        |
    Exit Score (0-100)
        |
    EXIT or HOLD

No single soft signal forces an exit on its own — they're blended into a
weighted score. Only a genuine HARD-RISK event (stop-loss breached, risk
engine flags unsafe, or max holding period exceeded) forces an immediate
EXIT regardless of the weighted score, matching the spec's "no single
signal should immediately force an exit unless it is a genuine hard-risk
event."

This module does not modify, call, or depend on buy_strategy.py /
sell_strategy.py's decision logic — it is a deliberately separate
evaluation, reusing only shared low-level building blocks (fundamental
scoring, news bias) the same way the entry engines do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.logger import get_logger
from strategy.fundamental_scoring import buy_fundamental_score, sell_fundamental_score
from news.news_bias import news_component

logger = get_logger(__name__)


@dataclass
class ExitEvaluation:
    action: str  # "EXIT" or "HOLD"
    exit_score: float  # 0-100, higher = stronger case to exit
    threshold: float
    technical_exit: float
    fundamental_exit: float
    news_exit: float
    risk_exit: float
    institutional_exit: float
    hard_risk_triggered: bool
    hard_risk_reason: str | None
    reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # Set ONLY when a stop-loss or target was breached intraday (via
    # day_low/day_high) — a real stop/target ORDER executes immediately
    # at that touch, not at whatever price is current by the time this
    # periodic check runs. When set, the caller should use THIS price
    # to close the position, not current_price, so P&L reflects what
    # actually would have happened rather than a later, unrelated price.
    suggested_exit_price: float | None = None


class ExitEngine:
    """
    Weighted exit scoring engine. Call evaluate() once per open position,
    per scan cycle, with the SAME fresh dataframe/fundamentals/news_score
    the scanner just computed for that symbol.
    """

    # Weights are a starting point (mirroring the entry engines' documented
    # philosophy) — Phase 2's Learning Engine / Optimizer should validate
    # and refine these once enough closed trades exist.
    # INSTITUTIONAL_WEIGHT (FII/DII flow, added later) took its 0.10 from
    # TECHNICAL (0.35->0.30) and RISK (0.30->0.25) — the two largest
    # weights — rather than from FUNDAMENTAL/NEWS, which were already
    # the smaller, secondary components.
    TECHNICAL_WEIGHT = 0.30
    FUNDAMENTAL_WEIGHT = 0.20
    NEWS_WEIGHT = 0.15
    RISK_WEIGHT = 0.25
    INSTITUTIONAL_WEIGHT = 0.10

    EXIT_THRESHOLD = 60.0

    MAX_HOLD_DAYS = 30

    def evaluate(
        self,
        dataframe: pd.DataFrame,
        fundamentals: dict[str, Any],
        news_score: float | None,
        position: dict[str, Any],
        risk_safe: bool = True,
        holding_days: int = 0,
        fii_dii_bias: float | None = None,
    ) -> ExitEvaluation:
        """
        position: {"direction": "BUY"/"SELL", "entry_price": float,
                   "current_price": float, "stop_loss": float | None,
                   "max_drawdown_percent": float}
        risk_safe: latest RiskManager.evaluate().safe for this symbol
                   (a False here is itself a hard-risk signal).
        holding_days: calendar/trading days since entry (from the Trade
                      Diary, since PortfolioPosition doesn't track entry
                      date itself).
        fii_dii_bias: signed market-wide institutional-flow bias in
                      [-1, +1] from data/fii_dii_data.py (positive =
                      net institutional buying), same value already
                      re-fetched fresh every monitoring cycle inside
                      execution/scanner.py's _evaluate_market_context().
                      None means no live/cached data was available that
                      cycle — treated as neutral (contributes 0 to the
                      exit score), never fabricated.
        """
        if dataframe.empty:
            raise ValueError("Empty dataframe.")

        latest = dataframe.iloc[-1]
        direction = position.get("direction", "BUY")
        reasons: list[str] = []
        diagnostics: dict[str, Any] = {}

        # ==========================================================
        # HARD-RISK CHECK (can force EXIT on its own)
        # ==========================================================
        hard_risk_triggered = False
        hard_risk_reason = None

        stop_loss = position.get("stop_loss")
        current_price = position.get("current_price", float(latest.get("close", 0.0)))
        day_high = position.get("day_high")
        day_low = position.get("day_low")

        suggested_exit_price = None
        if stop_loss:
            if direction == "BUY":
                # Use the day's LOW if available — catches a genuine
                # intraday stop-loss touch even if the close recovered
                # above the stop level. Falls back to close-only if
                # day_low wasn't supplied (backward compatible).
                touch_price = day_low if day_low is not None else current_price
                stop_breached = touch_price <= stop_loss
            else:
                touch_price = day_high if day_high is not None else current_price
                stop_breached = touch_price >= stop_loss
            if stop_breached:
                hard_risk_triggered = True
                hard_risk_reason = (
                    f"Stop-loss breached (intraday {'low' if direction == 'BUY' else 'high'}"
                    f"={touch_price}, stop={stop_loss})."
                )
                suggested_exit_price = touch_price

        # Target-hit hard-check (NEW — targets were previously display
        # only, never triggered an actual exit). Uses Target 1 (the
        # nearer, partial target) as the profit-booking trigger, same
        # day-High/Low-aware logic as the stop-loss check above.
        target1 = position.get("target1")
        if not hard_risk_triggered and target1:
            if direction == "BUY":
                touch_price = day_high if day_high is not None else current_price
                target_hit = touch_price >= target1
            else:
                touch_price = day_low if day_low is not None else current_price
                target_hit = touch_price <= target1
            if target_hit:
                hard_risk_triggered = True
                hard_risk_reason = (
                    f"Target 1 reached (intraday {'high' if direction == 'BUY' else 'low'}"
                    f"={touch_price}, target={target1})."
                )
                suggested_exit_price = touch_price

        if not hard_risk_triggered and not risk_safe:
            hard_risk_triggered = True
            hard_risk_reason = "Risk engine flagged this symbol as unsafe."

        if not hard_risk_triggered and holding_days >= self.MAX_HOLD_DAYS:
            hard_risk_triggered = True
            hard_risk_reason = f"Maximum holding period reached ({holding_days} >= {self.MAX_HOLD_DAYS} days)."

        # ==========================================================
        # TECHNICAL EXIT — trend reversal AGAINST the held direction
        # ==========================================================
        ema20 = float(latest.get("ema_20", 0) or 0)
        ema50 = float(latest.get("ema_50", 0) or 0)
        macd = float(latest.get("macd", 0) or 0)
        macd_signal = float(latest.get("macd_signal", 0) or 0)
        rsi = float(latest.get("rsi_14", 50) or 50)
        supertrend_bullish = bool(latest.get("supertrend", False))
        adx = float(latest.get("adx_14", 0) or 0)

        technical_checks = {}
        if direction == "BUY":
            technical_checks["ema_reversal"] = ema20 < ema50
            technical_checks["macd_reversal"] = macd < macd_signal
            technical_checks["supertrend_reversal"] = not supertrend_bullish
            technical_checks["rsi_overbought_fading"] = rsi < 45
        else:
            technical_checks["ema_reversal"] = ema20 > ema50
            technical_checks["macd_reversal"] = macd > macd_signal
            technical_checks["supertrend_reversal"] = supertrend_bullish
            technical_checks["rsi_oversold_fading"] = rsi > 55

        # Weak/fading trend strength also counts as a (softer) technical
        # exit signal, regardless of direction.
        technical_checks["trend_weakening"] = adx < 20

        technical_exit = (sum(technical_checks.values()) / len(technical_checks)) * 100
        diagnostics["technical_checks"] = technical_checks
        if technical_exit >= 60:
            reasons.append(f"Technical trend reversing against the {direction} position ({technical_exit:.0f}/100).")

        # ==========================================================
        # FUNDAMENTAL EXIT — deteriorating fundamentals for the held side
        # ==========================================================
        if direction == "BUY":
            fundamental_exit = sell_fundamental_score(fundamentals)  # "weakness" score
        else:
            fundamental_exit = buy_fundamental_score(fundamentals)  # "strength" score (bad for a short)
        if fundamental_exit >= 60:
            reasons.append(f"Fundamentals have deteriorated for this {direction} position ({fundamental_exit:.0f}/100).")

        # ==========================================================
        # NEWS EXIT — bidirectional, no-news is neutral (never forces exit)
        # ==========================================================
        has_news = news_score is not None
        if has_news:
            positive = news_component(news_score)
            news_exit = (100.0 - positive) if direction == "BUY" else positive
            if news_exit >= 60:
                reasons.append(f"News sentiment now works against this {direction} position ({news_exit:.0f}/100).")
        else:
            news_exit = 0.0  # absent news must not push toward exit
            reasons.append("No fresh news — news component does not contribute to the exit score.")

        # ==========================================================
        # RISK EXIT — drawdown from peak + stop-loss proximity
        # ==========================================================
        max_dd = float(position.get("max_drawdown_percent", 0.0) or 0.0)
        # Scale: 0% drawdown -> 0, 10%+ drawdown from peak -> 100 (capped)
        risk_exit = min(max_dd / 10.0 * 100.0, 100.0)
        if risk_exit >= 60:
            reasons.append(f"Drawdown from peak is significant ({max_dd:.1f}%).")

        # ==========================================================
        # INSTITUTIONAL EXIT — market-wide FII/DII flow working against
        # the held direction, bidirectional (no data is neutral, same
        # pattern as NEWS EXIT above, never forces exit on its own).
        # ==========================================================
        has_fii_dii = fii_dii_bias is not None
        if has_fii_dii:
            # Reuse the same [-1,1] -> [0,100] "bullishness" convention
            # as news_component(), so the BUY/SELL mirroring formula
            # below is identical in shape to the NEWS EXIT block above.
            institutional_bullishness = 50.0 + float(fii_dii_bias) * 50.0
            institutional_exit = (
                (100.0 - institutional_bullishness) if direction == "BUY" else institutional_bullishness
            )
            if institutional_exit >= 60:
                reasons.append(
                    f"Institutional (FII/DII) flow now works against this {direction} "
                    f"position ({institutional_exit:.0f}/100)."
                )
        else:
            institutional_exit = 0.0  # absent data must not push toward exit
            reasons.append("No FII/DII data — institutional flow does not contribute to the exit score.")

        # ==========================================================
        # WEIGHTED EXIT SCORE
        # ==========================================================
        exit_score = (
            technical_exit * self.TECHNICAL_WEIGHT
            + fundamental_exit * self.FUNDAMENTAL_WEIGHT
            + news_exit * self.NEWS_WEIGHT
            + risk_exit * self.RISK_WEIGHT
            + institutional_exit * self.INSTITUTIONAL_WEIGHT
        )

        if hard_risk_triggered:
            action = "EXIT"
            reasons.insert(0, f"HARD RISK EXIT: {hard_risk_reason}")
        else:
            action = "EXIT" if exit_score >= self.EXIT_THRESHOLD else "HOLD"
            reasons.append(f"Exit score {exit_score:.1f}/100 (threshold {self.EXIT_THRESHOLD}) -> {action}.")

        logger.info(
            "Exit evaluation for %s (%s): score=%.1f action=%s hard_risk=%s",
            position.get("symbol", "?"), direction, exit_score, action, hard_risk_triggered,
        )

        return ExitEvaluation(
            action=action,
            exit_score=round(exit_score, 2),
            threshold=self.EXIT_THRESHOLD,
            technical_exit=round(technical_exit, 2),
            fundamental_exit=round(fundamental_exit, 2),
            news_exit=round(news_exit, 2),
            risk_exit=round(risk_exit, 2),
            institutional_exit=round(institutional_exit, 2),
            hard_risk_triggered=hard_risk_triggered,
            hard_risk_reason=hard_risk_reason,
            reasons=reasons,
            diagnostics=diagnostics,
            suggested_exit_price=suggested_exit_price,
        )
