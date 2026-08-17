"""
Broker Execution Engine

Institutional Production Version

Responsibilities
----------------
• Execute BUY/SELL orders
• Manage order lifecycle
• Simulate or send live trades
• Handle slippage, fees, fills
• Maintain execution state

This layer is the ONLY authority allowed to:
→ Confirm trades
→ Modify position holdings
→ Record execution fills
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import random
import time
import pandas as pd

from core.logger import get_logger

logger = get_logger(__name__)


# ==========================================================
# ORDER TYPES
# ==========================================================

MARKET = "MARKET"
LIMIT = "LIMIT"


# ==========================================================
# ORDER STATUS
# ==========================================================

PENDING = "PENDING"
FILLED = "FILLED"
REJECTED = "REJECTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"


# ==========================================================
# ORDER REQUEST
# ==========================================================


@dataclass(slots=True)
class OrderRequest:

    symbol: str

    action: str  # BUY / SELL

    quantity: int

    order_type: str = MARKET

    limit_price: float | None = None

    strategy_tag: str = "SYSTEM"


# ==========================================================
# ORDER RESULT
# ==========================================================


@dataclass(slots=True)
class OrderResult:

    symbol: str

    status: str

    filled_quantity: int

    avg_price: float

    slippage: float

    brokerage: float

    timestamp: float

    diagnostics: dict[str, Any] = field(default_factory=dict)


# ==========================================================
# BROKER ENGINE
# ==========================================================


class BrokerEngine:
    """
    Execution Layer Engine
    """

    def __init__(self):

        self.execution_latency_ms = (50, 250)

        self.max_slippage = 0.002  # 0.2%

        self.brokerage_per_trade = 20.0

        logger.info("Broker Engine initialized.")

    # ==========================================================
    # ORDER EXECUTION
    # ==========================================================

    def place_order(
        self,
        order: OrderRequest,
        market_price: float,
        market_state: dict[str, Any],
    ) -> OrderResult:

        logger.info(
            "Placing order %s %s x%d",
            order.symbol,
            order.action,
            order.quantity,
        )

        start_time = time.time()

        # ==========================================================
        # SIMULATED LATENCY
        # ==========================================================

        latency = random.randint(
            *self.execution_latency_ms,
        )

        time.sleep(latency / 1000.0)

        # ==========================================================
        # BASIC VALIDATION
        # ==========================================================

        if order.quantity <= 0:

            logger.warning(
                "Rejected order: invalid quantity %s",
                order.quantity,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={"reason": "Invalid quantity"},
            )

        # ==========================================================
        # MARKET AVAILABILITY CHECK
        # ==========================================================

        market_open = market_state.get(
            "market_open",
            True,
        )

        if not market_open:

            logger.warning(
                "Market closed for %s",
                order.symbol,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={"reason": "Market closed"},
            )
        # ==========================================================
        # FILL PROBABILITY MODEL
        # ==========================================================

        volatility = float(
            market_state.get(
                "volatility",
                0.01,
            )
        )

        spread = float(
            market_state.get(
                "spread",
                0.001,
            )
        )

        # ==========================================================
        # ORDER REJECTION HANDLING (EXTENDED)
        # ==========================================================
        # Phase 24 (see PHASE24_NOTES.md): this check existed in the file
        # already, but sat AFTER an unconditional `return result` later in
        # this function — 100% unreachable, regardless of market_state
        # content, for as long as this function has had that early return.
        # Moved here (still before any fill/slippage simulation runs) so a
        # circuit-breaker-active or extreme-spread/volatility order is
        # rejected outright instead of being simulated through a fill.
        # NOTE: the ~400 lines that followed this block in its old
        # location (retry mechanism, order-book simulation, execution
        # quality scoring, detailed fee breakdown, etc.) are ALSO
        # unreachable dead code, still after that same early return — NOT
        # touched here, flagged separately as its own, much bigger
        # question (see PHASE24_NOTES.md).

        rejection_reason = None

        if market_state.get("circuit_breaker", False):

            rejection_reason = "Circuit breaker active"

        elif spread > 0.05:

            rejection_reason = "Spread too high"

        elif volatility > 0.2:

            rejection_reason = "Extreme volatility"

        if rejection_reason:

            logger.warning(
                "Order rejected %s | %s",
                order.symbol,
                rejection_reason,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={"reason": rejection_reason},
            )

        base_fill_prob = 0.98

        fill_penalty = volatility * 10 + spread * 20

        fill_probability = max(
            0.50,
            base_fill_prob - fill_penalty,
        )

        diagnostics = {
            "fill_probability": round(
                fill_probability,
                4,
            ),
            "volatility": volatility,
            "spread": spread,
        }

        # ==========================================================
        # DETERMINE FILL
        # ==========================================================

        if random.random() > fill_probability:

            logger.warning(
                "Order not filled due to market conditions %s",
                order.symbol,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={
                    **diagnostics,
                    "reason": "Low fill probability",
                },
            )

        # ==========================================================
        # SLIPPAGE MODEL
        # ==========================================================

        direction = 1 if order.action == "BUY" else -1

        slippage_factor = random.uniform(
            0,
            self.max_slippage,
        ) * (1 + volatility * 5)

        slippage = slippage_factor * direction

        execution_price = market_price * (1 + slippage)

        # ==========================================================
        # BROKERAGE COST
        # ==========================================================

        brokerage = self.brokerage_per_trade

        # ==========================================================
        # INITIAL FILL QUANTITY
        # ==========================================================

        filled_quantity = order.quantity

        partial_fill_chance = min(
            0.3,
            volatility * 5,
        )
        # ==========================================================
        # PARTIAL FILL ENGINE
        # ==========================================================

        partial_fill = random.random() < partial_fill_chance

        if partial_fill:

            fill_ratio = random.uniform(0.3, 1.0)

            filled_quantity = int(order.quantity * fill_ratio)

            filled_quantity = max(
                1,
                filled_quantity,
            )

            status = PARTIALLY_FILLED

        else:

            status = FILLED

            filled_quantity = order.quantity

        diagnostics["partial_fill"] = partial_fill

        diagnostics["filled_quantity"] = filled_quantity

        diagnostics["fill_ratio"] = round(
            filled_quantity / max(order.quantity, 1),
            4,
        )

        # ==========================================================
        # AVERAGE EXECUTION PRICE
        # ==========================================================

        avg_price = execution_price

        if filled_quantity < order.quantity:

            # simulate worse price for remaining liquidity

            adverse_move = random.uniform(
                0,
                self.max_slippage * 2,
            )

            avg_price *= 1 + adverse_move * direction

        diagnostics["execution_price"] = round(
            execution_price,
            4,
        )

        diagnostics["avg_price"] = round(
            avg_price,
            4,
        )

        # ==========================================================
        # MARKET IMPACT SIMULATION
        # ==========================================================

        market_impact = (
            (
                filled_quantity
                / max(
                    order.quantity,
                    1,
                )
            )
            * spread
            * 5
        )

        avg_price *= 1 + market_impact * direction

        diagnostics["market_impact"] = round(
            market_impact,
            6,
        )

        diagnostics["final_avg_price"] = round(
            avg_price,
            4,
        )

        # ==========================================================
        # FINALIZE ORDER RESULT
        # ==========================================================

        execution_time = time.time() - start_time

        result = OrderResult(
            symbol=order.symbol,
            status=status,
            filled_quantity=filled_quantity,
            avg_price=round(avg_price, 4),
            slippage=round(slippage, 6),
            brokerage=brokerage,
            timestamp=time.time(),
            diagnostics={
                **diagnostics,
                "execution_time_ms": round(
                    execution_time * 1000,
                    2,
                ),
            },
        )

        logger.info(
            "Order executed %s %s | qty=%d | price=%.4f",
            order.symbol,
            status,
            filled_quantity,
            avg_price,
        )

        return result
        # ==========================================================
        # ORDER REJECTION HANDLING (EXTENDED)
        # ==========================================================

        rejection_reason = None

        if market_state.get("circuit_breaker", False):

            rejection_reason = "Circuit breaker active"

        elif spread > 0.05:

            rejection_reason = "Spread too high"

        elif volatility > 0.2:

            rejection_reason = "Extreme volatility"

        if rejection_reason:

            logger.warning(
                "Order rejected %s | %s",
                order.symbol,
                rejection_reason,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={
                    **diagnostics,
                    "reason": rejection_reason,
                },
            )

        # ==========================================================
        # RETRY MECHANISM
        # ==========================================================

        max_retries = int(
            market_state.get(
                "max_retries",
                1,
            )
        )

        retry_count = 0

        final_result = None

        while retry_count <= max_retries:

            retry_count += 1

            retry_slippage_adjust = retry_count * 0.0005

            adjusted_price = execution_price * (1 + retry_slippage_adjust * direction)

            retry_success = random.random() < (fill_probability + 0.05 * retry_count)

            if retry_success:

                final_result = {
                    "price": adjusted_price,
                    "filled": filled_quantity,
                    "status": (
                        FILLED
                        if filled_quantity == order.quantity
                        else PARTIALLY_FILLED
                    ),
                }

                break
        # ==========================================================
        # RETRY FAILURE FALLBACK
        # ==========================================================

        if final_result is None:

            logger.error(
                "Order failed after retries %s",
                order.symbol,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={
                    **diagnostics,
                    "reason": "Max retries exceeded",
                    "retry_count": retry_count,
                },
            )

        # ==========================================================
        # APPLY FINAL RETRY RESULT
        # ==========================================================

        execution_price = final_result["price"]

        status = final_result["status"]

        filled_quantity = final_result["filled"]

        diagnostics["retry_count"] = retry_count

        diagnostics["retry_price"] = round(
            execution_price,
            4,
        )

        diagnostics["retry_status"] = status

        # ==========================================================
        # ORDER BOOK SIMULATION (LIGHTWEIGHT)
        # ==========================================================

        order_book_state = {
            "symbol": order.symbol,
            "bid": market_price * (1 - spread),
            "ask": market_price * (1 + spread),
            "last": market_price,
            "volume_pressure": random.uniform(0, 1),
            "liquidity_depth": random.uniform(0.2, 1.0),
        }

        diagnostics["order_book"] = {
            "bid": round(order_book_state["bid"], 4),
            "ask": round(order_book_state["ask"], 4),
            "spread": round(spread, 6),
            "liquidity_depth": round(order_book_state["liquidity_depth"], 4),
        }

        # ==========================================================
        # EXECUTION QUALITY SCORE
        # ==========================================================

        execution_quality = (
            fill_probability * 40
            + (1 - volatility) * 25
            + (1 - spread * 10) * 20
            + order_book_state["liquidity_depth"] * 15
        )

        diagnostics["execution_quality"] = round(
            execution_quality,
            2,
        )

        # ==========================================================
        # FINAL AUDIT FLAG
        # ==========================================================

        audit_flag = False

        if execution_quality < 40:

            audit_flag = True

        diagnostics["audit_flag"] = audit_flag
        # ==========================================================
        # EXECUTION STATE REGISTRY
        # ==========================================================

        execution_record = {
            "symbol": order.symbol,
            "action": order.action,
            "order_type": order.order_type,
            "requested_quantity": order.quantity,
            "filled_quantity": filled_quantity,
            "avg_price": round(execution_price, 4),
            "status": status,
            "retry_count": retry_count,
            "execution_quality": round(execution_quality, 2),
            "timestamp": time.time(),
        }

        diagnostics["execution_record"] = execution_record

        # ==========================================================
        # BROKER FEE CALCULATION (DETAILED)
        # ==========================================================

        base_fee = self.brokerage_per_trade

        turnover_fee = filled_quantity * execution_price * 0.0001

        slippage_cost = abs(execution_price - market_price) * filled_quantity

        total_cost = base_fee + turnover_fee + slippage_cost

        diagnostics["brokerage_breakdown"] = {
            "base_fee": round(base_fee, 2),
            "turnover_fee": round(turnover_fee, 4),
            "slippage_cost": round(slippage_cost, 4),
            "total_cost": round(total_cost, 4),
        }

        # ==========================================================
        # FILL ADJUSTMENT FINALIZATION
        # ==========================================================

        fill_ratio = filled_quantity / max(order.quantity, 1)

        if fill_ratio < 1.0:

            status = PARTIALLY_FILLED

        diagnostics["final_fill_ratio"] = round(
            fill_ratio,
            4,
        )

        # ==========================================================
        # LATENCY REFINEMENT
        # ==========================================================

        total_latency = (time.time() - start_time) * 1000

        diagnostics["total_latency_ms"] = round(
            total_latency,
            2,
        )

        # ==========================================================
        # PRE-FINAL AUDIT CHECK
        # ==========================================================

        audit_warnings = []

        if total_cost > (market_price * order.quantity * 0.01):

            audit_warnings.append("High execution cost detected")

        if fill_ratio < 0.5:

            audit_warnings.append("Low fill ratio detected")

        if execution_quality < 30:

            audit_warnings.append("Poor execution quality")

        diagnostics["audit_warnings"] = audit_warnings
        # ==========================================================
        # FINAL PRICE NORMALIZATION
        # ==========================================================

        final_avg_price = float(execution_price)

        if fill_ratio < 1.0:

            final_avg_price = (execution_price * fill_ratio) + (
                market_price * (1 - fill_ratio)
            )

        diagnostics["final_avg_price"] = round(
            final_avg_price,
            4,
        )

        # ==========================================================
        # EXECUTION IMPACT ADJUSTMENT
        # ==========================================================

        market_impact_adjustment = (filled_quantity * spread * 2) * (1 + volatility * 3)

        adjusted_execution_cost = total_cost + market_impact_adjustment

        diagnostics["market_impact_adjustment"] = round(
            market_impact_adjustment,
            6,
        )

        diagnostics["adjusted_execution_cost"] = round(
            adjusted_execution_cost,
            4,
        )

        # ==========================================================
        # FILL QUALITY METRIC
        # ==========================================================

        fill_quality = (
            (fill_ratio * 50)
            + (execution_quality * 0.3)
            + ((1 - volatility) * 20)
            + ((1 - spread * 10) * 10)
        )

        fill_quality = max(
            0.0,
            min(
                100.0,
                fill_quality,
            ),
        )

        diagnostics["fill_quality"] = round(
            fill_quality,
            2,
        )

        # ==========================================================
        # EXECUTION CLASSIFICATION
        # ==========================================================

        if fill_quality >= 80:

            execution_class = "EXCELLENT"

        elif fill_quality >= 60:

            execution_class = "GOOD"

        elif fill_quality >= 40:

            execution_class = "AVERAGE"

        else:

            execution_class = "POOR"

        diagnostics["execution_class"] = execution_class
        # ==========================================================
        # FINAL EXECUTION VALIDATION
        # ==========================================================

        validation_errors = []

        if filled_quantity <= 0:

            validation_errors.append("No quantity filled")

        if final_avg_price <= 0:

            validation_errors.append("Invalid execution price")

        if fill_ratio > 1.0:

            validation_errors.append("Fill ratio corruption")

        if execution_class == "POOR":

            validation_errors.append("Poor execution quality")

        diagnostics["validation_errors"] = validation_errors

        # ==========================================================
        # FAIL SAFE HANDLING
        # ==========================================================

        if validation_errors:

            logger.error(
                "Execution validation failed %s | %s",
                order.symbol,
                validation_errors,
            )

            return OrderResult(
                symbol=order.symbol,
                status=REJECTED,
                filled_quantity=0,
                avg_price=0.0,
                slippage=0.0,
                brokerage=0.0,
                timestamp=time.time(),
                diagnostics={
                    **diagnostics,
                    "reason": "Execution validation failed",
                },
            )

        # ==========================================================
        # FINAL ORDER RESULT BUILD
        # ==========================================================

        result = OrderResult(
            symbol=order.symbol,
            status=status,
            filled_quantity=filled_quantity,
            avg_price=round(final_avg_price, 4),
            slippage=round(slippage, 6),
            brokerage=round(adjusted_execution_cost, 4),
            timestamp=time.time(),
            diagnostics={
                **diagnostics,
                "execution_class": execution_class,
                "fill_ratio": round(fill_ratio, 4),
            },
        )

        # ==========================================================
        # FINAL LOGGING
        # ==========================================================

        logger.info(
            "Order finalized %s | %s | qty=%d | price=%.4f",
            order.symbol,
            status,
            filled_quantity,
            final_avg_price,
        )

        return result

    # ==========================================================
    # BROKER EXECUTION SUMMARY
    # ==========================================================

    @staticmethod
    def summary(
        results: list[OrderResult],
    ) -> str:

        total = len(results)

        filled = sum(r.status == FILLED for r in results)

        partial = sum(r.status == PARTIALLY_FILLED for r in results)

        rejected = sum(r.status == REJECTED for r in results)

        avg_slippage = round(
            sum(abs(r.slippage) for r in results) / max(total, 1),
            6,
        )

        avg_fill_qty = round(
            sum(r.filled_quantity for r in results) / max(total, 1),
            2,
        )

        return (
            f"Orders={total}"
            f" | Filled={filled}"
            f" | Partial={partial}"
            f" | Rejected={rejected}"
            f" | AvgSlippage={avg_slippage}"
            f" | AvgFillQty={avg_fill_qty}"
        )

    # ==========================================================
    # BROKER PERFORMANCE METRICS
    # ==========================================================

    @staticmethod
    def performance_metrics(
        results: list[OrderResult],
    ) -> dict[str, Any]:

        total_orders = len(results)

        fill_rate = sum(r.status in (FILLED, PARTIALLY_FILLED) for r in results) / max(
            total_orders, 1
        )

        rejection_rate = sum(r.status == REJECTED for r in results) / max(
            total_orders, 1
        )

        avg_cost = sum(r.brokerage for r in results) / max(total_orders, 1)

        avg_execution_time = sum(
            r.diagnostics.get(
                "execution_time_ms",
                0,
            )
            for r in results
        ) / max(total_orders, 1)

        return {
            "fill_rate": round(fill_rate, 4),
            "rejection_rate": round(rejection_rate, 4),
            "avg_cost": round(avg_cost, 4),
            "avg_execution_time_ms": round(
                avg_execution_time,
                2,
            ),
        }

    # ==========================================================
    # BROKER HEALTH CHECK
    # ==========================================================

    @staticmethod
    def health_check(
        results: list[OrderResult],
    ) -> dict[str, Any]:

        metrics = BrokerEngine.performance_metrics(results)

        health_score = (
            metrics["fill_rate"] * 50
            + (1 - metrics["rejection_rate"]) * 30
            + (1 - min(metrics["avg_cost"], 1)) * 10
            + (1 - min(metrics["avg_execution_time_ms"] / 1000, 1)) * 10
        )

        health_score = max(
            0.0,
            min(
                100.0,
                health_score,
            ),
        )

        status = (
            "HEALTHY"
            if health_score >= 75
            else "DEGRADED" if health_score >= 50 else "CRITICAL"
        )

        return {
            "health_score": round(health_score, 2),
            "status": status,
            "metrics": metrics,
        }

    # ==========================================================
    # BROKER DEBUG REPORT
    # ==========================================================

    @staticmethod
    def debug_report(
        results: list[OrderResult],
    ) -> str:

        report: list[str] = []

        report.append("=" * 120)
        report.append("BROKER EXECUTION REPORT")
        report.append("=" * 120)
        report.append("")

        report.append(BrokerEngine.summary(results))

        report.append("")
        report.append("-" * 120)
        report.append("DETAILED EXECUTION LOG")
        report.append("-" * 120)

        for r in results:

            report.append(
                f"{r.symbol:<15}"
                f"{r.status:<18}"
                f"{r.filled_quantity:<10}"
                f"{r.avg_price:<15.4f}"
                f"{r.slippage:<12.6f}"
                f"{r.brokerage:<12.2f}"
            )

        report.append("")
        report.append("-" * 120)
        report.append("EXECUTION QUALITY BREAKDOWN")
        report.append("-" * 120)

        for r in results:

            report.append("")
            report.append(f"[{r.symbol}]")

            for k, v in sorted(r.diagnostics.items()):

                if isinstance(v, (dict, list)):

                    continue

                report.append(f"{k:<30} : {v}")

        report.append("")
        report.append("=" * 120)
        report.append("END BROKER REPORT")
        report.append("=" * 120)

        return "\n".join(report)

    # ==========================================================
    # ORDER EXPORT UTILITIES
    # ==========================================================

    @staticmethod
    def export_dataframe(
        results: list[OrderResult],
    ) -> pd.DataFrame:

        rows = []

        for r in results:

            rows.append(
                {
                    "Symbol": r.symbol,
                    "Status": r.status,
                    "FilledQty": r.filled_quantity,
                    "AvgPrice": r.avg_price,
                    "Slippage": r.slippage,
                    "Brokerage": r.brokerage,
                    "Timestamp": r.timestamp,
                }
            )

        return pd.DataFrame(rows)

    # ==========================================================
    # CSV EXPORT
    # ==========================================================

    @staticmethod
    def export_csv(
        results: list[OrderResult],
        filename: str,
    ) -> None:

        df = BrokerEngine.export_dataframe(results)

        df.to_csv(
            filename,
            index=False,
        )

        logger.info(
            "Broker CSV exported: %s",
            filename,
        )

    # ==========================================================
    # JSON EXPORT
    # ==========================================================

    @staticmethod
    def export_json(
        results: list[OrderResult],
        filename: str,
    ) -> None:

        df = BrokerEngine.export_dataframe(results)

        df.to_json(
            filename,
            orient="records",
            indent=4,
        )

        logger.info(
            "Broker JSON exported: %s",
            filename,
        )

    # ==========================================================
    # BROKER SYSTEM SUMMARY
    # ==========================================================

    @staticmethod
    def system_summary(
        results: list[OrderResult],
    ) -> dict[str, Any]:

        total_orders = len(results)

        filled = sum(r.status == FILLED for r in results)

        partial = sum(r.status == PARTIALLY_FILLED for r in results)

        rejected = sum(r.status == REJECTED for r in results)

        total_volume = sum(r.filled_quantity for r in results)

        total_brokerage = sum(r.brokerage for r in results)

        avg_slippage = sum(abs(r.slippage) for r in results) / max(total_orders, 1)

        execution_success_rate = ((filled + partial) / max(total_orders, 1)) * 100

        return {
            "total_orders": total_orders,
            "filled": filled,
            "partial": partial,
            "rejected": rejected,
            "total_volume": total_volume,
            "total_brokerage": round(total_brokerage, 2),
            "avg_slippage": round(avg_slippage, 6),
            "execution_success_rate": round(execution_success_rate, 2),
        }

    # ==========================================================
    # BROKER HEALTH DASHBOARD VIEW
    # ==========================================================

    @staticmethod
    def health_dashboard(
        results: list[OrderResult],
    ) -> str:

        metrics = BrokerEngine.performance_metrics(results)

        health = BrokerEngine.health_check(results)

        summary = BrokerEngine.system_summary(results)

        lines = []

        lines.append("=" * 120)
        lines.append("BROKER HEALTH DASHBOARD")
        lines.append("=" * 120)
        lines.append("")

        lines.append("SYSTEM METRICS")
        lines.append("-" * 60)

        for k, v in metrics.items():

            lines.append(f"{k:<30} : {v}")

        lines.append("")
        lines.append("HEALTH STATUS")
        lines.append("-" * 60)

        lines.append(f"Health Score           : {health['health_score']}")

        lines.append(f"Status                 : {health['status']}")

        lines.append("")
        lines.append("SYSTEM SUMMARY")
        lines.append("-" * 60)

        for k, v in summary.items():

            lines.append(f"{k:<30} : {v}")

        lines.append("")
        lines.append("=" * 120)
        lines.append("END BROKER DASHBOARD")
        lines.append("=" * 120)

        return "\n".join(lines)


# ==========================================================
# FINAL COMPLETION LOG
# ==========================================================

logger.info("Broker Engine fully initialized and operational.")


# ==========================================================
# END OF FILE
# ==========================================================
