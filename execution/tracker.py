"""
Position Tracker Engine

Institutional Production Version

Responsibilities
----------------
• Track all open positions in real time
• Update price, PnL, highs/lows
• Maintain trade state
• Feed exit strategy engine
• Trigger risk updates
• Generate portfolio-level position snapshot

This engine does NOT:
• Open trades
• Close trades directly
• Modify strategy logic
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.logger import get_logger

from risk.exit_strategy import ExitStrategyEngine
from risk.risk_manager import RiskManager
from risk.position_sizing import PositionSizingEngine

logger = get_logger(__name__)


# ==========================================================
# POSITION STATE
# ==========================================================


@dataclass(slots=True)
class PositionState:

    symbol: str

    entry_price: float

    quantity: int

    direction: str  # BUY / SELL

    entry_time: str

    current_price: float = 0.0

    pnl_percent: float = 0.0

    pnl_absolute: float = 0.0

    highest_price: float = 0.0

    lowest_price: float = 0.0

    holding_days: int = 0

    stop_loss: float = 0.0

    take_profit: float = 0.0

    trailing_stop: float = 0.0

    status: str = "OPEN"  # OPEN / CLOSED / SUSPENDED

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# TRACKER RESULT
# ==========================================================


@dataclass(slots=True)
class TrackerResult:

    symbol: str

    action: str  # HOLD / EXIT / REDUCE / TRAIL

    status: str

    pnl_percent: float

    pnl_absolute: float

    current_price: float

    stop_loss: float

    take_profit: float

    warnings: list[str] = field(default_factory=list)

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# TRACKER SYSTEM
# ==========================================================


class PositionTracker:

    def __init__(self):

        self.positions: dict[str, PositionState] = {}

        self.exit_engine = ExitStrategyEngine()

        self.risk_manager = RiskManager()

        self.sizer = PositionSizingEngine()

        logger.info("Position Tracker Engine initialized successfully.")

    # ==========================================================
    # ADD POSITION
    # ==========================================================

    def add_position(self, state: PositionState) -> bool:

        if state.symbol in self.positions:

            logger.warning("Position for %s already exists. Overwriting.", state.symbol)

        self.positions[state.symbol] = state

        logger.info(
            "Tracked new position: %s | Qty=%d | Entry=%s",
            state.symbol,
            state.quantity,
            state.entry_price,
        )

        return True

    # ==========================================================
    # REMOVE POSITION
    # ==========================================================

    def remove_position(self, symbol: str) -> bool:

        if symbol in self.positions:

            del self.positions[symbol]

            logger.info("Removed %s from tracker database.", symbol)

            return True

        logger.warning("Attempted to remove non-existent symbol: %s", symbol)

        return False

    # ==========================================================
    # UPDATE SYSTEM
    # ==========================================================

    def update(
        self,
        dataframe_map: dict[str, pd.DataFrame],
        portfolio: dict[str, Any],
        market: dict[str, Any],
    ) -> list[TrackerResult]:

        results: list[TrackerResult] = []

        if not self.positions:

            logger.debug("No open positions to track.")

            return results

        logger.info("Updating %d active positions.", len(self.positions))

        for symbol, pos in list(self.positions.items()):

            if pos.status == "CLOSED":

                continue

            df = dataframe_map.get(symbol, None)

            if df is None or df.empty:

                logger.error("No historical dataframe found for %s", symbol)

                continue

            latest = df.iloc[-1]

            close = float(latest.get("close", pos.entry_price))

            high = float(latest.get("high", close))

            low = float(latest.get("low", close))

            # --------------------------------------------------
            # MATH ENGINE
            # --------------------------------------------------

            pos.current_price = close

            pos.highest_price = max(pos.highest_price, high)

            pos.lowest_price = (
                min(pos.lowest_price, low) if pos.lowest_price > 0 else low
            )

            pos.holding_days += 1

            # --------------------------------------------------
            # PNL CALCULATOR
            # --------------------------------------------------

            if pos.direction == "BUY":

                pos.pnl_absolute = (close - pos.entry_price) * pos.quantity

                pos.pnl_percent = ((close / pos.entry_price) - 1.0) * 100.0

            else:

                pos.pnl_absolute = (pos.entry_price - close) * pos.quantity

                pos.pnl_percent = ((pos.entry_price / close) - 1.0) * 100.0

            # --------------------------------------------------
            # RISK PIPELINE INTEGRATION
            # --------------------------------------------------

            from decision.validation_engine import ValidationResult
            from decision.decision_engine import FinalDecision

            # BUG FIX: `ValidationResult(passed=True)` was missing 3
            # mandatory fields (action, confidence, rejection_reason —
            # none of them have defaults) and would raise TypeError the
            # instant this ran.
            v = ValidationResult(
                passed=True, action=pos.direction, confidence=100.0, rejection_reason=None,
            )

            # A fully-populated neutral FinalDecision — PositionTracker
            # (unlike execution/scanner.py) has no fresh BUY/SELL scan
            # for this symbol here, only the position's OWN held
            # direction, so `action` is set straight to that. RiskManager
            # only needs a structurally valid decision to score against;
            # ExitStrategyEngine below only reads .action and .confidence
            # from it. (BUG FIX: the previous `FinalDecision(signal=...)`
            # used a kwarg — "signal" — that doesn't exist on this
            # dataclass at all, and was also missing every other
            # mandatory field; this crashed immediately the moment
            # update() was ever called.)
            d = FinalDecision(
                action=pos.direction, confidence=100.0, ranking=0.0,
                buy_score=0.0, sell_score=0.0,
                buy_probability=0.0, sell_probability=0.0,
                expected_return=0.0, expected_drawdown=0.0, expected_hold_days=0,
            )

            risk_result = self.risk_manager.evaluate(
                validation=v,
                decision=d,
                dataframe=df,
                portfolio=portfolio,
                market=market,
            )

            # --------------------------------------------------
            # EXIT STRATEGY ENGINE
            # --------------------------------------------------
            # BUG FIX: previously called with `position=pos` — a
            # PositionState DATACLASS instance — but
            # ExitStrategyEngine.evaluate() expects a plain dict and
            # calls `.get(...)` on it throughout; this would raise
            # AttributeError on the very first field read. Also missing
            # the required `decision` argument entirely. Both fixed here.

            position_input = {
                "symbol": symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "current_price": close,
                "holding_days": pos.holding_days,
                "highest_price": pos.highest_price,
                "lowest_price": pos.lowest_price,
                "day_high": high,
                "day_low": low,
                # Same risk-unsafe hard-exit wiring as
                # paper_trading/paper_trading_engine.py — see
                # PHASE19_NOTES.md.
                "emergency_exit": not risk_result.safe,
                "emergency_exit_reason": (
                    f"Risk engine flagged this symbol as unsafe "
                    f"(grade: {risk_result.risk_grade}, total_risk: "
                    f"{risk_result.total_risk:.0f}/100)."
                ),
            }

            exit_result = self.exit_engine.evaluate(
                decision=d,
                risk=risk_result,
                dataframe=df,
                position=position_input,
            )

            # --------------------------------------------------
            # PROCESS ACTIONS
            # --------------------------------------------------
            # ExitDecision.action is HOLD / PARTIAL_EXIT / FULL_EXIT
            # (risk/exit_engine.py's old EXIT / FORCE_EXIT / TRAIL vocabulary
            # never applied to ExitStrategyEngine — this matched neither
            # the class this code actually instantiates, above, nor any
            # value ExitStrategyEngine.evaluate() has ever returned).

            if exit_result.action == "FULL_EXIT":

                pos.status = "CLOSED"

                logger.info("Exit trigger detected for %s.", symbol)

            elif exit_result.action == "PARTIAL_EXIT":

                logger.info("Partial exit trigger detected for %s.", symbol)

            # ExitDecision.stop_loss is already the fully dynamic/trailing
            # stop (see risk/exit_strategy.py's DYNAMIC STOP ADJUSTMENT
            # section) — persist it (and the separate trailing_stop/
            # take_profit fields) onto the position every cycle, not only
            # on a TRAIL action (that action value doesn't exist on this
            # engine; the stop trails implicitly every time HOLD persists).
            pos.stop_loss = exit_result.stop_loss

            pos.trailing_stop = exit_result.trailing_stop

            pos.take_profit = exit_result.take_profit

            # --------------------------------------------------
            # COMPILE RESULT
            # --------------------------------------------------

            results.append(
                TrackerResult(
                    symbol=symbol,
                    action=exit_result.action,
                    status=pos.status,
                    pnl_percent=round(pos.pnl_percent, 2),
                    pnl_absolute=round(pos.pnl_absolute, 2),
                    current_price=close,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    # ExitDecision has no `.warnings` field — the
                    # fail-safe warnings list lives in diagnostics
                    # (see risk/exit_strategy.py's FAIL SAFE section).
                    warnings=exit_result.diagnostics.get("warnings", []),
                    diagnostics={
                        "holding_days": pos.holding_days,
                        "highest": pos.highest_price,
                        "lowest": pos.lowest_price,
                        "risk_grade": risk_result.risk_grade,
                        "exit_reason": exit_result.diagnostics.get("exit_reason"),
                        "trailing_stop": exit_result.trailing_stop,
                        "expected_rr": exit_result.expected_rr,
                        "confidence": exit_result.confidence,
                    },
                )
            )

        logger.info("Completed position tracker cycle.")

        return results

    # ==========================================================
    # GET SNAPSHOT
    # ==========================================================

    def get_snapshot(self) -> dict[str, Any]:

        open_count = sum(1 for p in self.positions.values() if p.status == "OPEN")

        total_pnl = sum(p.pnl_absolute for p in self.positions.values())

        return {
            "total_tracked_positions": len(self.positions),
            "open_positions_count": open_count,
            "total_portfolio_pnl_absolute": round(total_pnl, 2),
            "symbols": list(self.positions.keys()),
        }

    # ==========================================================
    # SUMMARY BUILDER
    # ==========================================================

    @staticmethod
    def summary(results: list[TrackerResult]) -> str:

        if not results:

            return "Tracker Summary: No active tracking items."

        total_items = len(results)

        exits = sum(1 for r in results if r.action == "FULL_EXIT")

        holds = sum(1 for r in results if r.action == "HOLD")

        partial_exits = sum(1 for r in results if r.action == "PARTIAL_EXIT")

        avg_pnl = sum(r.pnl_percent for r in results) / total_items

        return (
            f"Tracker Sync Report | Total={total_items}"
            f" | HOLDS={holds}"
            f" | EXITS={exits}"
            f" | PARTIAL_EXITS={partial_exits}"
            f" | Avg PnL={avg_pnl:.2f}%"
        )

    # ==========================================================
    # TOP MOVERS
    # ==========================================================

    @staticmethod
    def top_movers(
        results: list[TrackerResult],
        limit: int = 10,
    ) -> list[TrackerResult]:

        return sorted(
            results,
            key=lambda r: r.pnl_percent,
            reverse=True,
        )[:limit]

    # ==========================================================
    # DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        results: list[TrackerResult],
    ) -> str:

        report: list[str] = []

        report.append("=" * 120)
        report.append("TRACKER SYSTEM REPORT")
        report.append("=" * 120)
        report.append("")

        # <--- FIXED: Change Tracker to PositionTracker
        report.append(PositionTracker.summary(results))

        report.append("")
        report.append("-" * 120)

        for r in results:

            report.append(
                f"{r.symbol:<15}"
                f"{r.action:<10}"
                f"{r.status:<12}"
                f"{r.pnl_percent:>10.2f}%"
                f"{r.pnl_absolute:>15.2f}"
            )

        report.append("")
        report.append("=" * 120)

        return "\n".join(report)


# ==========================================================
# END OF FILE
# ==========================================================
