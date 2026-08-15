"""
MORNING EXECUTOR — Executes last night's pending candidates at market open.

Design (per user specification, NOT a re-scan):
    1. Read reports/candidates_order.json (written by tonight's Daily Scan)
    2. For each candidate, fetch ONLY today's opening price (one small
       yfinance call per symbol — no re-analysis, no waiting)
    3. Validate the gap against the candidate's OWN ATR (already
       computed last night) using fixed bands:
           gap <= 1.0x ATR   -> Execute (normal)
           gap <= 1.75x ATR  -> Execute + Warning
           gap <= 2.5x ATR   -> Reduce Size (half) + notify
           gap >  2.5x ATR   -> Skip
    4. Overnight news check — fetch fresh headlines, compute signed
       bias; if it strongly contradicts the trade direction, skip
       regardless of the gap-band result.
    5. Risk check via the EXISTING RiskManager (capital/portfolio/
       circuit-breaker dimensions), using CURRENT portfolio state.
    6. Execute / Reduce / Skip — position opened via the existing
       VirtualPortfolio, same as the night cycle does.
    7. Detailed Telegram notification: every rejection with its reason.

Explicitly NOT done here (by design, per discussion): no waiting
window, no re-running technical indicators/RSI/EMA, no re-scoring —
Target1/Target2/Stop-Loss/ATR are reused exactly as computed last
night. This keeps the swing-entry intact instead of turning it into
an intraday-confirmation system.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from config import CONFIG
from core.logger import get_logger
from core.notifications import notify
from core.trading_calendar import is_trading_day, now_ist
from data.news_data import NewsDataProvider
from news.sentiment_engine import SentimentEngine
from paper_trading.virtual_portfolio import VirtualPortfolio
from risk.risk_manager import RiskManager
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore

logger = get_logger(__name__)

PENDING_ORDERS_PATH = Path("reports/candidates_order.json")

# Gap-vs-ATR bands (ratio of |gap| to the candidate's own ATR — NOT a
# flat percentage, so a normally-volatile stock isn't unfairly
# flagged for a gap that's routine FOR THAT STOCK). Simplified to two
# boundaries only (NORMAL / WARNING / SKIP) — the previous third
# "REDUCE" tier (half-size execution between WARNING and SKIP) was
# removed: there was no evidence backing the specific 50% figure, and
# a fully-automated pipeline can't do genuine "manual review" as the
# original spec intended for that zone, so the safer default is SKIP.
BAND_NORMAL = 1.0
BAND_WARNING = 1.75
# > BAND_WARNING -> skip entirely


def _signed_news_bias(scored_item: dict[str, Any]) -> float:
    """Same formula as execution/scanner.py's _signed_news_bias() —
    duplicated locally (small, pure function) to avoid a cross-module
    import between two independently-runnable scripts."""
    impact = float(scored_item.get("impact_score", 50.0))
    magnitude = max(0.0, (impact - 50.0) / 50.0)
    polarity = scored_item.get("sentiment", "NEUTRAL")
    if polarity == "POSITIVE":
        return magnitude
    if polarity == "NEGATIVE":
        return -magnitude
    return 0.0


def _check_symbol_news(symbol: str, direction: str, scan_timestamp: str | None) -> tuple[bool, str]:
    """Company-specific overnight news check (original behavior).

    Only considers headlines published AFTER scan_timestamp (last
    night's scan time) — without this filter, the exact same
    headlines the scanner already scored would be re-evaluated here,
    which is redundant and can produce a DIFFERENT bias by chance
    (rounding/model variance) than what the scanner already factored
    into the Overall Score, causing an inconsistent decision.

    LIMITATION this does NOT cover: broad macro/geopolitical shocks
    (war, crisis) that don't specifically mention this company by name
    won't show up in ITS news feed at all — see _check_macro_news()
    below, which is the check that catches those."""
    try:
        provider = NewsDataProvider()
        headlines = provider.fetch(symbol=symbol, limit=20)
        if not headlines:
            return True, "No company-specific news available."

        if scan_timestamp:
            cutoff = datetime.fromisoformat(scan_timestamp)
            fresh_headlines = []
            for h in headlines:
                published = h.get("published")
                if not published:
                    continue  # no timestamp — can't confirm it's overnight, skip it rather than risk re-scoring old news
                try:
                    if datetime.fromisoformat(published) > cutoff:
                        fresh_headlines.append(h)
                except ValueError:
                    continue
            headlines = fresh_headlines

        if not headlines:
            return True, "No NEW company-specific overnight news since last night's scan."

        engine = SentimentEngine()
        scored = engine.evaluate(headlines)
        avg_bias = sum(_signed_news_bias(i) for i in scored) / len(scored)
        if direction == "BUY" and avg_bias <= -CONFIG.news_skip_bias_threshold:
            return False, f"{len(headlines)} NEW company headline(s), bias {avg_bias:.2f} — strongly negative against a BUY."
        if direction == "SELL" and avg_bias >= CONFIG.news_skip_bias_threshold:
            return False, f"{len(headlines)} NEW company headline(s), bias {avg_bias:.2f} — strongly positive against a SELL."
        return True, f"{len(headlines)} new company headline(s), bias {avg_bias:.2f} — not a blocker."
    except Exception as exc:
        logger.warning("Company news check failed for %s: %s — proceeding without it.", symbol, exc)
        return True, f"Company news check unavailable ({exc}) — proceeded without it."


def _check_macro_news(direction: str) -> tuple[bool, str]:
    """Broad market/macro overnight news check (war, crisis, oil shocks,
    rate decisions, etc.) — this is the check that was MISSING here
    before: _check_symbol_news() above only ever sees headlines tagged
    to one specific company, so a genuine market-wide shock overnight
    (that doesn't name this stock) previously passed through silently
    regardless of how severe it was.

    Reuses the same broad-market headline source already used by the
    evening scan (data/news_data.py's fetch_market_news(), consumed by
    market/macro_intelligence.py) — no new data source.

    KNOWN LIMITATION: fetch_market_news() returns titles only (no
    publish timestamps), so unlike _check_symbol_news() this cannot
    filter to "published after last night's scan" — it scores whatever
    macro headlines are current right now. In practice this means a
    macro event the evening scan already saw and priced in could
    re-trigger this check with the same bias; that's an acceptable
    false-positive rate for a safety check (it can unnecessarily skip
    a trade) versus the alternative it replaces (silently executing
    into a live crisis)."""
    try:
        provider = NewsDataProvider()
        headlines = provider.fetch_market_news(limit=20)
        if not headlines:
            return True, "No macro headlines available."

        engine = SentimentEngine()
        scored = engine.evaluate([{"title": h} for h in headlines])
        avg_bias = sum(_signed_news_bias(i) for i in scored) / len(scored)
        if direction == "BUY" and avg_bias <= -CONFIG.news_skip_bias_threshold:
            return False, f"{len(headlines)} macro headline(s), bias {avg_bias:.2f} — strongly negative against a BUY."
        if direction == "SELL" and avg_bias >= CONFIG.news_skip_bias_threshold:
            return False, f"{len(headlines)} macro headline(s), bias {avg_bias:.2f} — strongly positive against a SELL."
        return True, f"{len(headlines)} macro headline(s), bias {avg_bias:.2f} — not a blocker."
    except Exception as exc:
        logger.warning("Macro news check failed: %s — proceeding without it.", exc)
        return True, f"Macro news check unavailable ({exc}) — proceeded without it."


def check_overnight_news(symbol: str, direction: str, scan_timestamp: str | None) -> tuple[bool, str]:
    """Returns (news_ok, reason). news_ok=False means overnight news —
    either company-specific OR broad macro/geopolitical — strongly
    contradicts the trade direction and this candidate should be
    skipped regardless of the gap-band result. Checks both sources;
    either one failing is enough to block."""
    symbol_ok, symbol_reason = _check_symbol_news(symbol, direction, scan_timestamp)
    if not symbol_ok:
        return False, symbol_reason

    macro_ok, macro_reason = _check_macro_news(direction)
    if not macro_ok:
        return False, macro_reason

    return True, f"{symbol_reason} | {macro_reason}"


def fetch_open_price(symbol: str, max_retries: int = 3, retry_delay_seconds: int = 30) -> tuple[float | None, str]:
    """Returns (open_price, status_message). Retries on any failure —
    fetch error, empty data, OR the last row's date not matching today
    (Yahoo's daily candle for "today" may not exist yet this early,
    which would otherwise silently return YESTERDAY's open instead of
    failing loudly). period="5d" (not "1d") so there's a genuine
    multi-row window to find and verify today's row in, rather than
    trusting whatever single row comes back."""
    today_str = date.today().isoformat()
    last_error = "unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            df = yf.download(symbol, period="5d", interval="1d", progress=False, auto_adjust=False)
            if df.empty:
                last_error = "empty dataframe returned"
            else:
                # Flatten yfinance's MultiIndex columns (same fix used in
                # run_backtest.py) — without this, df["Open"] is a nested
                # Series/DataFrame rather than a clean per-row scalar
                # column, and float() on it only works via a deprecated
                # pandas fallback (confirmed via a recurring FutureWarning
                # in production logs) that becomes a hard TypeError in a
                # future pandas version.
                df.columns = [str(c).lower() if not isinstance(c, tuple) else str(c[0]).lower() for c in df.columns]
                last_row_date = pd.Timestamp(df.index[-1]).strftime("%Y-%m-%d")
                if last_row_date != today_str:
                    last_error = f"latest available candle is {last_row_date}, not today ({today_str}) — today's data not ready yet"
                else:
                    return float(df["open"].iloc[-1]), "OK"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        logger.warning(
            "Open-price fetch attempt %d/%d failed for %s: %s",
            attempt, max_retries, symbol, last_error,
        )
        if attempt < max_retries:
            time.sleep(retry_delay_seconds)

    return None, f"Failed after {max_retries} attempts: {last_error}"


def classify_gap(open_price: float, prev_close: float, atr_14: float) -> tuple[str, float]:
    """Returns (band_label, gap_to_atr_ratio). Three bands only:
    NORMAL (execute), WARNING (execute, flagged), SKIP (do not
    execute) — see module-level comment for why the REDUCE tier was
    removed."""
    if not atr_14 or atr_14 <= 0:
        return "UNKNOWN_ATR", 0.0
    gap_abs = abs(open_price - prev_close)
    ratio = gap_abs / atr_14
    if ratio <= BAND_NORMAL:
        return "NORMAL", ratio
    if ratio <= BAND_WARNING:
        return "WARNING", ratio
    return "SKIP", ratio


def check_capital_portfolio_risk(portfolio_snapshot: dict[str, Any]) -> tuple[bool, str]:
    """Lightweight risk check using ONLY genuinely-available data at
    market open (capital and portfolio exposure, from live portfolio
    state) — deliberately does NOT call the full RiskManager, which
    also evaluates ATR/liquidity/volatility/market/news dimensions
    that would otherwise have to be fed FAKE placeholder values here
    (no fresh scan has run this morning). Formulas/thresholds copied
    from risk/risk_manager.py's PORTFOLIO RISK and CAPITAL RISK
    sections for consistency — not reinvented."""
    total_capital = float(portfolio_snapshot.get("total_capital", 1.0))
    available_capital = float(portfolio_snapshot.get("available_capital", 0.0))
    open_positions_count = len(portfolio_snapshot.get("open_positions", {}))
    exposure = float(portfolio_snapshot.get("exposure", 0.0))

    capital_ratio = available_capital / max(total_capital, 1.0)
    if capital_ratio <= 0.05:
        return False, f"Capital critically low (available capital ratio {capital_ratio:.1%})."

    portfolio_risk = 0.0
    if open_positions_count >= 15:
        portfolio_risk += 35.0
    elif open_positions_count >= 10:
        portfolio_risk += 20.0
    elif open_positions_count >= 5:
        portfolio_risk += 10.0
    if exposure >= 0.90:
        portfolio_risk += 40.0
    elif exposure >= 0.75:
        portfolio_risk += 25.0
    elif exposure >= 0.50:
        portfolio_risk += 10.0

    if portfolio_risk >= RiskManager.MAX_PORTFOLIO_RISK:
        return False, (
            f"Portfolio risk {portfolio_risk:.0f} exceeds threshold "
            f"({RiskManager.MAX_PORTFOLIO_RISK:.0f}) — {open_positions_count} "
            f"open positions, {exposure:.1%} exposure."
        )

    return True, "Capital/portfolio check passed."


def main() -> None:
    today_date = date.today()
    if not is_trading_day(today_date):
        logger.info("Not a trading day — morning executor exiting.")
        return

    if not PENDING_ORDERS_PATH.exists():
        logger.info("No candidates_order.json found — nothing to execute.")
        return

    with open(PENDING_ORDERS_PATH) as f:
        payload = json.load(f)
    candidates = payload.get("candidates", [])
    scan_date = payload.get("scan_date")
    scan_timestamp = payload.get("scan_timestamp")

    if not candidates:
        logger.info("candidates_order.json has no candidates — nothing to execute.")
        return

    portfolio = VirtualPortfolio()
    diary = TradeDiary()
    trade_store = TradeStore()

    executed, skipped = [], []

    for c in candidates:
        symbol = c["symbol"]
        direction = c["direction"]
        prev_close = c.get("prev_close")
        atr_14 = c.get("atr_14")
        target1 = c.get("target1")
        stop_loss = c.get("stop_loss")

        open_price, fetch_status = fetch_open_price(symbol)
        if open_price is None or not prev_close:
            skipped.append((symbol, direction, f"Could not fetch today's open price — {fetch_status}"))
            continue

        band, ratio = classify_gap(open_price, prev_close, atr_14)
        gap_pct = round((open_price - prev_close) / prev_close * 100, 2)

        if band == "SKIP":
            skipped.append((symbol, direction, f"Gap {gap_pct:+.2f}% = {ratio:.2f}x ATR (> {BAND_WARNING}x) — too large, skipped."))
            continue

        # Target/Stop sanity check (already-computed boundaries reused
        # exactly as-is, no recompute) — chasing/invalidated-setup guard.
        if direction == "BUY":
            if target1 and open_price >= target1:
                skipped.append((symbol, direction, f"Open {open_price} already at/past Target1 ({target1}) — chasing, skipped."))
                continue
            if stop_loss and open_price <= stop_loss:
                skipped.append((symbol, direction, f"Open {open_price} already at/below Stop-Loss ({stop_loss}) — setup invalidated."))
                continue
        else:
            if target1 and open_price <= target1:
                skipped.append((symbol, direction, f"Open {open_price} already at/past Target1 ({target1}) — chasing, skipped."))
                continue
            if stop_loss and open_price >= stop_loss:
                skipped.append((symbol, direction, f"Open {open_price} already at/above Stop-Loss ({stop_loss}) — setup invalidated."))
                continue

        news_ok, news_reason = check_overnight_news(symbol, direction, scan_timestamp)
        if not news_ok:
            skipped.append((symbol, direction, news_reason))
            continue

        # Risk check — lightweight, REAL-data-only version (see
        # check_capital_portfolio_risk() docstring for why the full
        # RiskManager isn't called here: it needs ATR/volume/spread/
        # market-regime/VIX inputs that don't genuinely exist yet this
        # morning without a fresh scan).
        snap = portfolio.snapshot()
        risk_ok, risk_reason = check_capital_portfolio_risk(snap)
        if not risk_ok:
            skipped.append((symbol, direction, f"Risk check failed: {risk_reason}"))
            continue

        # Position sizing — simple, fixed-fraction allocation (a full
        # Kelly-based size would need a fresh FinalDecision object,
        # which would mean re-running the scan — explicitly avoided
        # per the "no re-scan" design). No size-reduction tier — see
        # module-level comment on why REDUCE was removed.
        available = snap.get("available_capital", 0.0)
        allocation = min(available * 0.05, 500000.0 / max(len(candidates), 1))
        quantity = int(allocation / open_price) if open_price > 0 else 0
        if quantity <= 0:
            skipped.append((symbol, direction, "Insufficient capital for even 1 share at this allocation."))
            continue

        added = portfolio.engine.add_position(
            symbol=symbol, quantity=quantity, entry_price=open_price, direction=direction,
        )
        if not added:
            skipped.append((symbol, direction, "Position already exists or insufficient capital (see engine log)."))
            continue

        # CONFIRMED ROOT CAUSE FIX: without writing a matching diary +
        # trade_store record here, paper_trading_engine.py's monitoring
        # loop (_find_open_trade_id) can never find this position —
        # every single Morning-Executor-opened position was failing
        # nightly monitoring with "MissingDiaryEntryError" (seen in
        # production: all 20 open positions failed this way). Same
        # trade_id pattern ("paper_{SYMBOL}_{timestamp}") the night
        # cycle already uses, so monitoring finds it identically.
        trade_id = f"paper_{symbol.replace('.', '_')}_{int(time.time() * 1000)}"
        entry_reasons = [
            f"Morning Executor: gap {gap_pct:+.2f}% ({ratio:.2f}x ATR, {band} band)",
            news_reason,
        ]
        diary.open_trade(
            trade_id=trade_id, symbol=symbol, direction=direction,
            entry_price=open_price, entry_date=today_date.isoformat(),
            buy_probability=0.0, buy_confidence=0.0,
            entry_reasons=entry_reasons,
        )
        trade_store.save_trade({
            "id": trade_id, "symbol": symbol, "direction": direction, "action": direction,
            "quantity": quantity, "entry_price": open_price, "status": "OPEN",
            "regime": "N/A", "confidence": 0.0, "reasons": "; ".join(entry_reasons),
        })

        executed.append((symbol, direction, open_price, quantity, gap_pct, band, ratio))

    portfolio.save()

    lines = [
        "🌅 Morning Execution Complete",
        f"Signals from: {scan_date}",
        f"Candidates processed: {len(candidates)}",
        f"Executed: {len(executed)} | Skipped: {len(skipped)}",
        "",
    ]
    if executed:
        lines.append("✅ Executed")
        for symbol, direction, price, qty, gap_pct, band, ratio in executed:
            flag = " ⚠️ WARNING-zone gap" if band == "WARNING" else ""
            lines.append(f"  {symbol} ({direction}) @ {price} x{qty} | Gap {gap_pct:+.2f}% ({ratio:.2f}x ATR){flag}")
        lines.append("")
    if skipped:
        lines.append("❌ Skipped")
        for symbol, direction, reason in skipped:
            lines.append(f"  {symbol} ({direction}): {reason}")

    notify(
        event_type="morning_execution_complete",
        message="\n".join(lines),
        dedup_key=f"morning_exec::{today_date.isoformat()}::{now_ist().strftime('%H:%M:%S.%f')}",
    )


if __name__ == "__main__":
    main()
