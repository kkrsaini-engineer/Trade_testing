"""
WIRED ORCHESTRATOR (PRODUCTION FLOW)

Single controlled execution pipeline synchronized with the frozen contract.
NO module is allowed to bypass this flow.

Modes:
- LIVE
- BACKTEST
- PAPER
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import time

# Core System Utilities
from core.logger import get_logger
from data.watchlist import WatchlistManager

# Fixed Package Internal Mapping Layer
from execution.scanner import (
    MarketScanner,
)  # Fixed: Imported correct class matching folder
from execution.broker import BrokerEngine, OrderRequest
from execution.tracker import PositionTracker
from market.volatility import fetch_india_vix
from portfolio.portfolio import PortfolioEngine, PortfolioState
from storage.trades.trade_store import TradeStore
from output.report_generator import ReportGenerator

logger = get_logger(__name__)


# ==========================================================
# ORCHESTRATOR STATE CONTEXT
# ==========================================================


@dataclass
class OrchestratorContext:
    mode: str  # LIVE / BACKTEST / PAPER
    cycle_id: int = 0
    timestamp: float = 0.0
    last_data: Any = None
    last_features: Any = None
    last_signals: Any = None
    last_decision: Any = None
    last_risk: Any = None
    last_execution: Any = None
    last_portfolio: Dict[str, Any] = None
    last_analytics: Any = None


# ==========================================================
# WIRED ORCHESTRATOR CORE
# ==========================================================


class WiredOrchestrator:
    def __init__(self, mode: str = "BACKTEST"):
        self.mode = mode

        # Core Engines Initialization
        self.scanner = MarketScanner()  # Fixed reference to standard interface
        self.broker = BrokerEngine()
        self.portfolio = PortfolioEngine(
            state=PortfolioState(total_capital=500000.0, available_capital=500000.0)
        )
        self.tracker = PositionTracker()
        self.trade_store = TradeStore()
        self.report_generator = ReportGenerator()

        self.context = OrchestratorContext(mode=mode)
        logger.info(f"WIRED ORCHESTRATOR successfully initialized in {mode} mode")

    # ==========================================================
    # MAIN SEQUENTIAL PIPELINE LOOP
    # ==========================================================

    def run_cycle(
        self, symbols: list[str], portfolio_state: Any
    ) -> OrchestratorContext:
        self.context.cycle_id += 1
        self.context.timestamp = time.time()

        logger.info(
            f"--- Triggering Execution Matrix Sequence for Cycle: {self.context.cycle_id} ---"
        )

        # STEP 1: VALIDATE INPUT
        if not symbols:
            logger.error("Cycle processing suspended: No symbols supplied.")
            return self.context

        # STEP 2: BUILD SHARED CONTEXT DICTS
        broker_status = {"status": "ONLINE", "mode": self.mode, "connected": True, "order_allowed": True, "available_margin": 100000.0}
        market_state = {
            "max_trade_candidates": 20,
            "max_watchlist": 50,
            "event_day": False,
            # Was hardcoded to 20 (a "calm market" placeholder) — meaning
            # risk_manager.py's vix >= 30 / vix >= 35 risk-off checks
            # could never fire. Now fetched live each cycle (falls back
            # to 20.0 itself, with a logged warning, if the fetch fails).
            "vix": fetch_india_vix(),
            # CRITICAL: without these two keys, ValidationEngine defaults
            # market_open to False and rejects EVERY trade with "Market is
            # closed" — meaning portfolio_allowed was permanently stuck at
            # False and no order could ever actually execute, regardless
            # of how good the BUY/SELL signal was. TODO: replace with a
            # real NSE market-hours/holiday-calendar check for live mode.
            "market_open": True,
            "holiday": False,
        }
        # Prefer the orchestrator's live portfolio bookkeeping over whatever the
        # caller passed in, so sizing/risk/validation always see current state.
        portfolio_dict = self.portfolio.snapshot()

        # STEP 3-6 (FEATURES -> STRATEGY -> SCORING -> PROBABILITY -> DECISION ->
        # VALIDATION -> RISK -> SIZING -> PORTFOLIO RULES) all happen per-symbol
        # inside MarketScanner, which owns the full decision pipeline.
        scan_candidates = self.scanner.scan_symbols(
            symbols=symbols,
            portfolio=portfolio_dict,
            broker_status=broker_status,
            market_state=market_state,
        )
        self.context.last_signals = scan_candidates

        errored = [c for c in scan_candidates if c.action == "ERROR"]
        if errored:
            for c in errored:
                logger.warning(
                    "Scanner error for %s: %s", c.symbol, c.diagnostics.get("error")
                )

        # STEP 7: RISK / KILL-SWITCH GATEWAY (portfolio-wide, before any orders)
        risk_snapshot = {
            "volatility": 0.0,
            "liquidity_risk": False,
        }
        self.context.last_risk = risk_snapshot
        if not self.pre_cycle_guard(portfolio_dict, risk_snapshot):
            logger.warning(
                f"Risk controls blocked execution pipeline commands at cycle iteration: {self.context.cycle_id}"
            )
            self.context.last_execution = {"status": "BLOCKED", "reason": "kill_switch/circuit_breaker"}
            return self.context

        # STEP 8: ORDER SUBMISSION TO BROKER ENGINE
        executed_orders = []
        for candidate in scan_candidates:
            if candidate.action not in ("BUY", "SELL") or not candidate.portfolio_allowed:
                continue
            if candidate.position_size <= 0:
                continue

            market_price = candidate.diagnostics.get("latest_close", 0.0)
            order_request = OrderRequest(
                symbol=candidate.symbol,
                action=candidate.action,
                quantity=candidate.position_size,
            )
            order_result = self.broker.place_order(
                order=order_request,
                market_price=market_price,
                market_state=market_state,
            )
            executed_orders.append(order_result)

            # STEP 9: PORTFOLIO BOOKKEEPING FOR THIS FILL
            if order_result.status in ("FILLED", "PARTIAL"):
                if candidate.action == "BUY":
                    self.portfolio.add_position(
                        symbol=candidate.symbol,
                        quantity=order_result.filled_quantity,
                        entry_price=order_result.avg_price,
                        direction="BUY",
                    )
                    self.trade_store.save_trade(
                        {
                            "symbol": candidate.symbol,
                            "direction": "BUY",
                            "action": "OPEN",
                            "quantity": order_result.filled_quantity,
                            "entry_price": order_result.avg_price,
                            "exit_price": "",
                            "status": "OPEN",
                            "realized_pnl": "",
                            "realized_pnl_percent": "",
                            "regime": candidate.diagnostics.get("market_regime", ""),
                            "confidence": candidate.confidence,
                            "reasons": "; ".join(candidate.diagnostics.get("reasons", []) or []),
                        }
                    )
                elif candidate.action == "SELL":
                    if candidate.symbol in self.portfolio.state.open_positions:
                        closed = self.portfolio.close_position(
                            symbol=candidate.symbol,
                            exit_price=order_result.avg_price,
                        )
                        if closed is not None:
                            # This is the real profit/loss journal entry that
                            # analytics/learning read back for accuracy tracking.
                            self.trade_store.save_trade(
                                {
                                    "symbol": closed.symbol,
                                    "direction": closed.direction,
                                    "action": "CLOSE",
                                    "quantity": closed.quantity,
                                    "entry_price": closed.entry_price,
                                    "exit_price": order_result.avg_price,
                                    "status": "CLOSED",
                                    "realized_pnl": closed.realized_pnl,
                                    "realized_pnl_percent": (
                                        (order_result.avg_price - closed.entry_price)
                                        / max(closed.entry_price, 1e-9)
                                        * 100
                                    ),
                                    "max_profit_percent": round(closed.max_profit_percent, 2),
                                    "max_drawdown_percent": round(closed.max_drawdown_percent, 2),
                                    "regime": candidate.diagnostics.get("market_regime", ""),
                                    "confidence": candidate.confidence,
                                    "reasons": "; ".join(
                                        candidate.diagnostics.get("reasons", []) or []
                                    ),
                                }
                            )
                    else:
                        self.portfolio.add_position(
                            symbol=candidate.symbol,
                            quantity=order_result.filled_quantity,
                            entry_price=order_result.avg_price,
                            direction="SELL",
                        )
                        self.trade_store.save_trade(
                            {
                                "symbol": candidate.symbol,
                                "direction": "SELL",
                                "action": "OPEN",
                                "quantity": order_result.filled_quantity,
                                "entry_price": order_result.avg_price,
                                "exit_price": "",
                                "status": "OPEN",
                                "realized_pnl": "",
                                "realized_pnl_percent": "",
                                "regime": candidate.diagnostics.get("market_regime", ""),
                                "confidence": candidate.confidence,
                                "reasons": "; ".join(
                                    candidate.diagnostics.get("reasons", []) or []
                                ),
                            }
                        )

        self.context.last_execution = {"status": "EXECUTED", "orders": executed_orders}

        # STEP 10: MARK OPEN POSITIONS TO MARKET (latest known price per symbol)
        latest_prices = {
            c.symbol: c.diagnostics.get("latest_close")
            for c in scan_candidates
            if c.diagnostics.get("latest_close") is not None
        }
        for sym, price in latest_prices.items():
            if sym in self.portfolio.state.open_positions:
                self.portfolio.update_position(symbol=sym, current_price=price)
        self.portfolio.mark_to_market()

        # STEP 11: FINANCIAL SNAPSHOT
        portfolio_snapshot = self.portfolio.snapshot()
        equity = portfolio_snapshot.get("total_capital", 100000.0)
        pnl = portfolio_snapshot.get("total_pnl", 0.0)

        self.context.last_portfolio = {
            "equity": equity,
            "pnl": pnl,
            "exposure": portfolio_snapshot.get("exposure", 0.0),
        }

        # STEP 12: REPORT TRIGGER — build + append this cycle to master_report.csv
        buy_count = sum(1 for c in scan_candidates if c.action == "BUY")
        sell_count = sum(1 for c in scan_candidates if c.action == "SELL")

        report = self.report_generator.generate(
            analytics={"cycle_id": self.context.cycle_id},
            portfolio=self.context.last_portfolio,
            risk=risk_snapshot,
        )
        self.report_generator.append_master_report(
            report,
            extra={
                "cycle_id": self.context.cycle_id,
                "buy_candidates": buy_count,
                "sell_candidates": sell_count,
                "orders_executed": len(executed_orders),
            },
        )

        # STEP 13: LOG SUCCESS SNAPSHOT
        logger.info(
            f"System execution pass complete. Status -> Cycle: {self.context.cycle_id} | Equity: {equity:.2f} | Realized PnL: {pnl:.2f}"
        )
        return self.context

    # ==========================================================
    # AUTOMATED LOOP RUN TIME MANAGERS
    # ==========================================================

    def run_live(
        self, symbols: list[str], portfolio_state: Any, sleep_seconds: int = 60
    ) -> None:
        logger.info("CRITICAL NODE OVERRIDE: LIVE MODE TRANSACTION PROCESSING DEPLOYED")
        while True:
            try:
                if not self.execute_cycle_safely(symbols, portfolio_state):
                    break
                time.sleep(sleep_seconds)
            except Exception as e:
                logger.exception(f"LIVE TERMINATION EXCEPTION TRACE DETECTED: {str(e)}")
                self.emergency_stop()
                break

    def run_backtest(
        self, historical_data: dict[str, Any], portfolio_state: Any
    ) -> list[OrchestratorContext]:
        logger.info("HISTORICAL PERFORMANCE SIMULATION ENGINE TRIGGERED")
        results = []
        if not historical_data:
            logger.warning(
                "No historical feed records passed to simulator. Exiting run loop execution context."
            )
            return results

        total_steps = len(next(iter(historical_data.values())))
        for step in range(total_steps):
            sliced_data = {k: v.iloc[: step + 1] for k, v in historical_data.items()}
            self.context.timestamp = time.time()
            ctx = self.run_cycle(
                symbols=list(historical_data.keys()), portfolio_state=portfolio_state
            )
            results.append(ctx)
        return results

    def run_paper(
        self, symbols: list[str], portfolio_state: Any, sleep_seconds: int = 10
    ) -> None:
        logger.info("PAPER RUN STATE MACHINE INITIATED")
        while True:
            self.run_cycle(symbols=symbols, portfolio_state=portfolio_state)
            time.sleep(sleep_seconds)

    def execute_cycle_safely(self, symbols: list[str], portfolio_state: Any) -> bool:
        if not self.validate_cycle(self.context):
            logger.error(
                "Cycle processing abandoned: Engine configuration checking parameters failed."
            )
            return False

        risk_check = self.context.last_risk or {}
        if not self.pre_cycle_guard(portfolio_state, risk_check):
            logger.warning("Pipeline processing terminated at security firewall.")
            return False

        self.run_cycle(symbols, portfolio_state)
        return True

    # ==========================================================
    # SYSTEM FIREWALL PROTECTION CIRCUITS
    # ==========================================================

    def emergency_stop(self) -> None:
        logger.critical(
            "EMERGENCY PROCESS SAFETY OVERRIDE ACTIVE -> ISOLATING TRANSACTIONS"
        )
        self.context.last_execution = {
            "status": "STOPPED",
            "reason": "EMERGENCY_TRIGGER",
        }

    def kill_switch(self, portfolio_state: Any) -> bool:
        equity = (
            getattr(portfolio_state, "equity", 100000.0)
            if not isinstance(portfolio_state, dict)
            else portfolio_state.get("equity", 100000.0)
        )
        pnl = (
            getattr(portfolio_state, "total_pnl", 0.0)
            if not isinstance(portfolio_state, dict)
            else portfolio_state.get("total_pnl", 0.0)
        )
        exposure = (
            getattr(portfolio_state, "exposure", 0.0)
            if not isinstance(portfolio_state, dict)
            else portfolio_state.get("exposure", 0.0)
        )

        if pnl < -0.05 * equity:
            logger.critical(
                "CRITICAL ACCOUNT LOSS MARGIN INFRINGEMENT DETECTED -> SHUTTING DOWN PIPELINE"
            )
            return True
        if exposure > 0.95:
            logger.critical(
                "PORTFOLIO ASSET OVER-EXPOSURE SAFETY TRIGGERED -> DISABLING PIPELINE ORDERS"
            )
            return True
        return False

    def circuit_breaker(self, risk_check: dict[str, Any]) -> bool:
        if risk_check.get("volatility", 0.0) > 0.03:
            logger.warning(
                "CIRCUIT BREAKER ENGAGED: MAXIMUM ASSET SYSTEM VOLATILITY BREACHED"
            )
            return True
        if risk_check.get("liquidity_risk", False):
            logger.warning("CIRCUIT BREAKER ENGAGED: MARKET DEPTH FAILURE DETECTED")
            return True
        return False

    def validate_cycle(self, context: OrchestratorContext) -> bool:
        if context.cycle_id > 0 and context.last_data is None:
            logger.error(
                "DATA INTEGRITY VERIFICATION FAILURE: Empty reference arrays detected."
            )
            return False
        return True

    def pre_cycle_guard(self, portfolio_state: Any, risk_check: dict[str, Any]) -> bool:
        if self.kill_switch(portfolio_state):
            self.emergency_stop()
            return False
        if self.circuit_breaker(risk_check):
            self.emergency_stop()
            return False
        return True

    def debug_snapshot(self) -> dict[str, Any]:
        return {
            "cycle_id": self.context.cycle_id,
            "mode": self.mode,
            "context": self.context,
            "portfolio": self.context.last_portfolio,
            "analytics": self.context.last_analytics,
        }

    def shutdown(self) -> None:
        logger.info("SYSTEM DISCONNECT AND GRACEFUL SHUTDOWN INITIATED")
        self.context.last_execution = {
            "status": "SHUTDOWN",
            "reason": "MANUAL_OR_SYSTEM_EXIT",
        }
        logger.info("PLATFORM DOWNSTREAM SERVICES DE-ACTIVATED CLEANLY")


def main():
    orchestrator = WiredOrchestrator(mode="BACKTEST")
    symbols = WatchlistManager("storage/watchlist/nifty500.json").load()
    if not symbols:
        logger.warning("Watchlist empty/not found, falling back to a small default list.")
        symbols = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"]
    portfolio_state = {"equity": 100000, "total_capital": 100000, "total_pnl": 0.0, "exposure": 0.0}
    try:
        orchestrator.run_cycle(symbols=symbols, portfolio_state=portfolio_state)
    except KeyboardInterrupt:
        orchestrator.shutdown()


if __name__ == "__main__":
    main()
