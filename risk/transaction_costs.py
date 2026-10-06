"""
TRANSACTION COSTS (2026-10-06, BUG_AUDIT_2026-10-05_PROFITABILITY.md H13)

Paper trading and the backtest used to book every trade at the raw
market price — zero STT, stamp duty, exchange fees, DP charges or
slippage — so every P&L number was gross and the system looked better
than it could ever be with real money (estimated ~₹14,600 of costs over
the first 519 trades).

Design: costs are booked AT EXIT, as an adjustment to the exit price
fed to PortfolioEngine (both legs' costs together). Entry prices stay
the real market fill, so stop-loss / target levels — computed from the
entry price — are not shifted by costs, and exit behavior is unchanged.
Side effect, accepted: an open position's unrealized P&L does not yet
include its costs; they appear in realized P&L when it closes.

Rates live in config.py (CONFIG.cost_*), sourced there.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    buy_pct: float = 0.0          # % of traded value, buy leg
    sell_pct: float = 0.0         # % of traded value, sell leg
    sell_flat_rupees: float = 0.0  # flat charge per DELIVERY sell (DP charge)
    slippage_pct: float = 0.0     # % of traded value, every leg

    @classmethod
    def from_config(cls) -> "CostModel":
        from config import CONFIG

        if not CONFIG.apply_transaction_costs:
            return cls()
        return cls(
            buy_pct=CONFIG.cost_buy_pct,
            sell_pct=CONFIG.cost_sell_pct,
            sell_flat_rupees=CONFIG.cost_sell_flat_rupees,
            slippage_pct=CONFIG.slippage_pct_per_side,
        )


ZERO_COSTS = CostModel()


def round_trip_cost(
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    model: CostModel,
) -> float:
    """Rupee cost of opening AND closing `quantity` shares.

    BUY position : buy at entry, sell (delivery) at exit -> DP charge applies.
    SELL position: sell at entry, buy back at exit. No DP charge (no
    delivered shares leave the demat). NOTE (audit H14): an overnight
    short is not actually possible in the NSE cash segment; this only
    keeps the paper numbers from being cost-free.
    """
    if quantity <= 0:
        return 0.0
    entry_value = abs(entry_price) * quantity
    exit_value = abs(exit_price) * quantity
    slip = model.slippage_pct / 100.0
    if str(direction).upper() == "SELL":
        cost = entry_value * (model.sell_pct / 100.0 + slip)
        cost += exit_value * (model.buy_pct / 100.0 + slip)
    else:
        cost = entry_value * (model.buy_pct / 100.0 + slip)
        cost += exit_value * (model.sell_pct / 100.0 + slip)
        cost += model.sell_flat_rupees
    return cost


def net_exit_price(
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    model: CostModel,
) -> float:
    """The exit price that, fed to PortfolioEngine, books P&L net of the
    full round-trip cost for these `quantity` shares."""
    if quantity <= 0:
        return exit_price
    per_share = round_trip_cost(direction, entry_price, exit_price, quantity, model) / quantity
    # A long receives less on the sale; a short pays more to buy back.
    return exit_price + per_share if str(direction).upper() == "SELL" else exit_price - per_share
