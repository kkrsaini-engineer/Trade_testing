"""
NOTIFICATIONS (shared Telegram helper)

Single canonical implementation of "send an advisory Telegram message,
with a severity level, without repeating the same event" — reused by
every module that needs to notify (Daily Scan, Paper Trading, Exit
Engine observations, Trade Diary, Analysis/Learning/Optimizer/
Regression, Market Intelligence).

Telegram is ONLY a notification channel. Nothing in this module (or any
of its callers) opens/closes trades, executes BUY/SELL/EXIT, or
modifies any probability, confidence, or weight — it only reports what
the production engine or research modules have already decided/observed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.logger import get_logger
from output.telegram_alert import TelegramAlert

logger = get_logger(__name__)

DEDUP_STORE_PATH = "storage/reports/telegram_dedup.json"

SEVERITY_LOW = "🟢 LOW"
SEVERITY_MEDIUM = "🟡 MEDIUM"
SEVERITY_HIGH = "🟠 HIGH"
SEVERITY_CRITICAL = "🔴 CRITICAL"


def severity_from_magnitude(magnitude: float) -> str:
    """Classify an unsigned 0-1 magnitude into the 4 required severity
    levels. Advisory labeling only — does not affect any decision."""
    magnitude = abs(magnitude)
    if magnitude >= 0.85:
        return SEVERITY_CRITICAL
    if magnitude >= 0.65:
        return SEVERITY_HIGH
    if magnitude >= 0.45:
        return SEVERITY_MEDIUM
    return SEVERITY_LOW


_telegram_singleton: TelegramAlert | None = None
_telegram_checked = False


def _get_telegram() -> TelegramAlert | None:
    global _telegram_singleton, _telegram_checked
    if not _telegram_checked:
        _telegram_checked = True
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            _telegram_singleton = TelegramAlert(bot_token=token, chat_id=chat_id)
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — "
                "notifications will be logged only, not sent."
            )
    return _telegram_singleton


def _load_dedup_store() -> dict[str, list[str]]:
    path = Path(DEDUP_STORE_PATH)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    today_key = time.strftime("%Y-%m-%d")
    return {today_key: data.get(today_key, [])}


def _save_dedup_store(store: dict[str, list[str]]) -> None:
    Path(DEDUP_STORE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(DEDUP_STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)


def notify(
    event_type: str,
    message: str,
    severity: str = SEVERITY_LOW,
    dedup_key: str | None = None,
) -> bool:
    """
    Send an advisory Telegram notification (or log it if no Telegram
    credentials are configured). Returns True if actually sent/logged,
    False if suppressed as a duplicate.

    dedup_key: a stable string identifying "this specific event" (e.g.
    f"trade_opened::{symbol}::{price}"). If the SAME dedup_key was
    already notified today, this call is a no-op — prevents repeatedly
    notifying the same event across multiple runs in one day.
    If dedup_key is None, event_type alone is used (so at most one
    notification per event_type per day, e.g. "daily_scan_started").
    """
    key = dedup_key or event_type
    today_key = time.strftime("%Y-%m-%d")
    store = _load_dedup_store()

    if key in store.get(today_key, []):
        logger.info("Notification suppressed (duplicate today): %s", key)
        return False

    full_message = f"{severity}\n{message}"
    telegram = _get_telegram()
    if telegram is not None:
        # Telegram hard-limits messages to 4096 characters. Confirmed
        # via production logs that this can be hit by more than one
        # notification type (Holding Status, Monitoring Summary with a
        # long failure list, ...) — splitting HERE, once, at the
        # central choke-point protects every current and future
        # notify() call instead of fixing each oversized message
        # individually as it's discovered.
        TELEGRAM_SAFE_LIMIT = 4000

        # BUGFIX (2026-09-18, Phase 2 — see BUG_AUDIT_2026-09-18.md item
        # #5): output/telegram_alert.py's TelegramAlert.send() logs its
        # own failures but then deliberately RE-RAISES them (network
        # error, invalid bot token/chat id, Telegram rate-limit, etc.).
        # This module's own docstring says Telegram is ONLY an advisory
        # notification channel that must never affect the production
        # engine — but with no try/except here, an uncaught send()
        # exception propagated straight out of notify() and crashed
        # whatever real trading/paper-trading/orchestrator/market-
        # intelligence code path happened to call notify(), taking down
        # actual trading logic because of a Telegram-side hiccup having
        # nothing to do with trading. Every telegram.send() call below
        # is now guarded: a failure is logged and swallowed here, never
        # propagated to the caller.
        if len(full_message) <= TELEGRAM_SAFE_LIMIT:
            try:
                telegram.send(full_message, level=event_type.upper())
            except Exception as exc:
                logger.error("Telegram notification failed (swallowed, not fatal): %s", exc)
        else:
            lines = full_message.split("\n")
            parts: list[str] = []
            current: list[str] = []
            current_len = 0
            for line in lines:
                line_len = len(line) + 1
                if current and current_len + line_len > TELEGRAM_SAFE_LIMIT:
                    parts.append("\n".join(current))
                    current, current_len = [], 0
                current.append(line)
                current_len += line_len
            if current:
                parts.append("\n".join(current))
            total = len(parts)
            for i, part in enumerate(parts, start=1):
                try:
                    telegram.send(f"(Part {i}/{total})\n{part}", level=event_type.upper())
                except Exception as exc:
                    logger.error(
                        "Telegram notification part %d/%d failed (swallowed, not fatal): %s",
                        i, total, exc,
                    )
    else:
        logger.info("[Notification — no Telegram configured] %s", full_message)

    store.setdefault(today_key, []).append(key)
    _save_dedup_store(store)
    return True
