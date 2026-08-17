"""
TRADE DIARY

Complete lifecycle diary for every paper-trade: entry reasoning, a daily
monitoring log (one entry per scan while the position is open), and exit
reasoning — all in one JSON file per trade so the full history of *why*
a trade was opened, held, and eventually closed is preserved and
auditable.

One file per trade: storage/trades/diary/<trade_id>.json
An index file (storage/trades/diary/_index.json) tracks open vs closed
trade IDs so callers don't need to scan the whole directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TradeDiary:

    def __init__(self, base_path: str = "storage/trades/diary"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_path / "_index.json"

    # ==========================================================
    # INDEX HELPERS
    # ==========================================================

    def _load_index(self) -> dict[str, list[str]]:
        if not self.index_path.exists():
            return {"open": [], "closed": []}
        with open(self.index_path) as f:
            return json.load(f)

    def _save_index(self, index: dict[str, list[str]]) -> None:
        with open(self.index_path, "w") as f:
            json.dump(index, f, indent=2)

    def _trade_path(self, trade_id: str) -> Path:
        return self.base_path / f"{trade_id}.json"

    # ==========================================================
    # LIFECYCLE
    # ==========================================================

    def open_trade(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_date: str,
        buy_probability: float,
        buy_confidence: float,
        entry_reasons: list[str],
    ) -> None:
        record = {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": direction,
            "entry_date": entry_date,
            "entry_price": entry_price,
            "buy_probability": buy_probability,
            "buy_confidence": buy_confidence,
            # Thesis-decay baseline (Point 16, PHASE28_NOTES.md) — always
            # None at open_trade() time. The real live entry path
            # (scripts/morning_executor.py) hardcodes buy_confidence/
            # buy_probability to 0.0 since it deliberately skips a full
            # re-scan at entry, so those two fields can't serve as a
            # genuine decay baseline. Populated instead by
            # capture_thesis_baseline() the first time monitoring
            # produces a real held-direction confidence value.
            "entry_thesis_confidence": None,
            "entry_reasons": entry_reasons,
            "daily_log": [],
            "status": "OPEN",
            "exit_date": None,
            "exit_price": None,
            "exit_reason": None,
            "holding_days": 0,
            "max_profit_percent": 0.0,
            "max_drawdown_percent": 0.0,
            "final_pnl": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with open(self._trade_path(trade_id), "w") as f:
            json.dump(record, f, indent=2)

        index = self._load_index()
        if trade_id not in index["open"]:
            index["open"].append(trade_id)
        self._save_index(index)

    def add_daily_log(
        self,
        trade_id: str,
        date: str,
        current_price: float,
        current_pnl: float,
        current_buy_confidence: float,
        current_sell_confidence: float,
        exit_score: float,
        recommendation: str,  # "HOLD" or "EXIT"
        notes: list[str] | None = None,
    ) -> None:
        record = self._read(trade_id)
        if record is None:
            return

        record["daily_log"].append({
            "date": date,
            "current_price": current_price,
            "current_pnl": current_pnl,
            "current_buy_confidence": current_buy_confidence,
            "current_sell_confidence": current_sell_confidence,
            "current_exit_score": exit_score,
            "recommendation": recommendation,
            "notes": notes or [],
        })
        record["holding_days"] = len(record["daily_log"])
        record["updated_at"] = time.time()

        self._write(trade_id, record)

    def capture_thesis_baseline(self, trade_id: str, confidence: float | None) -> None:
        """Capture this position's held-direction confidence as its
        permanent thesis-decay baseline — the FIRST time a real value is
        available. Idempotent: a no-op once a baseline is already
        stored, and a no-op (not a permanent skip) if `confidence` is
        None this cycle, so a transient diagnostics gap doesn't
        permanently block capture on a later cycle.

        See PHASE28_NOTES.md (Point 16): the live entry path
        (scripts/morning_executor.py) hardcodes open_trade()'s
        buy_confidence/buy_probability to 0.0 (it deliberately skips a
        full re-scan at entry), so those fields can't serve as a genuine
        decay baseline. This captures the first REAL confidence value
        computed once monitoring starts instead — see
        risk/exit_strategy.py's thesis-decay time exit, which compares
        each day's held-direction confidence against this baseline.
        """
        if confidence is None:
            return

        record = self._read(trade_id)
        if record is None:
            return

        if record.get("entry_thesis_confidence") is not None:
            return

        record["entry_thesis_confidence"] = confidence
        record["updated_at"] = time.time()

        self._write(trade_id, record)

    def close_trade(
        self,
        trade_id: str,
        exit_date: str,
        exit_price: float,
        exit_reason: str,
        final_pnl: float,
        max_profit_percent: float,
        max_drawdown_percent: float,
        final_pnl_percent: float = 0.0,
        exit_score: float = 0.0,
        target1_status: str = "N/A",
        target2_status: str = "N/A",
        stop_loss_status: str = "N/A",
    ) -> None:
        record = self._read(trade_id)
        if record is None:
            return

        record["status"] = "CLOSED"
        record["exit_date"] = exit_date
        record["exit_price"] = exit_price
        record["exit_reason"] = exit_reason
        record["final_pnl"] = final_pnl
        record["final_pnl_percent"] = final_pnl_percent
        record["max_profit_percent"] = max_profit_percent
        record["max_drawdown_percent"] = max_drawdown_percent
        record["exit_score"] = exit_score
        record["target1_status"] = target1_status
        record["target2_status"] = target2_status
        record["stop_loss_status"] = stop_loss_status
        record["updated_at"] = time.time()

        self._write(trade_id, record)

        index = self._load_index()
        if trade_id in index["open"]:
            index["open"].remove(trade_id)
        if trade_id not in index["closed"]:
            index["closed"].append(trade_id)
        self._save_index(index)

    # ==========================================================
    # READ
    # ==========================================================

    def _read(self, trade_id: str) -> dict[str, Any] | None:
        path = self._trade_path(trade_id)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def _write(self, trade_id: str, record: dict[str, Any]) -> None:
        with open(self._trade_path(trade_id), "w") as f:
            json.dump(record, f, indent=2)

    def get_diary(self, trade_id: str) -> dict[str, Any] | None:
        return self._read(trade_id)

    def list_open_trade_ids(self) -> list[str]:
        return self._load_index().get("open", [])

    def list_closed_trade_ids(self) -> list[str]:
        return self._load_index().get("closed", [])

    def get_open_trades(self) -> list[dict[str, Any]]:
        return [r for tid in self.list_open_trade_ids() if (r := self._read(tid)) is not None]

    def get_closed_trades(self) -> list[dict[str, Any]]:
        return [r for tid in self.list_closed_trade_ids() if (r := self._read(tid)) is not None]
