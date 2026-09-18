import time
from core.logger import get_logger
from execution.broker import MARKET, OrderRequest

logger = get_logger(__name__)


class TradeExecutor:

    def __init__(self, broker, risk_manager):

        self.broker = broker

        self.risk_manager = risk_manager

        self.executed_ids = set()

    def execute(self, orders, portfolio):

        results = []

        for order in orders:

            # prevent duplicate execution

            if order.get("id") in self.executed_ids:

                continue

            start = time.time()

            risk = self.risk_manager.evaluate_order(order, portfolio)

            if not risk.get("approved"):

                continue

            try:

                # BUGFIX (2026-09-18, Phase 4 dead-code cleanup, audit item
                # "execution/trade_executor.py's TradeExecutor.execute()
                # broker.place_order() ko galat signature se call karta
                # hai"): this used to call
                # `self.broker.place_order(order)` with the raw dict as a
                # single positional argument. `BrokerEngine.place_order()`'s
                # real signature is
                # `place_order(order: OrderRequest, market_price: float,
                # market_state: dict)` (see the live caller in
                # orchestrator.py) -- calling it with just one positional
                # argument would raise `TypeError: place_order() missing 2
                # required positional arguments`, and even that one
                # argument was the wrong shape: `place_order()` reads
                # `order.symbol` / `order.action` / `order.quantity` as
                # OrderRequest attributes, not dict keys. This class has no
                # live caller anywhere in the codebase today (confirmed via
                # repo-wide grep), so this never actually fired in
                # production -- fixed here, mirroring orchestrator.py's
                # real call pattern, so it is correct if this executor is
                # ever wired back into a live pipeline.
                order_request = OrderRequest(
                    symbol=order.get("symbol"),
                    action=order.get("action"),
                    quantity=order.get("quantity"),
                    order_type=order.get("order_type", MARKET),
                    limit_price=order.get("limit_price"),
                    strategy_tag=order.get("strategy_tag", "SYSTEM"),
                )

                result = self.broker.place_order(
                    order=order_request,
                    market_price=order.get("market_price", 0.0),
                    market_state=order.get("market_state", {}),
                )

                latency = time.time() - start

                self.executed_ids.add(order.get("id"))

                results.append(
                    {
                        "order": order,
                        "status": "EXECUTED",
                        "latency": latency,
                        "result": result,
                    }
                )

            except Exception as e:

                logger.error(f"EXECUTION FAILED: {e}")

        return results
