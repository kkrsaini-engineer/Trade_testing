"""
PHASE 2 — MODULE 4: INSTITUTIONAL BACKTESTING ENGINE

Replays historical OHLCV data day-by-day through the REAL production
pipeline (MarketScanner -> BrokerEngine -> PortfolioEngine), so the
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
from execution.broker import BrokerEngine, OrderRequest
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
            f"False Positives      : {m.get('false_positives', 0)}",
            f"False Negatives      : {m.get('false_negatives', 0)} (see note below)",
            "",
            "NOTE: 'False Negatives' (good setups the engine incorrectly",
            "skipped) can't be measured from trade history alone — it",
            "requires re-scoring every NO_TRADE day against what actually",
            "happened next, which Module 1 (Analysis Engine) does",
            "separately using the full_report.csv history.",
        ]
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
        max_candidates_per_day: int = 10,
    ) -> BacktestResult:
        """
        historical_data: {symbol: dataframe} — each dataframe must have a
        "timestamp" column plus open/high/low/close/volume, ordered oldest
        to newest, ideally spanning multiple market regimes.
        fundamentals: optional {symbol: dict} — static snapshot used for
        every day. STILL A KNOWN LIMITATION, NOT fixed here: historical
        point-in-time fundamentals aren't available from this pipeline,
        so the same fundamentals dict is scored against every simulated
        day regardless of what the company's actual fundamentals were on
        that historical date. Fixing this properly needs a real
        as-reported-on-date fundamentals data source/store — new data
        infrastructure, not a code change, and out of scope for FIX #4
        (which addressed the separately-confirmed FII/DII / macro-news
        / delivery-percentage live-snapshot leakage — see
        MarketScanner.__init__'s disable_live_market_context NOTE).
        """
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

        state = PortfolioState(total_capital=initial_capital, available_capital=initial_capital)
        portfolio = PortfolioEngine(state=state)
        broker = BrokerEngine()
        result = BacktestResult()

        total_steps = min(len(df) for df in historical_data.values())
        if total_steps <= min_history:
            raise ValueError(
                f"Not enough history: shortest series has {total_steps} rows, "
                f"need at least {min_history}."
            )

        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0
        rr_values = []
        buy_wins = buy_total = sell_wins = sell_total = 0
        opened_buy_count = opened_sell_count = 0
        error_type_counts: Counter = Counter()
        error_sample_messages: dict[str, str] = {}
        no_trade_reasons: Counter = Counter()
        no_trade_count = [0]
        blocked_by_portfolio = [0]
        total_scan_attempts = 0

        for step in range(min_history, total_steps):
            broker_status = {
                "status": "ONLINE",
                "mode": "BACKTEST",
                "connected": True,
                "order_allowed": True,
                "available_margin": initial_capital,
            }
            market_state = {
                "max_trade_candidates": max_candidates_per_day,
                "max_watchlist": 50,
                # Without these, ValidationEngine defaults market_open to
                # False and rejects every single simulated day with
                # "Market is closed." — meaning the backtest could never
                # execute a single trade regardless of signal quality.
                "market_open": True,
                "holiday": False,
            }
            portfolio_dict = portfolio.snapshot()

            bundles = {
                sym: DataBundle(
                    symbol=sym,
                    market=historical_data[sym].iloc[: step + 1].copy(),
                    fundamentals=fundamentals.get(sym, {}),
                    news=[],
                )
                for sym in symbols
            }

            scan_results = self.scanner.scan_symbols(
                symbols=symbols,
                portfolio=portfolio_dict,
                broker_status=broker_status,
                market_state=market_state,
                bundles=bundles,
            )

            # scan_symbols() returns ONLY the already-filtered
            # executable_results (BUY/SELL + portfolio_allowed) — by
            # construction it can NEVER contain ERROR or NO_TRADE
            # entries, so diagnosing "why zero trades" from THAT list
            # is structurally blind (confirmed: an earlier version of
            # this diagnostic always showed "0 errors" regardless of
            # what was actually happening). The FULL per-symbol list,
            # including NO_TRADE/ERROR and their rejection reasons, is
            # stashed separately for exactly this purpose.
            full_scan_results = getattr(self.scanner, "_last_full_scan_results", scan_results)
            for r in full_scan_results:
                total_scan_attempts += 1
                if r.action == "ERROR":
                    err_type = r.diagnostics.get("error_type", "UnknownError")
                    error_type_counts[err_type] += 1
                    if err_type not in error_sample_messages:
                        error_sample_messages[err_type] = str(r.diagnostics.get("error", ""))[:200]
                elif r.action == "NO_TRADE":
                    no_trade_count[0] += 1
                    reason = r.diagnostics.get("validation_rejection_reason") or r.diagnostics.get("portfolio_rule_reason") or "score below threshold"
                    no_trade_reasons[str(reason)[:100]] += 1
                elif r.action in ("BUY", "SELL") and not r.portfolio_allowed:
                    blocked_by_portfolio[0] += 1
                    reason = r.diagnostics.get("portfolio_rule_reason") or "unknown"
                    no_trade_reasons[f"signal generated but portfolio blocked: {str(reason)[:80]}"] += 1

            candidates = sorted(
                (r for r in scan_results if r.action in ("BUY", "SELL") and r.portfolio_allowed),
                key=lambda r: r.ranking,
                reverse=True,
            )[:max_candidates_per_day]

            for candidate in candidates:
                price = candidate.diagnostics.get("latest_close")
                # "if not price" alone does NOT catch NaN — NaN is
                # truthy in Python (bool(float('nan')) is True) — so a
                # NaN price would silently pass through into trade
                # execution, producing NaN PnL that cascades into NaN
                # CAGR/Sortino/Expectancy in the final report (confirmed
                # via a real backtest run). Explicit isnan check
                # required, matching the same fix already applied in
                # paper_trading_engine.py.
                if not price or (isinstance(price, float) and math.isnan(price)):
                    continue

                order = OrderRequest(
                    symbol=candidate.symbol,
                    action=candidate.action,
                    quantity=candidate.position_size,
                )
                order_result = broker.place_order(order=order, market_price=price, market_state=market_state)
                if order_result.status not in ("FILLED", "PARTIAL"):
                    continue

                if candidate.action == "BUY":
                    if candidate.symbol in portfolio.state.open_positions:
                        # Existing position is a SHORT (SELL direction) —
                        # a BUY signal here means "cover the short",
                        # mirroring how the SELL branch closes an
                        # existing BUY. Without this, short positions
                        # could only ever be closed by the final forced
                        # close-everything-at-the-end step.
                        existing = portfolio.state.open_positions[candidate.symbol]
                        if existing.direction == "SELL":
                            closed = portfolio.close_position(
                                symbol=candidate.symbol, exit_price=order_result.avg_price
                            )
                            if closed is not None:
                                self._record_closed_trade(result, closed, candidate, wins, losses)
                                if closed.realized_pnl > 0:
                                    wins += 1
                                    gross_profit += closed.realized_pnl
                                else:
                                    losses += 1
                                    gross_loss += abs(closed.realized_pnl)
                                if closed.max_drawdown_percent > 0:
                                    rr_values.append(
                                        closed.max_profit_percent / max(closed.max_drawdown_percent, 1e-9)
                                    )
                                # Count by the CLOSED POSITION's actual
                                # direction (it was a SELL position being
                                # covered here), not by candidate.action
                                # (BUY) — this is what was previously
                                # inverted, causing "BUY Accuracy" to
                                # only ever reflect short-covers instead
                                # of genuine BUY-entry trades.
                                sell_total += 1
                                if closed.realized_pnl > 0:
                                    sell_wins += 1
                    else:
                        portfolio.add_position(
                            symbol=candidate.symbol,
                            quantity=order_result.filled_quantity,
                            entry_price=order_result.avg_price,
                            direction="BUY",
                        )
                        opened_buy_count += 1
                elif candidate.action == "SELL":
                    if candidate.symbol in portfolio.state.open_positions:
                        existing_direction = portfolio.state.open_positions[candidate.symbol].direction
                        closed = portfolio.close_position(symbol=candidate.symbol, exit_price=order_result.avg_price)
                        if closed is not None:
                            self._record_closed_trade(
                                result, closed, candidate, wins, losses,
                            )
                            if closed.realized_pnl > 0:
                                wins += 1
                                gross_profit += closed.realized_pnl
                            else:
                                losses += 1
                                gross_loss += abs(closed.realized_pnl)
                            if closed.max_drawdown_percent > 0:
                                rr_values.append(
                                    closed.max_profit_percent / max(closed.max_drawdown_percent, 1e-9)
                                )
                            # FIXED: previously always counted here as
                            # "sell_total" regardless of what direction
                            # the closed position actually was. Since
                            # the overwhelmingly common lifecycle is
                            # BUY-entry -> SELL-signal-closes-it, this
                            # silently mislabeled nearly every genuine
                            # BUY trade's outcome as a SELL trade,
                            # making "BUY Accuracy" reflect only the
                            # rare short-cover case (confirmed via a
                            # real backtest: 1 BUY trade vs 314 SELL).
                            if existing_direction == "BUY":
                                buy_total += 1
                                if closed.realized_pnl > 0:
                                    buy_wins += 1
                            else:
                                sell_total += 1
                                if closed.realized_pnl > 0:
                                    sell_wins += 1
                    else:
                        portfolio.add_position(
                            symbol=candidate.symbol,
                            quantity=order_result.filled_quantity,
                            entry_price=order_result.avg_price,
                            direction="SELL",
                        )
                        opened_sell_count += 1

            # Mark every open position to today's close, then snapshot equity.
            for sym in list(portfolio.state.open_positions.keys()):
                if sym in historical_data and step < len(historical_data[sym]):
                    price = float(historical_data[sym].iloc[step]["close"])
                    portfolio.update_position(symbol=sym, current_price=price)
            portfolio.mark_to_market()

            equity = portfolio.state.total_capital + portfolio.state.total_pnl
            result.equity_curve.append(equity)
            ts_col = historical_data[symbols[0]].iloc[step].get("timestamp", step)
            result.dates.append(ts_col)
            # Reuses the market_regime the scanner already computed for
            # this day (same regime-detection used live) — no separate
            # computation needed. Falls back to "UNKNOWN" if no scan
            # result was available (e.g. all symbols rejected pre-regime).
            day_regime = scan_results[0].diagnostics.get("market_regime", "UNKNOWN") if scan_results else "UNKNOWN"
            result.regimes.append(day_regime)

        # Close anything still open at the end, so realized P&L covers the
        # whole run (otherwise long-held winners/losers would be invisible
        # to win-rate / profit-factor).
        for sym in list(portfolio.state.open_positions.keys()):
            last_price = float(historical_data[sym].iloc[-1]["close"])
            closed = portfolio.close_position(symbol=sym, exit_price=last_price)
            if closed is not None:
                self._record_closed_trade(result, closed, None, wins, losses)
                if closed.realized_pnl > 0:
                    wins += 1
                    gross_profit += closed.realized_pnl
                else:
                    losses += 1
                    gross_loss += abs(closed.realized_pnl)

        result.metrics = self._compute_metrics(
            result, initial_capital, wins, losses, gross_profit, gross_loss,
            rr_values, buy_wins, buy_total, sell_wins, sell_total,
        )
        result.metrics["opened_buy_count"] = opened_buy_count
        result.metrics["opened_sell_count"] = opened_sell_count
        result.metrics["still_open_at_end"] = len(portfolio.state.open_positions)
        result.metrics["total_scan_attempts"] = total_scan_attempts
        result.metrics["error_count"] = sum(error_type_counts.values())
        result.metrics["error_breakdown"] = dict(error_type_counts.most_common(5))
        result.metrics["error_samples"] = error_sample_messages
        result.metrics["no_trade_count"] = no_trade_count[0]
        result.metrics["blocked_by_portfolio_count"] = blocked_by_portfolio[0]
        result.metrics["no_trade_reasons"] = dict(no_trade_reasons.most_common(5))
        return result

    def _record_closed_trade(self, result, closed, candidate, wins, losses):
        result.closed_trades.append({
            "symbol": closed.symbol,
            "direction": closed.direction,
            "entry_price": closed.entry_price,
            "realized_pnl": closed.realized_pnl,
            "max_profit_percent": closed.max_profit_percent,
            "max_drawdown_percent": closed.max_drawdown_percent,
        })

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
