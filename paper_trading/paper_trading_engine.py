"""
PAPER TRADING ENGINE

Runs the existing production scanner exactly as it does today. Whenever
it produces a valid BUY/SELL signal that passes all production
validations, a VIRTUAL position is opened (no real broker, no real
orders — pure simulation). Every open position is then re-evaluated on
every subsequent cycle using the same scanner intelligence plus a
separate ExitEngine, which decides HOLD or EXIT.

This module does not change, wrap, or reinterpret any BUY/SELL decision
logic — it only reacts to scanner.py's output.

Workflow per cycle (see module docstring in the spec):
    Entry -> Daily Monitoring -> Exit Engine -> Trade Closed -> Trade Diary
"""

from __future__ import annotations

import math
import time
from datetime import date
from typing import Any

from core.logger import get_logger
from core.notifications import notify, severity_from_magnitude
from core.trading_calendar import is_trading_day, now_ist
from execution.scanner import MarketScanner
from market.volatility import fetch_india_vix
from paper_trading.virtual_portfolio import VirtualPortfolio
from risk.exit_engine import ExitEngine
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore

logger = get_logger(__name__)


class PaperTradingEngine:

    def __init__(
        self,
        scanner: MarketScanner | None = None,
        portfolio: VirtualPortfolio | None = None,
        diary: TradeDiary | None = None,
        trade_store: TradeStore | None = None,
        exit_engine: ExitEngine | None = None,
    ):
        self.scanner = scanner or MarketScanner()
        self.portfolio = portfolio or VirtualPortfolio()
        self.diary = diary or TradeDiary()
        self.trade_store = trade_store or TradeStore()
        self.exit_engine = exit_engine or ExitEngine()

    # ==========================================================
    # MAIN DAILY CYCLE
    # ==========================================================

    def run_cycle(self, symbols: list[str], force: bool = False) -> dict[str, Any]:
        today_date = date.today()
        if not force and not is_trading_day(today_date):
            logger.info("Not an NSE trading day (%s) — skipping cycle entirely. "
                        "No new entries, no monitoring (no fresh market data exists anyway).",
                        today_date.isoformat())
            return {
                "date": today_date.isoformat(),
                "status": "SKIPPED_NON_TRADING_DAY",
                "opened_today": [], "closed_today": [], "monitored": [],
                "portfolio_snapshot": self.portfolio.snapshot(),
            }

        today = today_date.isoformat()
        broker_status = {
            "status": "ONLINE", "mode": "PAPER",
            "connected": True, "order_allowed": True, "available_margin": 1e12,
        }
        # BUG FIX: this dict previously had no "vix" key at all, so every
        # RiskManager.evaluate() call made during DAILY MONITORING of an
        # already-open position (below) fell back to risk_manager.py's
        # hardcoded default (vix=20.0) regardless of the real market —
        # meaning the vix>=30/vix>=35 hard-risk exit trigger could never
        # fire for a held position for the entire holding period, only
        # at the original entry-time scan (a separate code path that did
        # fetch real VIX). Fetched once per cycle (not per symbol) since
        # VIX is a single market-wide value, same reasoning as the other
        # once-per-run fetches in execution/scanner.py.
        market_state = {
            "max_trade_candidates": 20, "max_watchlist": 50,
            "market_open": True, "holiday": False,
            "vix": fetch_india_vix(),
        }

        open_symbols = set(self.portfolio.engine.state.open_positions.keys())
        opened_today: list[dict[str, Any]] = []
        closed_today: list[dict[str, Any]] = []
        monitored: list[dict[str, Any]] = []
        monitoring_errors: list[str] = []
        # Severity-based exception handling (not blanket continue):
        # Data Fetch / Evaluation -> recoverable, skip this symbol only.
        # Exit Logic -> recoverable but flagged for manual review (a
        #   broken exit-evaluation on a held position is not fatal today,
        #   but must not be silently treated as routine).
        # Portfolio Update / State Persistence -> NON-recoverable: a
        #   partial state mutation (position removed from open_positions
        #   but diary/trade_store write failed, or the final save()
        #   itself failing) risks silent data corruption if we just keep
        #   going. These abort the remaining cycle instead of continuing.
        cycle_aborted = False
        cycle_abort_reason: str | None = None
        holding_status_rows: list[dict[str, Any]] = []
        closed_details: list[dict[str, Any]] = []

        snap_at_start = self.portfolio.snapshot()
        return_pct = snap_at_start.get("portfolio_return_percent", 0.0)
        notify(
            event_type="paper_trading_started",
            message=(
                f"🚀 Paper Trading Started\n"
                f"Date: {today}\n"
                f"Time: {now_ist().strftime('%H:%M:%S')} IST\n"
                f"Portfolio Value: {snap_at_start.get('portfolio_value', 0):.2f}\n"
                f"Cash Balance: {snap_at_start.get('available_capital', 0):.2f}\n"
                f"Overall Return: {'+' if return_pct >= 0 else ''}{return_pct:.2f}%\n"
                f"Open Positions: {len(open_symbols)}\n"
                f"Maximum Positions: {market_state['max_trade_candidates']}\n"
                f"Status: Evaluating executable trades..."
            ),
            dedup_key=f"paper_trading_started::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
        )

        # --------------------------------------------------
        # 1. MONITOR EXISTING OPEN POSITIONS FIRST
        #    (uses the SAME production scanner intelligence via
        #    evaluate_position() — a MONITORING-ONLY method that never
        #    runs entry-only checks like duplicate_position/max_positions,
        #    since this position already legitimately exists.)
        # --------------------------------------------------
        for symbol in list(open_symbols):
            portfolio_dict = self.portfolio.engine.snapshot()
            pos = self.portfolio.engine.state.open_positions[symbol]
            result = self.scanner.evaluate_position(
                symbol=symbol,
                position={
                    "symbol": symbol,
                    "direction": pos.direction,
                    "current_price": pos.current_price,
                    "max_drawdown_percent": pos.max_drawdown_percent,
                },
                portfolio=portfolio_dict,
                broker_status=broker_status, market_state=market_state,
            )
            if result.action == "ERROR":
                stage = result.diagnostics.get("error_stage", "Data Fetch / Evaluation")
                err_type = result.diagnostics.get("error_type", "UnknownError")
                err_msg = result.diagnostics.get("error", "unknown error")
                logger.warning("Monitoring scan failed for %s [%s]: %s: %s", symbol, stage, err_type, err_msg)
                monitoring_errors.append(f"{symbol} [{stage}] {err_type}: {err_msg}")
                continue

            self.portfolio.register_sector(symbol, result.diagnostics.get("sector"))

            current_price = result.diagnostics.get("latest_close", pos.current_price)

            # ROOT-CAUSE GUARD (see CHANGELOG.md): an occasional bad/
            # incomplete market-data fetch can produce a NaN close price
            # even when the scan otherwise "succeeds" (action != ERROR).
            # Using a NaN price here would silently corrupt this
            # position's P&L today AND, if it reaches close_position(),
            # PERMANENTLY corrupt the whole portfolio's cumulative
            # total_pnl (NaN is contagious through +=) for every future
            # day. Skip this symbol for this cycle instead — same
            # fail-safe pattern as the existing action=="ERROR" skip
            # above — and retry next cycle when fresh data is available.
            if current_price is None or (
                isinstance(current_price, float) and math.isnan(current_price)
            ):
                logger.warning(
                    "Latest close price is NaN/invalid for %s; skipping this "
                    "monitoring cycle (will retry next run).", symbol,
                )
                monitoring_errors.append(f"{symbol} [Data Fetch] InvalidPriceError: NaN/invalid close price")
                continue

            try:
                self.portfolio.engine.update_position(symbol=symbol, current_price=current_price)
            except Exception as exc:
                logger.exception("Portfolio Update stage failed for %s — NON-RECOVERABLE, aborting cycle", symbol)
                err_line = f"{symbol} [Portfolio Update] {type(exc).__name__}: {exc}"
                monitoring_errors.append(err_line)
                notify(
                    event_type="cycle_aborted",
                    message=(
                        f"🔴 Paper Trading Cycle ABORTED — Non-Recoverable Failure\n"
                        f"{err_line}\n\n"
                        f"Portfolio state mutation failed mid-cycle. Remaining symbols "
                        f"this cycle were NOT monitored, to avoid risking a partially "
                        f"corrupted state. Already-completed changes will still be saved."
                    ),
                    severity="🔴 CRITICAL",
                    dedup_key=f"cycle_aborted::{symbol}::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
                )
                cycle_aborted = True
                cycle_abort_reason = err_line
                break
            pos = self.portfolio.engine.state.open_positions[symbol]  # refreshed

            trade_id = self._find_open_trade_id(symbol)
            diary_record = self.diary.get_diary(trade_id) if trade_id else None
            holding_days = len(diary_record["daily_log"]) if diary_record else 0

            dataframe = result.diagnostics.get("_dataframe")
            fundamentals = result.diagnostics.get("_fundamentals") or {}
            news_score = result.diagnostics.get("_news_score")

            if dataframe is None:
                logger.warning("No dataframe available to evaluate exit for %s; holding by default.", symbol)
                monitoring_errors.append(f"{symbol} [Data Fetch] MissingDataError: no market data available")
                continue

            position_input = {
                "symbol": symbol,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "current_price": current_price,
                "stop_loss": result.diagnostics.get("stop_loss"),
                "target1": result.diagnostics.get("target1"),
                "target2": result.diagnostics.get("target2"),
                "day_high": result.diagnostics.get("latest_high"),
                "day_low": result.diagnostics.get("latest_low"),
                "max_drawdown_percent": pos.max_drawdown_percent,
            }
            try:
                exit_eval = self.exit_engine.evaluate(
                    dataframe=dataframe, fundamentals=fundamentals, news_score=news_score,
                    position=position_input, risk_safe=result.diagnostics.get("risk_safe", True),
                    holding_days=holding_days,
                    fii_dii_bias=result.diagnostics.get("fii_dii_bias"),
                )
            except Exception as exc:
                logger.exception("Exit Logic stage failed for %s — REVIEW REQUIRED", symbol)
                err_line = f"{symbol} [Exit Logic] {type(exc).__name__}: {exc}"
                monitoring_errors.append(err_line)
                notify(
                    event_type="exit_logic_review_required",
                    message=(
                        f"🟠 REVIEW REQUIRED — Exit Evaluation Failed\n"
                        f"{err_line}\n\n"
                        f"This position's exit decision could NOT be computed today. "
                        f"It remains open and unmonitored for exit conditions until "
                        f"the next successful cycle — please review manually if this "
                        f"repeats."
                    ),
                    severity="🟠 HIGH",
                    dedup_key=f"exit_logic_review::{symbol}::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
                )
                continue

            if trade_id is None:
                logger.warning("No open diary entry found for %s; skipping diary update.", symbol)
                monitoring_errors.append(f"{symbol} [Portfolio Update] MissingDiaryEntryError: no matching diary entry found")
                continue
            try:
                self.diary.add_daily_log(
                    trade_id=trade_id, date=today, current_price=current_price,
                    current_pnl=pos.unrealized_pnl,
                    current_buy_confidence=result.diagnostics.get("buy_decision_confidence", 0.0),
                    current_sell_confidence=result.diagnostics.get("sell_decision_confidence", 0.0),
                    exit_score=exit_eval.exit_score, recommendation=exit_eval.action,
                    notes=exit_eval.reasons,
                )
            except Exception as exc:
                logger.exception("Portfolio Update stage (diary) failed for %s — NON-RECOVERABLE, aborting cycle", symbol)
                err_line = f"{symbol} [Portfolio Update] {type(exc).__name__}: {exc}"
                monitoring_errors.append(err_line)
                notify(
                    event_type="cycle_aborted",
                    message=(
                        f"🔴 Paper Trading Cycle ABORTED — Non-Recoverable Failure\n"
                        f"{err_line}\n\n"
                        f"Portfolio and diary are now out of sync for this symbol. "
                        f"Remaining symbols this cycle were NOT monitored, to avoid "
                        f"risking further state corruption. Already-completed changes "
                        f"will still be saved."
                    ),
                    severity="🔴 CRITICAL",
                    dedup_key=f"cycle_aborted::{symbol}::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
                )
                cycle_aborted = True
                cycle_abort_reason = err_line
                break
            monitored.append({"symbol": symbol, "action": exit_eval.action, "exit_score": exit_eval.exit_score})

            # Holding Status row (item 3) — built entirely from values
            # already computed above (pos, exit_eval, result.diagnostics).
            stop_loss_v = result.diagnostics.get("stop_loss", 0.0) or 0.0
            target1_v = result.diagnostics.get("target1", 0.0) or 0.0
            target2_v = result.diagnostics.get("target2", 0.0) or 0.0
            # Direction-aware: for BUY, target is ABOVE entry (positive
            # distance = still need to rise) and stop is BELOW (positive
            # distance = still safely above stop). For SELL this is
            # REVERSED — target is below entry, stop is above — so the
            # sign of each comparison must flip, or SELL positions would
            # always show a false REACHED/HIT regardless of actual price
            # movement (this was the confirmed bug).
            if pos.direction == "SELL":
                dist_target1 = round((current_price - target1_v) / current_price * 100, 2) if target1_v and current_price else None
                dist_target2 = round((current_price - target2_v) / current_price * 100, 2) if target2_v and current_price else None
                dist_stop = round((stop_loss_v - current_price) / current_price * 100, 2) if stop_loss_v and current_price else None
            else:
                dist_target1 = round((target1_v - current_price) / current_price * 100, 2) if target1_v and current_price else None
                dist_target2 = round((target2_v - current_price) / current_price * 100, 2) if target2_v and current_price else None
                dist_stop = round((current_price - stop_loss_v) / current_price * 100, 2) if stop_loss_v and current_price else None

            if exit_eval.action == "EXIT":
                status_label = "EXIT CANDIDATE"
            elif dist_target2 is not None and dist_target2 <= 0:
                status_label = "TARGET 2 REACHED"
            elif dist_target1 is not None and dist_target1 <= 0:
                status_label = "TARGET 1 REACHED"
            elif dist_stop is not None and dist_stop <= 2.0:
                status_label = "STOP LOSS WARNING"
            else:
                status_label = "HOLD"

            if pos.direction == "SELL":
                target1_pct = round((pos.entry_price - target1_v) / pos.entry_price * 100, 2) if target1_v and pos.entry_price else None
                target2_pct = round((pos.entry_price - target2_v) / pos.entry_price * 100, 2) if target2_v and pos.entry_price else None
            else:
                target1_pct = round((target1_v - pos.entry_price) / pos.entry_price * 100, 2) if target1_v and pos.entry_price else None
                target2_pct = round((target2_v - pos.entry_price) / pos.entry_price * 100, 2) if target2_v and pos.entry_price else None

            holding_status_rows.append({
                "trade_id": trade_id, "symbol": symbol, "direction": pos.direction,
                "holding_days": holding_days,
                "entry_date": diary_record.get("entry_date") if diary_record else "N/A",
                "entry_price": pos.entry_price, "current_price": current_price,
                "pnl_pct": pos.unrealized_pnl_percent, "pnl_rupees": pos.unrealized_pnl,
                "highest_pnl": pos.max_profit_percent, "lowest_pnl": -pos.max_drawdown_percent,
                "dist_target1": dist_target1, "dist_target2": dist_target2, "dist_stop": dist_stop,
                "target1_pct": target1_pct, "target2_pct": target2_pct,
                "probability": result.probability,
                "buy_confidence": result.diagnostics.get("buy_decision_confidence", 0.0),
                "sell_confidence": result.diagnostics.get("sell_decision_confidence", 0.0),
                "status": status_label,
            })

            # "Existing Position Updated" — only notify on a MEANINGFUL
            # change vs the last logged values (prevents spamming every
            # trivial fluctuation), and dedup by (symbol, today's date)
            # so this fires at most once per position per day.
            prev_log = diary_record["daily_log"][-1] if diary_record and diary_record["daily_log"] else None
            new_buy_conf = result.diagnostics.get("buy_decision_confidence", 0.0)
            new_sell_conf = result.diagnostics.get("sell_decision_confidence", 0.0)
            CHANGE_THRESHOLD = 10.0
            meaningfully_changed = prev_log is None or (
                abs(new_buy_conf - prev_log.get("current_buy_confidence", 0.0)) >= CHANGE_THRESHOLD
                or abs(new_sell_conf - prev_log.get("current_sell_confidence", 0.0)) >= CHANGE_THRESHOLD
                or abs(exit_eval.exit_score - prev_log.get("current_exit_score", 0.0)) >= CHANGE_THRESHOLD
                or exit_eval.action != prev_log.get("recommendation")
            )
            if meaningfully_changed:
                position_status = "EXIT" if exit_eval.action == "EXIT" else (
                    "REVIEW" if exit_eval.exit_score >= exit_eval.threshold * 0.7 else "HOLD"
                )
                # NOTE: no longer sent as an individual Telegram message
                # (was causing 1 message per position per day — see the
                # consolidated "Paper Trading Summary" at the end of this
                # cycle instead, which reflects every meaningful change).

            if exit_eval.action == "EXIT":
                closed = None
                # A stop-loss/target breach detected via day_low/day_high
                # means a real order would have executed AT that touch
                # price, not whatever price is current by the time this
                # periodic check runs (which can be significantly
                # different if price has since moved further) — use
                # that price when it's available, so P&L reflects what
                # actually would have happened.
                actual_exit_price = (
                    exit_eval.suggested_exit_price
                    if exit_eval.suggested_exit_price is not None
                    else current_price
                )
                try:
                    closed = self.portfolio.engine.close_position(symbol=symbol, exit_price=actual_exit_price)
                    if closed is not None:
                        self.trade_store.save_trade({
                            "symbol": closed.symbol, "direction": closed.direction, "action": "CLOSE",
                            "quantity": closed.quantity, "entry_price": closed.entry_price,
                            "exit_price": actual_exit_price, "status": "CLOSED",
                            "realized_pnl": closed.realized_pnl,
                            "realized_pnl_percent": closed.realized_pnl_percent,
                            "max_profit_percent": closed.max_profit_percent,
                            "max_drawdown_percent": closed.max_drawdown_percent,
                            "regime": result.diagnostics.get("market_regime", ""),
                            "confidence": result.confidence,
                            "reasons": "; ".join(exit_eval.reasons),
                        })
                        target1_status = (
                            "REACHED" if dist_target1 is not None and dist_target1 <= 0
                            else "NOT REACHED" if dist_target1 is not None else "N/A"
                        )
                        target2_status = (
                            "REACHED" if dist_target2 is not None and dist_target2 <= 0
                            else "NOT REACHED" if dist_target2 is not None else "N/A"
                        )
                        stop_loss_status = (
                            "HIT" if dist_stop is not None and dist_stop <= 0
                            else "NOT HIT" if dist_stop is not None else "N/A"
                        )
                        # dist_stop only compares the LATEST current_price
                        # against the stop level — it can show "NOT HIT"
                        # even when the exit was genuinely triggered by an
                        # INTRADAY dip/spike (day_low/day_high) that
                        # recovered by the time this runs, contradicting
                        # the Exit Reason shown right above it (the exact
                        # bug reported). exit_eval.hard_risk_reason is the
                        # actual source of truth for why the exit
                        # happened, so it overrides here when it
                        # specifically says the stop was breached.
                        if exit_eval.hard_risk_reason and "stop-loss breached" in exit_eval.hard_risk_reason.lower():
                            stop_loss_status = "HIT"
                        self.diary.close_trade(
                            trade_id=trade_id, exit_date=today, exit_price=current_price,
                            exit_reason=exit_eval.hard_risk_reason or "; ".join(exit_eval.reasons[-1:]),
                            final_pnl=closed.realized_pnl,
                            final_pnl_percent=closed.realized_pnl_percent,
                            max_profit_percent=closed.max_profit_percent,
                            max_drawdown_percent=closed.max_drawdown_percent,
                            exit_score=exit_eval.exit_score,
                            target1_status=target1_status,
                            target2_status=target2_status,
                            stop_loss_status=stop_loss_status,
                        )
                        closed_today.append({"symbol": symbol, "pnl": closed.realized_pnl})
                        open_symbols.discard(symbol)
                except Exception as exc:
                    logger.exception("Portfolio Update stage (close) failed for %s — NON-RECOVERABLE, aborting cycle", symbol)
                    err_line = f"{symbol} [Portfolio Update] {type(exc).__name__}: {exc}"
                    monitoring_errors.append(err_line)
                    notify(
                        event_type="cycle_aborted",
                        message=(
                            f"🔴 Paper Trading Cycle ABORTED — Non-Recoverable Failure\n"
                            f"{err_line}\n\n"
                            f"A position close operation failed partway through — the "
                            f"portfolio/diary/trade journal may now be inconsistent for "
                            f"this symbol. Remaining symbols this cycle were NOT "
                            f"monitored, to avoid risking further corruption. "
                            f"Already-completed changes will still be saved."
                        ),
                        severity="🔴 CRITICAL",
                        dedup_key=f"cycle_aborted::{symbol}::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
                    )
                    cycle_aborted = True
                    cycle_abort_reason = err_line
                    break

                if closed is not None:
                    closed_at = time.time()
                    pnl_pct = closed.realized_pnl_percent
                    trigger = self._classify_exit_trigger(exit_eval)
                    closed_details.append({
                        "symbol": symbol, "direction": closed.direction,
                        "entry_price": closed.entry_price, "exit_price": actual_exit_price,
                        "pnl_pct": pnl_pct, "pnl_rupees": closed.realized_pnl,
                        "trigger": trigger, "holding_days": holding_days,
                        "trade_id": trade_id, "exit_score": exit_eval.exit_score,
                        "exit_threshold": exit_eval.threshold,
                        "exit_reasons": [
                            r for r in (exit_eval.reasons or [])
                            if not r.lower().startswith("exit score")
                        ][:3],
                        "risk_factor_detail": self._top_risk_factors(result.diagnostics),
                        "target1_status": target1_status, "target2_status": target2_status,
                        "stop_loss_status": stop_loss_status,
                    })

        total_positions = len(open_symbols) + len(closed_today)
        if total_positions > 0:
            successful_count = len(monitored)
            failed_count = len(monitoring_errors)

            # Group failures by exception type for the breakdown.
            groups: dict[str, dict[str, Any]] = {}
            for err in monitoring_errors:
                if "[" in err and "]" in err:
                    symbol_part, rest = err.split("[", 1)
                    stage_part, detail_part = rest.split("]", 1)
                    detail_part = detail_part.strip()
                    if ":" in detail_part:
                        exc_type, exc_msg = detail_part.split(":", 1)
                    else:
                        exc_type, exc_msg = "UnknownError", detail_part
                    exc_type = exc_type.strip()
                    exc_msg = exc_msg.strip()
                else:
                    symbol_part, exc_type, exc_msg = err, "UnknownError", err

                key = f"{exc_type}::{stage_part.strip() if '[' in err else ''}"
                g = groups.setdefault(key, {"type": exc_type, "stage": stage_part.strip() if "[" in err else "N/A", "symbols": [], "reason": exc_msg})
                g["symbols"].append(symbol_part.strip().split(".")[0])

            holding_count = successful_count - len(closed_details)
            summary_lines = [
                "📊 Paper Trading Summary",
                f"{total_positions} Positions Evaluated",
                f"{len(closed_details)} Closed",
                f"{max(holding_count, 0)} Holding",
            ]
            if failed_count:
                summary_lines.append(f"{failed_count} Failed")

            if closed_details:
                short_trigger = {
                    "Stop Loss Hit": "Stop Loss", "Target Achieved": "Target Hit",
                    "Risk Management Exit": "Risk Exit",
                    "Time-Based Exit": "Time Exit",
                    "Momentum Weakened": "Momentum Exit",
                    "Fundamentals Weakened": "Fundamentals Exit",
                    "Negative News": "News Exit",
                    "Trend Reversal": "Trend Exit",
                }
                summary_lines.append("-" * 16)
                summary_lines.append("Closed")
                for c in closed_details:
                    summary_lines.append("-" * 14)
                    summary_lines.extend(self._render_closed_trade_block(c, short_trigger))

            if groups:
                summary_lines.append("")
                summary_lines.append("Failure Breakdown")
                for g in groups.values():
                    summary_lines.append(f"{g['type']} ({g['stage']})")
                    summary_lines.append(f"{len(g['symbols'])}")
                    summary_lines.append("Affected Symbols")
                    summary_lines.extend(g["symbols"])
                    summary_lines.append("Reason")
                    summary_lines.append(g["reason"])
                    summary_lines.append("")

            notify(
                event_type="monitoring_summary",
                message="\n".join(summary_lines).strip(),
                severity="🔴 CRITICAL" if failed_count == total_positions and total_positions > 0 else "🟢 LOW",
                dedup_key=f"monitoring_summary::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
            )
        else:
            recent_closed = self.diary.get_closed_trades()
            # Most-recently-closed first, capped to a reasonable count
            # so this doesn't grow unbounded over the life of the portfolio.
            recent_closed = sorted(
                recent_closed, key=lambda r: r.get("updated_at", 0), reverse=True
            )[:20]

            lines = [
                "📊 Paper Trading Summary",
                "0 Positions Evaluated",
                "",
                "No open positions to monitor.",
            ]
            if recent_closed:
                short_trigger = {
                    "Stop Loss Hit": "Stop Loss", "Target Achieved": "Target Hit", "Risk Management Exit": "Risk Exit",
                    "Time-Based Exit": "Time Exit", "Momentum Weakened": "Momentum Exit",
                    "Fundamentals Weakened": "Fundamentals Exit", "Negative News": "News Exit",
                    "Trend Reversal": "Trend Exit",
                }
                lines.append("")
                lines.append(f"Last {len(recent_closed)} Exit(s)")
                for rec in recent_closed:
                    lines.append("-" * 14)
                    c = {
                        "symbol": rec.get("symbol", "N/A"), "direction": rec.get("direction", ""),
                        "entry_price": rec.get("entry_price"), "exit_price": rec.get("exit_price"),
                        "pnl_pct": rec.get("final_pnl_percent", 0.0), "pnl_rupees": rec.get("final_pnl", 0.0),
                        "trigger": rec.get("exit_reason", "N/A"), "holding_days": rec.get("holding_days", "N/A"),
                        "exit_score": rec.get("exit_score"), "target1_status": rec.get("target1_status"),
                        "target2_status": rec.get("target2_status"), "stop_loss_status": rec.get("stop_loss_status"),
                    }
                    lines.extend(self._render_closed_trade_block(c, short_trigger))

            notify(
                event_type="monitoring_summary",
                message="\n".join(lines).strip(),
                dedup_key=f"monitoring_summary_empty::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
            )

        self.portfolio.engine.mark_to_market()

        if holding_status_rows:
            # Build each position's block SEPARATELY (not one giant
            # joined string) so they can be grouped into Telegram-safe
            # chunks below — CONFIRMED root cause of this notification
            # silently not arriving: Telegram has a hard 4096-character
            # limit per message, and output/telegram_alert.py does not
            # check the API response, so an oversized single message
            # (e.g. ~9980 chars for 22 positions) gets silently
            # rejected while the code logs "sent" regardless.
            position_blocks = []
            for r in holding_status_rows:
                block = [
                    f"{r['symbol']} ({r['direction']}) — {r['status']}",
                    f"Trade ID: {r['trade_id']}",
                    f"Holding Days: {r['holding_days']} | Entry Date: {r['entry_date']}",
                    f"Entry: {r['entry_price']} | Current: {r['current_price']}",
                    f"PnL: {r['pnl_pct']:.2f}% (₹{r['pnl_rupees']:.2f})",
                    f"Highest: {r['highest_pnl']:.2f}% | Lowest: {r['lowest_pnl']:.2f}%",
                ]
                t1_label = f" ({r['target1_pct']}%)" if r.get("target1_pct") is not None else ""
                t2_label = f" ({r['target2_pct']}%)" if r.get("target2_pct") is not None else ""
                if r["dist_target1"] is not None:
                    if r["dist_target1"] > 0:
                        block.append(f"Target 1{t1_label} progress:- {-r['dist_target1']:.2f}% remaining")
                    else:
                        block.append(f"Target 1{t1_label}: REACHED ({abs(r['dist_target1']):.2f}% Beyond)")
                if r["dist_target2"] is not None:
                    if r["dist_target2"] > 0:
                        block.append(f"Target 2{t2_label} progress:- {-r['dist_target2']:.2f}% remaining")
                    else:
                        block.append(f"Target 2{t2_label}: REACHED ({abs(r['dist_target2']):.2f}% Beyond)")
                if r["dist_stop"] is not None:
                    block.append(f"Stop Loss Distance: {r['dist_stop']:.2f}%")
                block.append(f"Probability: {r['probability']:.1f}%")
                block.append(f"BUY Confidence: {r['buy_confidence']:.1f}% | SELL Confidence: {r['sell_confidence']:.1f}%")
                position_blocks.append("\n".join(block))

            # Portfolio-level aggregate — same computation as before,
            # appended to the LAST part only.
            total_unrealized_rupees = sum(r["pnl_rupees"] for r in holding_status_rows)
            winning = sum(1 for r in holding_status_rows if r["pnl_rupees"] > 0)
            losing = sum(1 for r in holding_status_rows if r["pnl_rupees"] <= 0)
            sign = "+" if total_unrealized_rupees >= 0 else ""
            summary_block = "\n".join([
                "━━━━━━━━━━━━━━━━━━",
                "📊 Portfolio Summary (all open positions)",
                f"Positions Held: {len(holding_status_rows)} ({winning} winning, {losing} losing)",
                f"Total Unrealized P&L: {sign}₹{total_unrealized_rupees:.2f}",
                "(This is separate from Realized PnL in the Daily Portfolio Summary — "
                "this reflects only currently-open positions, mark-to-market at today's prices.)",
            ])

            # Group into chunks of 7 positions each (safe margin under
            # Telegram's 4096-char limit for typical block sizes), with
            # a defense-in-depth check: if a chunk of 7 still happens to
            # exceed ~4000 chars (unusually long values), it's split
            # further rather than risk another silent rejection.
            POSITIONS_PER_CHUNK = 7
            SAFE_CHAR_LIMIT = 4000
            chunks: list[list[str]] = []
            current_chunk: list[str] = []
            current_len = 0
            for block in position_blocks:
                block_len = len(block) + 2
                if current_chunk and (
                    len(current_chunk) >= POSITIONS_PER_CHUNK
                    or current_len + block_len > SAFE_CHAR_LIMIT
                ):
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_len = 0
                current_chunk.append(block)
                current_len += block_len
            if current_chunk:
                chunks.append(current_chunk)

            total_parts = len(chunks)
            for part_num, chunk in enumerate(chunks, start=1):
                part_lines = [f"📋 Holding Status (Part {part_num}/{total_parts})", ""]
                part_lines.append("\n\n".join(chunk))
                if part_num == total_parts:
                    part_lines.append("")
                    part_lines.append(summary_block)
                notify(
                    event_type="holding_status",
                    message="\n".join(part_lines).strip(),
                    dedup_key=f"holding_status::{today}::part{part_num}::{now_ist().strftime('%H:%M:%S.%f')}",
                )

        # --------------------------------------------------
        # 2. NEW-ENTRY SCANNING — REMOVED (now Morning Executor's job)
        #    Previously this section independently scanned and opened
        #    new positions right here, at night, using the previous
        #    close as "entry price" — the exact stale-price problem
        #    Morning Executor was built to fix (executing at the
        #    genuine next-morning market-open price instead). Leaving
        #    both active meant Paper Trading would open a position
        #    FIRST (with the stale price), and Morning Executor's
        #    later, correct attempt would be silently rejected as a
        #    duplicate-symbol by add_position() — defeating the whole
        #    point. Paper Trading is now monitoring-only; new entries
        #    happen exclusively via scripts/morning_executor.py.
        # --------------------------------------------------

        try:
            self.portfolio.save()
        except Exception as exc:
            logger.exception("State Persistence stage failed — NON-RECOVERABLE, this cycle's changes may be lost")
            notify(
                event_type="cycle_aborted",
                message=(
                    f"🔴 STATE PERSISTENCE FAILED — Non-Recoverable\n"
                    f"Stage: State Persistence\n"
                    f"Exception Type: {type(exc).__name__}\n"
                    f"Exception Message: {exc}\n\n"
                    f"Today's Opened/Closed/Monitored changes may NOT have been "
                    f"saved to disk. Do not trust this cycle's summary until "
                    f"verified manually."
                ),
                severity="🔴 CRITICAL",
                dedup_key=f"state_persistence_failed::{today}::{now_ist().strftime('%H:%M:%S.%f')}",
            )
            raise  # do not swallow — let the workflow run itself fail loudly

        summary = {
            "date": today,
            "opened_today": opened_today,
            "closed_today": closed_today,
            "monitored": monitored,
            "monitoring_errors": monitoring_errors,
            "cycle_aborted": cycle_aborted,
            "cycle_abort_reason": cycle_abort_reason,
            "open_positions_at_start": len(open_symbols) + len(closed_today),
            "opening_balance": snap_at_start.get("available_capital", 0.0),
            "portfolio_snapshot": self.portfolio.snapshot(),
        }
        logger.info(
            "Paper trading cycle complete: %d opened, %d closed, %d monitored.",
            len(opened_today), len(closed_today), len(monitored),
        )
        return summary

    def _find_open_trade_id(self, symbol: str) -> str | None:
        prefix = f"paper_{symbol.replace('.', '_')}_"
        for tid in self.diary.list_open_trade_ids():
            if tid.startswith(prefix):
                return tid
        return None

    @staticmethod
    def _new_trade_id(symbol: str) -> str:
        # Unique per position lifetime (symbol + open timestamp) — if the
        # same symbol trades again later after a prior position closed,
        # this avoids overwriting the earlier CLOSED diary record.
        return f"paper_{symbol.replace('.', '_')}_{int(time.time() * 1000)}"

    # ==========================================================
    # TELEGRAM REPORT FORMATTING (presentation only — every value
    # used below was already computed elsewhere; nothing new here.
    # Market Intelligence stays fully decoupled — see
    # market_intelligence/market_intelligence_engine.py, which runs on
    # its own separate schedule and sends its own summary.)
    # ==========================================================

    @staticmethod
    def _fmt_ts(epoch: float | None) -> str:
        if not epoch:
            return "N/A"
        from datetime import datetime, timezone
        from core.trading_calendar import IST_OFFSET
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc) + IST_OFFSET
        return dt.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _extract_reason_line(reasons_list: list[str], prefix: str) -> str | None:
        """Find an already-existing reason line by its prefix (e.g.
        "BUY Strength", "SELL engine validation") — these are produced
        by decision_engine.py's own reasons list, not recomputed here."""
        for r in reasons_list:
            if r.strip().lower().startswith(prefix.lower()):
                return r.strip()
        return None

    def _format_buy_report(
        self, candidate: Any, price: float, reasons_list: list[str], trade_id: str,
    ) -> str:
        d = candidate.diagnostics
        action = candidate.action
        other_side = "SELL" if action == "BUY" else "BUY"

        strength_prefixes = ("buy strength", "sell strength", "decision quality",
                             "buy engine validation", "sell engine validation")
        filtered_reasons = [
            r for r in reasons_list
            if not r.strip().lower().startswith(strength_prefixes)
        ]
        top_reasons = "\n".join(f"• {r}" for r in filtered_reasons[:5]) or "• N/A"

        buy_strength = self._extract_reason_line(reasons_list, "BUY Strength")
        sell_strength = self._extract_reason_line(reasons_list, "SELL Strength")
        decision_quality = self._extract_reason_line(reasons_list, "Decision Quality")
        other_side_rejection = self._extract_reason_line(
            reasons_list, f"{other_side} engine validation"
        )

        stop_loss = d.get("stop_loss", 0.0) or 0.0
        target1 = d.get("target1", 0.0) or 0.0
        target2 = d.get("target2", 0.0) or 0.0
        expected_hold_days = d.get("expected_hold_days", 0) or 0

        stop_pct = round(abs(price - stop_loss) / price * 100, 2) if stop_loss and price else 0.0
        stop_dist = abs(price - stop_loss)

        def target_block(label: str, target_price: float) -> list[str]:
            if not target_price or not price:
                return [f"{label}: N/A"]
            pct = round((target_price - price) / price * 100, 2)
            rr = round(abs(target_price - price) / stop_dist, 2) if stop_dist else 0.0
            sign = "+" if pct >= 0 else ""
            return [f"{label}: {sign}{pct:.1f}%  (Risk:Reward 1:{rr:.2f})"]

        # Decision Margin — overall score vs the qualifying threshold that
        # actually decided this trade (already computed by the strategy).
        # Labeled "(Screening)" because this is buy_decision.overall_score
        # / sell_decision.overall_score (Tier2*0.45 + Tier3*0.55 from
        # buy_strategy.py/sell_strategy.py) — a DIFFERENT number from the
        # full 8-component weighted-blend score (buy_score.overall /
        # sell_score.overall from buy_scoring.py/sell_scoring.py) that
        # feeds into BUY/SELL Strength further below. Both are legitimate
        # but answer different questions; showing them both as unqualified
        # "{action} Score" was confirmed confusing (numbers don't match
        # because they're genuinely different calculations).
        score_key, threshold_key = (
            ("buy_overall_score", "buy_qualify_threshold") if action == "BUY"
            else ("sell_overall_score", "sell_qualify_threshold")
        )
        score = d.get(score_key)
        threshold = d.get(threshold_key)
        margin_lines = []
        if score is not None and threshold is not None:
            margin = round(score - threshold, 2)
            margin_lines = [
                "",
                "Decision Margin",
                f"{action} Score (Screening): {score:.1f}",
                f"Threshold: {threshold:.1f}",
                f"Margin: {'+' if margin >= 0 else ''}{margin:.1f}",
            ]

        opened_ts = self._fmt_ts(time.time())

        lines = [
            "🟢 New Virtual Trade Opened",
            f"Trade ID: {trade_id}",
            f"Symbol: {candidate.symbol}",
            f"Signal: {action}",
            f"Entry Price: {price}",
            f"Quantity: {candidate.position_size}",
            f"Probability: {candidate.probability:.1f}%",
            f"Confidence: {candidate.confidence:.1f}%",
            "",
            f"📅 Expected Holding: ~{expected_hold_days} days",
            "🎯 " + target_block("Target 1 (Partial)", target1)[0],
            "🎯 " + target_block("Target 2 (Final)", target2)[0],
            f"🛑 Expected Stop Loss: -{stop_pct:.1f}%",
        ]
        lines += margin_lines
        if buy_strength or sell_strength or decision_quality:
            lines.append("")
            lines.append("Conflict Resolution (full-blend score + probability, 50/50 — different basis than Decision Margin above)")
            if buy_strength:
                lines.append(buy_strength)
            if sell_strength:
                lines.append(sell_strength)
            if decision_quality:
                lines.append(decision_quality)
        lines.append("")
        lines.append("Top Reasons")
        lines.append(top_reasons)
        if other_side_rejection:
            lines.append("")
            lines.append(f"Why {other_side} was rejected:")
            lines.append(f"• {other_side_rejection}")
        lines += [
            "",
            "Lifecycle",
            f"Opened: {opened_ts}",
            "Holding: 0 Days",
            "Status: ACTIVE",
        ]

        return "\n".join(lines)

    def _format_position_update(
        self, symbol: str, pos: Any, holding_days: int, buy_conf: float, sell_conf: float,
        exit_eval: Any, position_status: str, result_diagnostics: dict,
        trade_id: str, created_at: float | None,
    ) -> str:
        current_price = pos.current_price
        stop_loss = result_diagnostics.get("stop_loss", 0.0) or 0.0
        target1 = result_diagnostics.get("target1", 0.0) or 0.0
        target2 = result_diagnostics.get("target2", 0.0) or 0.0
        if pos.direction == "SELL":
            dist_to_target1 = (
                round((current_price - target1) / current_price * 100, 2)
                if target1 and current_price else None
            )
            dist_to_target2 = (
                round((current_price - target2) / current_price * 100, 2)
                if target2 and current_price else None
            )
            dist_to_stop = (
                round((stop_loss - current_price) / current_price * 100, 2)
                if stop_loss and current_price else None
            )
        else:
            dist_to_target1 = (
                round((target1 - current_price) / current_price * 100, 2)
                if target1 and current_price else None
            )
            dist_to_target2 = (
                round((target2 - current_price) / current_price * 100, 2)
                if target2 and current_price else None
            )
            dist_to_stop = (
                round((current_price - stop_loss) / current_price * 100, 2)
                if stop_loss and current_price else None
            )
        current_pnl_rupees = pos.unrealized_pnl

        lines = [
            f"🔄 Position Update: {symbol} ({pos.direction})",
            f"Trade ID: {trade_id}",
            f"Holding Days: {holding_days}",
            f"Current Price: {current_price}",
            f"Entry Price: {pos.entry_price}",
            f"Current PnL: {pos.unrealized_pnl_percent:.2f}% (₹{current_pnl_rupees:.2f})",
            f"Highest PnL achieved: {pos.max_profit_percent:.2f}%",
            f"Lowest PnL achieved: -{pos.max_drawdown_percent:.2f}%",
        ]
        if dist_to_target1 is not None:
            lines.append(f"Remaining Distance to Target 1: {dist_to_target1:.2f}%")
        if dist_to_target2 is not None:
            lines.append(f"Remaining Distance to Target 2: {dist_to_target2:.2f}%")
        if dist_to_stop is not None:
            lines.append(f"Remaining Distance to Stop Loss: {dist_to_stop:.2f}%")
        lines += [
            f"BUY Confidence: {buy_conf:.1f}%",
            f"SELL Confidence: {sell_conf:.1f}%",
            f"Exit Score: {exit_eval.exit_score:.1f}/100",
            f"Recommendation: {position_status}",
            "",
            "Lifecycle",
            f"Opened: {self._fmt_ts(created_at)}",
            f"Holding: {holding_days} Days",
            "Status: ACTIVE",
        ]
        return "\n".join(lines)

    @staticmethod
    def _top_risk_factors(diagnostics: dict) -> list[str]:
        """Shows WHICH risk dimension actually drove total_risk, sorted
        by genuine CONTRIBUTION (value x weight from RiskManager's own
        weights) rather than raw value — a high raw score on a
        low-weight dimension (e.g. Sector, weight 0.05) can contribute
        LESS than a moderate score on a high-weight one (e.g. Market/
        Portfolio, weight 0.12 each). Confirmed via risk_manager.py:
        total_risk = sum(value * weight), NOT max(value). Also flags
        the 6 instant-unsafe overrides (circuit breaker, emergency
        stop, daily loss lock, VIX spike, event day, news shock) which
        bypass the weighted-sum entirely and force unsafe=True on
        their own — these were previously invisible in this notification."""
        OVERRIDE_FLAGS = {
            "circuit_override": "🔴 Circuit breaker active (INSTANT unsafe, bypasses all other scoring)",
            "emergency_stop": "🔴 Portfolio emergency stop enabled (INSTANT unsafe)",
            "daily_loss_lock": "🔴 Daily loss limit reached (INSTANT unsafe)",
            "vix_override": "🟠 Extreme VIX spike (+10 to total_risk)",
            "event_override": "🟠 High-impact market event today (+15 to total_risk)",
            "news_override": "🟠 Extreme news shock (+15 to total_risk)",
        }
        lines = [msg for key, msg in OVERRIDE_FLAGS.items() if diagnostics.get(key)]

        components = diagnostics.get("risk_components")
        weights = diagnostics.get("risk_weights")
        labels = {
            "atr": "ATR (volatility)", "gap": "Overnight Gap", "overnight": "Overnight Hold",
            "news": "News", "liquidity": "Liquidity", "volatility": "Volatility",
            "market": "Market", "portfolio": "Portfolio", "sector": "Sector",
            "correlation": "Correlation", "capital": "Capital",
        }
        if components and weights:
            scored = []
            for key, label in labels.items():
                val = components.get(key)
                w = weights.get(key)
                if val is not None and w is not None:
                    scored.append((label, float(val), float(w), float(val) * float(w)))
            scored.sort(key=lambda t: t[3], reverse=True)
            total = sum(t[3] for t in scored) or 1.0
            for label, val, w, contribution in scored:
                pct_of_total = round(contribution / total * 100, 0)
                lines.append(f"{label}: {val:.0f}/100 (weight {w:.0%}) -> contributes {contribution:.1f} = {pct_of_total:.0f}% of total_risk")
            return lines

        # Fallback: nested dicts not present in this diagnostics dict —
        # show raw values only, clearly labeled as such so it isn't
        # mistaken for contribution-ranking.
        fallback_labels = {
            "volatility_risk": "Volatility", "liquidity_risk": "Liquidity",
            "news_risk": "News", "market_risk": "Market", "portfolio_risk": "Portfolio",
            "sector_risk": "Sector", "atr_risk": "ATR (volatility)", "gap_risk": "Overnight Gap",
            "overnight_risk": "Overnight Hold",
        }
        scored = []
        for key, label in fallback_labels.items():
            val = diagnostics.get(key)
            if val is not None:
                try:
                    scored.append((label, float(val)))
                except (TypeError, ValueError):
                    continue
        scored.sort(key=lambda kv: kv[1], reverse=True)
        if scored:
            lines.append("(weights unavailable — raw values only, NOT contribution-ranked)")
            lines += [f"{label}: {val:.0f}/100" for label, val in scored]
        return lines

    @staticmethod
    def _render_closed_trade_block(c: dict, short_trigger: dict) -> list[str]:
        """Full detail lines for one closed trade — Symbol/Direction/
        Entry/Exit/Return%/PnL/Holding Days/Exit Score/Target1/Target2/
        StopLoss/Reason. Shared by both the "closed this cycle" section
        and the "last known exits" (0 open positions) recap."""
        lines = []
        tag = f"{c['symbol'].split('.')[0]}" + (" SELL" if c["direction"] == "SELL" else "")
        lines.append(tag)
        if c.get("entry_price") is not None:
            lines.append(f"Entry: {c['entry_price']} -> Exit: {c.get('exit_price', 'N/A')}")
        sign = "+" if c["pnl_pct"] >= 0 else ""
        lines.append(f"Return: {sign}{c['pnl_pct']:.2f}%  |  P&L: ₹{c['pnl_rupees']:.2f}")
        lines.append(f"Holding Days: {c.get('holding_days', 'N/A')}")
        lines.append(f"Exit Reason: {short_trigger.get(c['trigger'], c['trigger'])}")
        exit_reasons = c.get("exit_reasons") or []
        if exit_reasons:
            lines.append("Reason:")
            for r in exit_reasons:
                lines.append(f"  • {r}")
                if "flagged this symbol as unsafe" in r.lower():
                    for factor in c.get("risk_factor_detail") or []:
                        lines.append(f"      - {factor}")
        if c.get("exit_score") is not None:
            threshold = c.get("exit_threshold")
            if threshold is not None:
                lines.append(f"Exit Score: {c['exit_score']:.1f} / 100")
                lines.append(f"Threshold : {threshold:.0f}")
            else:
                lines.append(f"Exit Score: {c['exit_score']:.1f}/100")
        t1 = c.get("target1_status")
        t2 = c.get("target2_status")
        sl = c.get("stop_loss_status")
        if t1 and t1 != "N/A":
            lines.append(f"Target 1: {t1}")
        if t2 and t2 != "N/A":
            lines.append(f"Target 2: {t2}")
        if sl and sl != "N/A":
            lines.append(f"Stop Loss: {sl}")
        return lines

    @staticmethod
    def _classify_exit_trigger(exit_eval: Any) -> str:
        """Classifies the ALREADY-COMPUTED exit reason into a short
        label. Purely a text categorization of exit_eval's existing
        output (hard_risk_reason / reasons) — does not change when or
        why ExitEngine decides to exit, only how it's labeled here."""
        reason_text = (exit_eval.hard_risk_reason or "").lower()
        if "stop-loss" in reason_text:
            return "Stop Loss Hit"
        if "target" in reason_text and "reached" in reason_text:
            return "Target Achieved"
        if "risk engine" in reason_text or "unsafe" in reason_text:
            return "Risk Management Exit"
        if "maximum holding" in reason_text:
            return "Time-Based Exit"
        # Weighted-score exit (no hard-risk override) — look at which
        # sub-score(s) were the biggest drivers, from exit_eval's own
        # already-computed breakdown.
        subscores = {
            "Momentum Weakened": exit_eval.technical_exit,
            "Fundamentals Weakened": exit_eval.fundamental_exit,
            "Negative News": exit_eval.news_exit,
            "Risk Management Exit": exit_eval.risk_exit,
        }
        top = max(subscores, key=subscores.get)
        return "Trend Reversal" if top == "Momentum Weakened" and exit_eval.technical_exit >= 80 else top

    def _format_trade_closed(
        self, symbol: str, closed: Any, exit_price: float, pnl_pct: float,
        holding_days: int, exit_eval: Any, trade_id: str,
        created_at: float | None, closed_at: float | None,
    ) -> str:
        trigger = self._classify_exit_trigger(exit_eval)
        explanation = exit_eval.hard_risk_reason or (
            exit_eval.reasons[-1] if exit_eval.reasons else "N/A"
        )
        top_reasons = "\n".join(f"• {r}" for r in exit_eval.reasons[:5]) or "• N/A"
        pnl_rupees = closed.realized_pnl
        return (
            f"🔴 Virtual Trade Closed: {symbol} ({closed.direction})\n"
            f"Trade ID: {trade_id}\n"
            f"Holding Days: {holding_days}\n"
            f"Entry: {closed.entry_price}\n"
            f"Exit: {exit_price}\n"
            f"Total Return: {pnl_pct:.2f}%\n"
            f"Total P&L: ₹{pnl_rupees:.2f}\n"
            f"Exit Reason: {trigger}\n"
            f"Why: {explanation}\n\n"
            f"Top Reasons\n{top_reasons}\n\n"
            f"Lifecycle\n"
            f"Opened: {self._fmt_ts(created_at)}\n"
            f"Closed: {self._fmt_ts(closed_at)}\n"
            f"Holding: {holding_days} Days"
        )
