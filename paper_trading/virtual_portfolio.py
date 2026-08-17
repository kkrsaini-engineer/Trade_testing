"""
VIRTUAL PORTFOLIO

Paper-trading portfolio built on top of the existing, already-audited
PortfolioEngine — not a parallel re-implementation. Adds two things
PortfolioEngine doesn't need for live trading but paper trading does:

1. PERSISTENCE — a daily script run is a fresh process, so the virtual
   portfolio's state (cash, open positions) must be saved after every run
   and reloaded at the start of the next one, or every "day" would reset
   to the initial capital.
2. SECTOR EXPOSURE tracking for reporting (needs the symbol->sector
   mapping, which PortfolioEngine itself doesn't know about).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from portfolio.portfolio import PortfolioEngine, PortfolioState, PortfolioPosition


class VirtualPortfolio:

    def __init__(
        self,
        initial_capital: float = 500_000.0,
        state_path: str = "storage/trades/virtual_portfolio_state.json",
    ):
        self.initial_capital = initial_capital
        self.state_path = Path(state_path)
        self.engine = self._load_or_init()

    # ==========================================================
    # PERSISTENCE
    # ==========================================================

    def _load_or_init(self) -> PortfolioEngine:
        if self.state_path.exists():
            with open(self.state_path) as f:
                saved = json.load(f)
            state = PortfolioState(
                total_capital=saved["total_capital"],
                available_capital=saved["available_capital"],
                used_capital=saved.get("used_capital", 0.0),
                total_pnl=saved.get("total_pnl", 0.0),
                total_pnl_percent=saved.get("total_pnl_percent", 0.0),
                exposure=saved.get("exposure", 0.0),
                risk_score=saved.get("risk_score", 0.0),
                # Phase 22: older saved state files predate these fields.
                # Default peak_equity/day_start_equity to total_capital (a
                # clean "no drawdown/loss yet" baseline) rather than 0.0 —
                # 0.0 would make snapshot() treat them as "not yet
                # initialized" and report 0% drawdown/loss regardless of
                # actual equity, which is also safe but less informative
                # than starting the tracker from a real number immediately.
                # current_trading_day defaults to "" so the very next
                # update_equity_tracking() call (always called at the top
                # of run_cycle()) is guaranteed to treat it as a new day
                # and (re)capture day_start_equity correctly.
                peak_equity=saved.get("peak_equity", saved["total_capital"]),
                day_start_equity=saved.get("day_start_equity", saved["total_capital"]),
                current_trading_day=saved.get("current_trading_day", ""),
                # Phase 23: same backward-compat reasoning as the daily
                # fields above.
                week_start_equity=saved.get("week_start_equity", saved["total_capital"]),
                current_trading_week=saved.get("current_trading_week", ""),
                month_start_equity=saved.get("month_start_equity", saved["total_capital"]),
                current_trading_month=saved.get("current_trading_month", ""),
            )
            engine = PortfolioEngine(state=state)
            for symbol, pos in saved.get("open_positions", {}).items():
                engine.state.open_positions[symbol] = PortfolioPosition(**pos)
            for pos in saved.get("closed_positions", []):
                engine.state.closed_positions.append(PortfolioPosition(**pos))
            self._symbol_sector = saved.get("symbol_sector", {})
            return engine

        self._symbol_sector = {}
        state = PortfolioState(
            total_capital=self.initial_capital,
            available_capital=self.initial_capital,
        )
        return PortfolioEngine(state=state)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        def pos_to_dict(p: PortfolioPosition) -> dict[str, Any]:
            return {
                "symbol": p.symbol, "quantity": p.quantity, "entry_price": p.entry_price,
                "current_price": p.current_price, "direction": p.direction,
                "unrealized_pnl": p.unrealized_pnl, "unrealized_pnl_percent": p.unrealized_pnl_percent,
                "realized_pnl": p.realized_pnl, "highest_price": p.highest_price,
                "lowest_price": p.lowest_price, "max_profit_percent": p.max_profit_percent,
                "max_drawdown_percent": p.max_drawdown_percent, "status": p.status,
                "updated_at": p.updated_at,
            }

        payload = {
            "total_capital": self.engine.state.total_capital,
            "available_capital": self.engine.state.available_capital,
            "used_capital": self.engine.state.used_capital,
            "total_pnl": self.engine.state.total_pnl,
            "total_pnl_percent": self.engine.state.total_pnl_percent,
            "exposure": self.engine.state.exposure,
            "risk_score": self.engine.state.risk_score,
            "peak_equity": self.engine.state.peak_equity,
            "day_start_equity": self.engine.state.day_start_equity,
            "current_trading_day": self.engine.state.current_trading_day,
            "week_start_equity": self.engine.state.week_start_equity,
            "current_trading_week": self.engine.state.current_trading_week,
            "month_start_equity": self.engine.state.month_start_equity,
            "current_trading_month": self.engine.state.current_trading_month,
            "open_positions": {
                sym: pos_to_dict(p) for sym, p in self.engine.state.open_positions.items()
            },
            "closed_positions": [pos_to_dict(p) for p in self.engine.state.closed_positions],
            "symbol_sector": self._symbol_sector,
            "saved_at": time.time(),
        }
        with open(self.state_path, "w") as f:
            json.dump(payload, f, indent=2)

    # ==========================================================
    # SECTOR EXPOSURE
    # ==========================================================

    def register_sector(self, symbol: str, sector: str | None) -> None:
        if sector:
            self._symbol_sector[symbol] = sector

    def sector_for(self, symbol: str) -> str | None:
        return self._symbol_sector.get(symbol)

    def sector_exposure(self) -> dict[str, float]:
        exposure: dict[str, float] = {}
        for symbol, pos in self.engine.state.open_positions.items():
            sector = self._symbol_sector.get(symbol, "Unknown")
            value = pos.quantity * pos.current_price
            exposure[sector] = exposure.get(sector, 0.0) + value
        return exposure

    # ==========================================================
    # SUMMARY
    # ==========================================================

    def snapshot(self) -> dict[str, Any]:
        s = self.engine.snapshot()
        closed = self.engine.state.closed_positions
        wins = sum(1 for p in closed if p.realized_pnl > 0)
        losses = sum(1 for p in closed if p.realized_pnl <= 0)
        total_closed = len(closed)

        s["portfolio_value"] = s["available_capital"] + sum(
            p.quantity * p.current_price for p in self.engine.state.open_positions.values()
        )
        # Use the PERSISTED total_capital as the return% baseline — NOT
        # self.initial_capital (a constructor-only default that is never
        # synced with the actual state file, so it silently goes stale
        # whenever total_capital is changed directly in the JSON).
        baseline_capital = self.engine.state.total_capital
        s["portfolio_return_percent"] = (
            (s["portfolio_value"] - baseline_capital) / baseline_capital * 100
            if baseline_capital else 0.0
        )
        s["win_rate"] = round(wins / total_closed * 100, 2) if total_closed else None
        s["loss_rate"] = round(losses / total_closed * 100, 2) if total_closed else None
        s["sector_exposure"] = self.sector_exposure()
        return s
