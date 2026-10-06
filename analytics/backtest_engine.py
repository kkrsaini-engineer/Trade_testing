"""
PHASE 2 — MODULE 4: INSTITUTIONAL BACKTESTING ENGINE

Replays historical OHLCV data day-by-day through the REAL production
pipeline (MarketScanner -> Morning-Executor rules -> ExitStrategyEngine ->
PortfolioEngine — see run()'s 2026-10-06 docstring), so the
backtest exercises the exact same code path as live/paper trading —
not a separate, parallel simulation that can drift out of sync.

This replaces analytics/backtester.py's run() method, which had several
integration bugs (orders built via `type("Order", (), dict)` instead of
the real OrderRequest dataclass, positions that were opened but never
closed so realized P&L was never captured, and a mathematically incorrect
entry-price back-calculation). Rather than patch that in place, this is a
fresh, tested implementation; analytics/backtester.py is left untouched
for now.

Usage:
    from analytics.backtest_engine import BacktestEngine
    engine = BacktestEngine()
    result = engine.run(historical_data, initial_capital=100000)
    print(result.report())
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.logger import get_logger
from data.data_engine import DataBundle
from execution.scanner import MarketScanner
from portfolio.portfolio import PortfolioEngine, PortfolioState

logger = get_logger(__name__)


@dataclass
class BacktestResult:
    equity_curve: list[float] = field(default_factory=list)
    dates: list[Any] = field(default_factory=list)
    regimes: list[str] = field(default_factory=list)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    reason: str = ""

    def report(self) -> str:
        if self.status != "OK":
            return (
                "=== INSTITUTIONAL BACKTEST REPORT ===\n"
                f"Status: {self.status}\n"
                f"Reason: {self.reason}"
            )
        m = self.metrics
        lines = [
            "=== INSTITUTIONAL BACKTEST REPORT ===",
            f"Trades              : {m.get('total_trades', 0)}",
            f"Win Rate             : {m.get('win_rate', 0):.2f}%",
            f"Profit Factor        : {m.get('profit_factor', 0):.2f}",
            f"CAGR                 : {m.get('cagr', 0):.2f}%",
            f"Max Drawdown         : {m.get('max_drawdown', 0):.2f}%",
            f"Sharpe Ratio         : {m.get('sharpe', 0):.2f}",
            f"Sortino Ratio        : {m.get('sortino', 0):.2f}",
            f"Expectancy           : {m.get('expectancy', 0):.2f}",
            f"Average R:R          : {m.get('avg_rr', 0):.2f}",
            f"BUY Accuracy         : {m.get('buy_accuracy', 0):.2f}%  ({m.get('buy_trades', 0)} trades)",
            f"SELL Accuracy        : {m.get('sell_accuracy', 0):.2f}% ({m.get('sell_trades', 0)} trades)",
            f"Positions Opened     : {m.get('opened_buy_count', 0)} BUY, {m.get('opened_sell_count', 0)} SELL"
            f" ({m.get('still_open_at_end', 0)} still open at backtest end)",
            f"Avg Holding (cycles) : {m.get('avg_holding_days', 0)}",
            f"Cost per side        : {m.get('cost_pct_per_side', 0)}%",
            f"False Positives      : {m.get('false_positives', 0)}",
            f"False Negatives      : {m.get('false_negatives', 0)} (see note below)",
            "",
            "NOTE: 'False Negatives' (good setups the engine incorrectly",
            "skipped) can't be measured from trade history alone — it",
            "requires re-scoring every NO_TRADE day against what actually",
            "happened next, which Module 1 (Analysis Engine) does",
            "separately using the full_report.csv history.",
            "",
            "CAVEAT: fundamentals are today's snapshot for every simulated",
            "day (no point-in-time history) and there is no historical news",
            "— fundamental/news-driven scores are optimistic.",
        ]
        exits = m.get("exit_breakdown") or {}
        if exits:
            lines.append("")
            lines.append("Exit breakdown:")
            for name, b in sorted(exits.items(), key=lambda kv: -kv[1]["trades"]):
                lines.append(f"  {name}: {b['trades']} trades, {b['wins']} wins, P&L {b['pnl']:.2f}")
        skips = m.get("entry_skips") or {}
        if skips:
            lines.append("")
            lines.append("Entries skipped at the open:")
            for name, count in sorted(skips.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {name}: {count}")
        error_count = m.get("error_count", 0)
        total_attempts = m.get("total_scan_attempts", 0)
        if error_count:
            error_rate = round(error_count / total_attempts * 100, 1) if total_attempts else 0.0
            lines.append("")
            lines.append(f"⚠️ Scan Errors: {error_count} / {total_attempts} attempts ({error_rate}%)")
            for err_type, count in m.get("error_breakdown", {}).items():
                sample = m.get("error_samples", {}).get(err_type, "")
                lines.append(f"  {err_type}: {count}x — e.g. \"{sample}\"")
            if error_rate > 50:
                lines.append("  NOTE: majority of attempts failed with errors — this likely explains a low/zero trade count above, not weak signal quality.")

        no_trade_count = m.get("no_trade_count", 0)
        blocked_count = m.get("blocked_by_portfolio_count", 0)
        if total_attempts and m.get("total_trades", 0) == 0:
            no_trade_rate = round(no_trade_count / total_attempts * 100, 1)
            lines.append("")
            lines.append(f"NO_TRADE breakdown: {no_trade_count} / {total_attempts} attempts ({no_trade_rate}%) rejected before signal, {blocked_count} signal(s) blocked by portfolio rules")
            for reason, count in m.get("no_trade_reasons", {}).items():
                lines.append(f"  {reason}: {count}x")
        return "\n".join(lines)


class BacktestEngine:
    """Institutional-grade backtester: replays real history through the
    real production scanner/broker/portfolio, across bull/bear/sideways
    and high/low volatility periods (whatever the input data covers)."""

    def __init__(self, scanner: MarketScanner | None = None):
        # FIX #4 (architecture review — backtest contamination): this
        # scanner instance is reused across the entire day-by-day replay
        # loop in run() below (hundreds of simulated days from ONE
        # MarketScanner object). MarketScanner's FII/DII / macro-news /
        # delivery-percentage lookups are lazy-fetch-once-and-cache —
        # correct for a single live scan, but here that meant one real
        # live snapshot (whatever was live on the day the backtest
        # happened to run) got silently reused for every simulated
        # historical day. disable_live_market_context=True routes all
        # three through their already-supported "no live data" fallback
        # instead — see MarketScanner.__init__'s NOTE for the full
        # explanation, including a correction of an earlier claim that
        # VIX also leaked here (it doesn't — VIX was never fetched in
        # this code path to begin with).
        #
        # Only applies when no scanner is explicitly passed in — a
        # caller supplying its own scanner is assumed to have already
        # made its own live-data decision.
        self.scanner = scanner or MarketScanner(disable_live_market_context=True)

    def run(
        self,
        historical_data: dict[str, pd.DataFrame],
        fundamentals: dict[str, dict] | None = None,
        initial_capital: float = 100000.0,
        min_history: int = 250,
        max_candidates_per_day: int = 100,
        cost_pct_per_side: float = 0.0,
    ) -> BacktestResult:
        """
        REWRITTEN 2026-10-06 (BUG_AUDIT_2026-10-05_PROFITABILITY.md M9).

        The old replay did not simulate the system that actually trades:
          - it FILLED at the signal bar's own CLOSE — the same close the
            signal was computed from (live: the night scan uses the
            close, and the Morning Executor fills at the NEXT day's open);
          - it had NO stop-loss, NO targets, NO ExitStrategyEngine and NO
            risk-based exit at all — a position only closed when an
            opposite BUY/SELL signal appeared, or at the end of the run;
          - it skipped every Morning-Executor rule (gap bands, gap-chase
            filter, stop/target sanity, capital check, sizing formula);
          - it sliced every symbol by ROW POSITION, which silently
            misaligns dates whenever one symbol has a missing day;
          - fills went through BrokerEngine's RANDOM slippage, so two
            runs of the same backtest never gave the same answer.
        Any threshold or R:R "tuned" on it would have been tuned on a
        different system.

        Now, for each trading day D (dates aligned by timestamp):
          1. OPEN of D: last night's candidates are executed exactly as
             scripts/morning_executor.py does — classify_gap() SKIP band,
             is_gap_chase(), open-vs-target1/stop sanity checks,
             check_capital_portfolio_risk(), the same sizing formula —
             filled at D's open (plus optional cost). News is not
             available historically, so the news check is skipped.
          2. Monitoring: every open position (including ones opened this
             morning) goes through MarketScanner.evaluate_position() and
             ExitStrategyEngine with the SAME position input live paper
             trading builds (paper_trading_engine.build_exit_position_
             input). D's full bar is used as "today", so a stop/target
             touched any time during D fills at the level (or the open if
             gapped through) — the same fill live gets via the 9:20 check
             plus the next morning's previous-session check (audit H1),
             only acted on a few hours earlier.
          3. CLOSE of D: the scan runs on data up to and including D and
             its top candidates become tomorrow's orders.
        No look-ahead: step 3 only sees bars <= D, and step 1 only uses
        D's open for the fill.

        cost_pct_per_side: optional round-trip cost model — each fill is
        made worse by this % (buy higher / sell lower). Default 0.0.

        fundamentals: still a static snapshot for every simulated day —
        point-in-time historical fundamentals are not available from
        this pipeline. This IS still optimistic for any fundamental-
        driven score; it is documented here and in the report, not fixed.
        """
        # Imported here, not at module top: scripts/morning_executor.py
        # pulls in yfinance/config at import time, which the rest of this
        # module doesn't need.
        import dataclasses

        from paper_trading.paper_trading_engine import build_exit_position_input
        from risk.exit_strategy import ExitStrategyEngine
        from scripts.morning_executor import (
            check_capital_portfolio_risk,
            classify_gap,
            is_gap_chase,
        )

        fundamentals = fundamentals or {}
        symbols = list(historical_data.keys())

        if not symbols:
            return BacktestResult(
                status="NOT_READY",
                reason=(
                    "No historical OHLCV dataset available. "
                    "Required: multi-year historical data."
                ),
            )

        data: dict[str, pd.DataFrame] = {}
        for sym, df in historical_data.items():
            frame = df.copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
            if getattr(frame["timestamp"].dt, "tz", None) is not None:
                frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
            frame = frame.sort_values("timestamp").reset_index(drop=True)
            data[sym] = frame

        longest = max(len(df) for df in data.values())
        if longest <= min_history:
            raise ValueError(
                f"Not enough history: longest series has {longest} rows, "
                f"need more than {min_history}."
            )

        all_dates = sorted({ts.normalize() for df in data.values() for ts in df["timestamp"]})

        state = PortfolioState(total_capital=initial_capital, available_capital=initial_capital)
        portfolio = PortfolioEngine(state=state)
        exit_engine = ExitStrategyEngine()
        result = BacktestResult()
        cost = max(float(cost_pct_per_side), 0.0) / 100.0

        broker_status = {
            "status": "ONLINE", "mode": "BACKTEST", "connected": True,
            "order_allowed": True, "available_margin": initial_capital,
        }
        market_state = {
            "max_trade_candidates": max_candidates_per_day,
            "max_watchlist": 50,
            # Without these, ValidationEngine defaults market_open to False
            # and rejects every simulated day with "Market is closed.".
            "market_open": True,
            "holiday": False,
        }

        pending: list[dict[str, Any]] = []
        meta: dict[str, dict[str, Any]] = {}

        counters = {
            "wins": 0, "losses": 0, "gross_profit": 0.0, "gross_loss": 0.0,
            "opened_buy": 0, "opened_sell": 0,
        }
        rr_values: list[float] = []
        error_type_counts: Counter = Counter()
        error_sample_messages: dict[str, str] = {}
        no_trade_reasons: Counter = Counter()
        entry_skips: Counter = Counter()
        no_trade_count = 0
        blocked_by_portfolio = 0
        total_scan_attempts = 0

        def rows_through(sym: str, day: pd.Timestamp) -> int:
            return int(data[sym]["timestamp"].searchsorted(day + pd.Timedelta(days=1), side="left"))

        def bar_on(sym: str, day: pd.Timestamp):
            n = rows_through(sym, day)
            if n == 0:
                return None, 0
            row = data[sym].iloc[n - 1]
            if row["timestamp"].normalize() != day:
                return None, n
            return row, n

        def fill(price: float, direction: str, opening: bool) -> float:
            # opening a BUY / closing a SELL = buying -> pay more
            buying = (direction == "BUY") == opening
            return price * (1 + cost) if buying else price * (1 - cost)

        def record_close(closed, exit_price: float, day: pd.Timestamp, reason: str) -> None:
            info = meta.pop(closed.symbol, {})
            pnl = closed.realized_pnl
            if pnl > 0:
                counters["wins"] += 1
                counters["gross_profit"] += pnl
            else:
                counters["losses"] += 1
                counters["gross_loss"] += abs(pnl)
            if closed.max_drawdown_percent > 0:
                rr_values.append(closed.max_profit_percent / max(closed.max_drawdown_percent, 1e-9))
            result.closed_trades.append({
                "symbol": closed.symbol,
                "direction": closed.direction,
                "entry_price": closed.entry_price,
                "exit_price": exit_price,
                "realized_pnl": pnl,
                "realized_pnl_percent": closed.realized_pnl_percent,
                "max_profit_percent": closed.max_profit_percent,
                "max_drawdown_percent": closed.max_drawdown_percent,
                "entry_date": str(info.get("entry_date", ""))[:10],
                "exit_date": str(day)[:10],
                "holding_days": info.get("cycles", 0),
                "exit_reason": reason,
            })

        for day in all_dates:
            day_str = day.date().isoformat()
            portfolio.update_equity_tracking(day_str)

            # ---------------- 1. OPEN: execute last night's candidates
            if pending:
                todays_orders, pending = pending, []
                for c in todays_orders:
                    sym, direction = c["symbol"], c["direction"]
                    row, _ = bar_on(sym, day)
                    if row is None:
                        entry_skips["no bar on execution day"] += 1
                        continue
                    open_price = float(row["open"])
                    prev_close = c.get("prev_close")
                    if not prev_close or open_price <= 0 or math.isnan(open_price):
                        entry_skips["no valid open/prev_close"] += 1
                        continue
                    band, _ratio = classify_gap(open_price, prev_close, c.get("atr_14"))
                    gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
                    if band == "SKIP":
                        entry_skips["gap too large (SKIP band)"] += 1
                        continue
                    if is_gap_chase(direction, gap_pct):
                        entry_skips["gap chase filter"] += 1
                        continue
                    target1, stop_loss = c.get("target1"), c.get("stop_loss")
                    if direction == "BUY" and (
                        (target1 and open_price >= target1) or (stop_loss and open_price <= stop_loss)
                    ):
                        entry_skips["open past target1/stop"] += 1
                        continue
                    if direction == "SELL" and (
                        (target1 and open_price <= target1) or (stop_loss and open_price >= stop_loss)
                    ):
                        entry_skips["open past target1/stop"] += 1
                        continue
                    snap = portfolio.snapshot()
                    risk_ok, _reason = check_capital_portfolio_risk(snap)
                    if not risk_ok:
                        entry_skips["capital/portfolio check"] += 1
                        continue
                    entry_price = fill(open_price, direction, opening=True)
                    allocation = min(
                        snap.get("available_capital", 0.0) * 0.05,
                        initial_capital / max(len(todays_orders), 1),
                    )
                    quantity = int(allocation / entry_price) if entry_price > 0 else 0
                    if quantity <= 0:
                        entry_skips["insufficient capital"] += 1
                        continue
                    if not portfolio.add_position(
                        symbol=sym, quantity=quantity, entry_price=entry_price, direction=direction,
                    ):
                        entry_skips["already open / no capital"] += 1
                        continue
                    meta[sym] = {"entry_date": day, "cycles": 0, "entry_thesis": None}
                    if direction == "BUY":
                        counters["opened_buy"] += 1
                    else:
                        counters["opened_sell"] += 1

            # ---------------- 2. MONITOR open positions with D's full bar
            for sym in list(portfolio.state.open_positions.keys()):
                row, n = bar_on(sym, day)
                if row is None:
                    continue
                pos = portfolio.state.open_positions[sym]
                bundle = DataBundle(
                    symbol=sym, market=data[sym].iloc[:n].copy(),
                    fundamentals=fundamentals.get(sym, {}), news=[],
                )
                scan = self.scanner.evaluate_position(
                    symbol=sym,
                    position={
                        "symbol": sym, "direction": pos.direction,
                        "current_price": pos.current_price,
                        "max_drawdown_percent": pos.max_drawdown_percent,
                    },
                    portfolio=portfolio.snapshot(),
                    broker_status=broker_status, market_state=market_state, bundle=bundle,
                )
                diag = scan.diagnostics
                risk_result = diag.get("_risk_result")
                final_decision = diag.get("_final_decision")
                dataframe = diag.get("_dataframe")
                current_price = diag.get("latest_close")
                if (
                    scan.action == "ERROR" or risk_result is None or final_decision is None
                    or dataframe is None or current_price is None
                    or (isinstance(current_price, float) and math.isnan(current_price))
                ):
                    continue

                portfolio.update_position(symbol=sym, current_price=current_price)
                pos = portfolio.state.open_positions[sym]
                info = meta.setdefault(sym, {"entry_date": day, "cycles": 0, "entry_thesis": None})

                held_conf = (
                    diag.get("buy_decision_confidence") if pos.direction == "BUY"
                    else diag.get("sell_decision_confidence")
                )
                if info["entry_thesis"] is None and held_conf is not None:
                    info["entry_thesis"] = held_conf

                position_input = build_exit_position_input(
                    symbol=sym, pos=pos, current_price=current_price,
                    diagnostics=diag, risk_result=risk_result,
                    holding_days=info["cycles"],
                    entered_today=info["entry_date"] == day,
                    prev_session=None,
                    entry_thesis_confidence=info["entry_thesis"],
                    held_thesis_confidence=held_conf,
                )
                info["cycles"] += 1

                exit_eval = exit_engine.evaluate(
                    decision=dataclasses.replace(final_decision, action=pos.direction),
                    risk=risk_result, dataframe=dataframe, position=position_input,
                )
                raw_exit = (
                    exit_eval.suggested_exit_price
                    if exit_eval.suggested_exit_price is not None else current_price
                )
                exit_price = fill(raw_exit, pos.direction, opening=False)
                reason = exit_eval.diagnostics.get("exit_reason") or (
                    exit_eval.reasons[-1] if exit_eval.reasons else exit_eval.action
                )

                if exit_eval.action == "FULL_EXIT":
                    closed = portfolio.close_position(symbol=sym, exit_price=exit_price)
                    if closed is not None:
                        record_close(closed, exit_price, day, reason)
                    continue

                active_stop = exit_eval.diagnostics.get("active_stop")
                if active_stop is not None:
                    pos.stop_level = float(active_stop)

                if exit_eval.action == "PARTIAL_EXIT":
                    qty = min(max(1, round(pos.quantity * exit_eval.exit_percent / 100.0)), pos.quantity)
                    portfolio.partial_exit(symbol=sym, quantity=qty, exit_price=exit_price)
                    if sym not in portfolio.state.open_positions:
                        record_close(portfolio.state.closed_positions[-1], exit_price, day, reason)
                        continue
                    if exit_eval.diagnostics.get("partial_exit"):
                        portfolio.state.open_positions[sym].partial_taken = True

                portfolio.observe_range(sym, diag.get("latest_high"), diag.get("latest_low"))

            # ---------------- 3. CLOSE: scan -> tomorrow's orders
            bundles = {}
            for sym in symbols:
                row, n = bar_on(sym, day)
                if row is None or n < min_history:
                    continue
                bundles[sym] = DataBundle(
                    symbol=sym, market=data[sym].iloc[:n].copy(),
                    fundamentals=fundamentals.get(sym, {}), news=[],
                )

            scan_results = []
            if bundles:
                scan_results = self.scanner.scan_symbols(
                    symbols=list(bundles.keys()),
                    portfolio=portfolio.snapshot(),
                    broker_status=broker_status,
                    market_state=market_state,
                    bundles=bundles,
                )
                full_scan_results = getattr(self.scanner, "_last_full_scan_results", scan_results)
                for r in full_scan_results:
                    total_scan_attempts += 1
                    if r.action == "ERROR":
                        err_type = r.diagnostics.get("error_type", "UnknownError")
                        error_type_counts[err_type] += 1
                        error_sample_messages.setdefault(err_type, str(r.diagnostics.get("error", ""))[:200])
                    elif r.action == "NO_TRADE":
                        no_trade_count += 1
                        why = (
                            r.diagnostics.get("validation_rejection_reason")
                            or r.diagnostics.get("portfolio_rule_reason") or "score below threshold"
                        )
                        no_trade_reasons[str(why)[:100]] += 1
                    elif r.action in ("BUY", "SELL") and not r.portfolio_allowed:
                        blocked_by_portfolio += 1
                        why = r.diagnostics.get("portfolio_rule_reason") or "unknown"
                        no_trade_reasons[f"signal generated but portfolio blocked: {str(why)[:80]}"] += 1

                candidates = sorted(
                    (r for r in scan_results if r.action in ("BUY", "SELL") and r.portfolio_allowed),
                    key=lambda r: r.ranking, reverse=True,
                )[:max_candidates_per_day]
                pending = [
                    {
                        "symbol": r.symbol,
                        "direction": r.action,
                        "prev_close": r.diagnostics.get("latest_close"),
                        "atr_14": r.diagnostics.get("atr_14"),
                        "stop_loss": r.diagnostics.get("stop_loss"),
                        "target1": r.diagnostics.get("target1"),
                    }
                    for r in candidates
                ]

            # ---------------- mark to market at D's close
            portfolio.mark_to_market()
            portfolio.update_equity_tracking(day_str)
            if not bundles and not portfolio.state.open_positions and not result.equity_curve:
                continue  # still inside the warm-up period: nothing tradeable yet
            result.equity_curve.append(portfolio.state.total_capital + portfolio.state.total_pnl)
            result.dates.append(day)
            result.regimes.append(
                scan_results[0].diagnostics.get("market_regime", "UNKNOWN") if scan_results else "UNKNOWN"
            )

        # Close anything still open at the end so realized P&L covers the
        # whole run (otherwise long-held winners/losers would be invisible).
        last_day = all_dates[-1]
        for sym in list(portfolio.state.open_positions.keys()):
            pos = portfolio.state.open_positions[sym]
            last_price = fill(float(data[sym].iloc[-1]["close"]), pos.direction, opening=False)
            closed = portfolio.close_position(symbol=sym, exit_price=last_price)
            if closed is not None:
                record_close(closed, last_price, last_day, "Open at backtest end (closed at last close)")

        buy_total = sum(1 for t in result.closed_trades if t["direction"] == "BUY")
        sell_total = sum(1 for t in result.closed_trades if t["direction"] == "SELL")
        buy_wins = sum(1 for t in result.closed_trades if t["direction"] == "BUY" and t["realized_pnl"] > 0)
        sell_wins = sum(1 for t in result.closed_trades if t["direction"] == "SELL" and t["realized_pnl"] > 0)

        result.metrics = self._compute_metrics(
            result, initial_capital, counters["wins"], counters["losses"],
            counters["gross_profit"], counters["gross_loss"],
            rr_values, buy_wins, buy_total, sell_wins, sell_total,
        )
        exit_breakdown: dict[str, dict[str, Any]] = {}
        for t in result.closed_trades:
            key = self._exit_category(t["exit_reason"])
            bucket = exit_breakdown.setdefault(key, {"trades": 0, "pnl": 0.0, "wins": 0})
            bucket["trades"] += 1
            bucket["pnl"] = round(bucket["pnl"] + t["realized_pnl"], 2)
            bucket["wins"] += t["realized_pnl"] > 0
        holds = [t["holding_days"] for t in result.closed_trades]
        result.metrics.update({
            "opened_buy_count": counters["opened_buy"],
            "opened_sell_count": counters["opened_sell"],
            "still_open_at_end": len(portfolio.state.open_positions),
            "total_scan_attempts": total_scan_attempts,
            "error_count": sum(error_type_counts.values()),
            "error_breakdown": dict(error_type_counts.most_common(5)),
            "error_samples": error_sample_messages,
            "no_trade_count": no_trade_count,
            "blocked_by_portfolio_count": blocked_by_portfolio,
            "no_trade_reasons": dict(no_trade_reasons.most_common(5)),
            "entry_skips": dict(entry_skips),
            "exit_breakdown": exit_breakdown,
            "avg_holding_days": round(sum(holds) / len(holds), 2) if holds else 0.0,
            "cost_pct_per_side": cost_pct_per_side,
            "engine_version": "2026-10-06-live-flow",
        })
        return result

    @staticmethod
    def _exit_category(reason: str) -> str:
        reason = reason or ""
        if reason.startswith("Risk engine"):
            return "risk_engine"
        if "Stop-loss" in reason:
            return "stop_loss"
        if "Final target" in reason:
            return "final_target"
        if "Partial target" in reason:
            return "partial_target"
        if "Trend reversal" in reason:
            return "trend_reversal"
        if "backtest end" in reason:
            return "open_at_end"
        return "other"

    @staticmethod
    def _compute_walk_forward_windows(
        result: "BacktestResult", initial_capital: float, n_windows: int = 4,
    ) -> list[dict[str, Any]]:
        """Splits the backtest into N sequential, non-overlapping
        windows and computes metrics for EACH window independently —
        the core spirit of walk-forward (rolling, out-of-sample-style
        evaluation) adapted for a rule-based strategy with no fittable
        parameters to literally retrain window-over-window. A strategy
        whose win-rate/CAGR swings wildly between windows is more
        likely overfit to one historical stretch than one with
        consistent numbers across all windows."""
        curve = result.equity_curve
        if len(curve) < n_windows * 10:  # need a reasonable minimum per window
            return []

        window_size = len(curve) // n_windows
        windows = []
        for w in range(n_windows):
            start = w * window_size
            end = start + window_size if w < n_windows - 1 else len(curve)
            window_curve = curve[start:end]
            if len(window_curve) < 2:
                continue

            window_returns = [
                (window_curve[i] / window_curve[i - 1] - 1)
                for i in range(1, len(window_curve)) if window_curve[i - 1] > 0
            ]
            win_days = sum(1 for r in window_returns if r > 0)
            window_start_equity = window_curve[0]
            window_end_equity = window_curve[-1]
            window_return_pct = (
                (window_end_equity / window_start_equity - 1) * 100
                if window_start_equity > 0 else 0.0
            )
            mean_r = sum(window_returns) / len(window_returns) if window_returns else 0.0
            std_r = (
                (sum((r - mean_r) ** 2 for r in window_returns) / len(window_returns)) ** 0.5
                if window_returns else 0.0
            )
            sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0

            windows.append({
                "window": w + 1,
                "days": len(window_curve),
                "return_pct": round(window_return_pct, 2),
                "win_rate": round(win_days / len(window_returns) * 100, 2) if window_returns else None,
                "sharpe": round(sharpe, 2),
                "start_date": str(result.dates[start]) if start < len(result.dates) else None,
                "end_date": str(result.dates[end - 1]) if end - 1 < len(result.dates) else None,
            })
        return windows

    @staticmethod
    def _compute_regime_breakdown(result: "BacktestResult", returns: list[float]) -> dict[str, Any]:
        """Segments the day-over-day equity return series by the
        market regime active on each day (reusing the SAME
        MarketRegimeEngine used live — see the regimes list populated
        during run()), so performance can be judged per-regime instead
        of only as one blended number across the whole period. `returns`
        has one fewer entry than `regimes` (return[i] is between day
        i-1 and day i), so it's paired with regimes[1:]."""
        if not returns or len(result.regimes) < 2:
            return {}

        by_regime: dict[str, list[float]] = {}
        for regime, ret in zip(result.regimes[1:], returns):
            by_regime.setdefault(regime, []).append(ret)

        breakdown = {}
        for regime, rets in by_regime.items():
            wins = sum(1 for r in rets if r > 0)
            breakdown[regime] = {
                "days": len(rets),
                "win_rate": round(wins / len(rets) * 100, 2) if rets else None,
                "avg_daily_return_pct": round(sum(rets) / len(rets) * 100, 4) if rets else None,
            }
        return breakdown

    def _compute_metrics(
        self, result, initial_capital, wins, losses, gross_profit, gross_loss,
        rr_values, buy_wins, buy_total, sell_wins, sell_total,
    ) -> dict[str, Any]:
        curve = result.equity_curve
        total_trades = wins + losses

        win_rate = (wins / total_trades * 100) if total_trades else 0.0
        profit_factor = (
            (gross_profit / gross_loss) if gross_loss > 0
            else (999.99 if gross_profit > 0 else 0.0)  # "no losses" sentinel — inf isn't valid JSON
        )
        expectancy = ((gross_profit - gross_loss) / total_trades) if total_trades else 0.0
        avg_rr = (sum(rr_values) / len(rr_values)) if rr_values else 0.0

        # CAGR
        years = max(len(curve) / 252.0, 1e-9)
        final_equity = curve[-1] if curve else initial_capital
        cagr = ((final_equity / initial_capital) ** (1 / years) - 1) * 100 if initial_capital > 0 else 0.0

        # Max drawdown
        peak = -math.inf
        max_dd = 0.0
        for e in curve:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, (peak - e) / peak)
        max_dd *= 100

        # Daily returns -> Sharpe / Sortino (assume 252 trading days/year, 0% risk-free)
        returns = [
            (curve[i] / curve[i - 1] - 1) for i in range(1, len(curve)) if curve[i - 1] > 0
        ]
        sharpe = 0.0
        sortino = 0.0
        if returns:
            mean_r = sum(returns) / len(returns)
            std_r = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0.0

            downside = [r for r in returns if r < 0]
            down_std = (sum(r ** 2 for r in downside) / len(returns)) ** 0.5 if downside else 0.0
            sortino = (mean_r / down_std * (252 ** 0.5)) if down_std > 0 else 0.0

        buy_trades = [t for t in result.closed_trades if t["direction"] == "BUY"]
        sell_trades = [t for t in result.closed_trades if t["direction"] == "SELL"]
        buy_accuracy = (
            sum(1 for t in buy_trades if t["realized_pnl"] > 0) / len(buy_trades) * 100
            if buy_trades else 0.0
        )
        sell_accuracy = (
            sum(1 for t in sell_trades if t["realized_pnl"] > 0) / len(sell_trades) * 100
            if sell_trades else 0.0
        )

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "cagr": cagr,
            "max_drawdown": max_dd,
            "sharpe": sharpe,
            "sortino": sortino,
            "expectancy": expectancy,
            "avg_rr": avg_rr,
            "buy_trades": len(buy_trades),
            "sell_trades": len(sell_trades),
            "buy_accuracy": buy_accuracy,
            "sell_accuracy": sell_accuracy,
            "regime_breakdown": self._compute_regime_breakdown(result, returns),
            "walk_forward_windows": self._compute_walk_forward_windows(result, initial_capital),
            # "False positives" here = losing trades (the engine said
            # trade, it lost). True false-positive/negative classification
            # against a ground truth needs labeled data this pipeline
            # doesn't have yet.
            "false_positives": losses,
            "false_negatives": None,
            "final_equity": result.equity_curve[-1] if result.equity_curve else initial_capital,
        }
