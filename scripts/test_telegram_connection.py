"""
TELEGRAM CONNECTIVITY TEST

Sends exactly ONE message to verify TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
are correctly configured (as GitHub Actions secrets, or local env vars).

Does not touch trading strategy, BUY/SELL logic, or Paper Trading — this
only exercises the existing output/telegram_alert.py send path.

Usage:
    python scripts/test_telegram_connection.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.logger import get_logger  # noqa: E402
from core.trading_calendar import now_ist  # noqa: E402
from output.telegram_alert import TelegramAlert  # noqa: E402

logger = get_logger(__name__)


def _repo_name() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY")
    if env_repo:
        return env_repo
    try:
        url = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return url.rsplit("/", 1)[-1].removesuffix(".git")
    except Exception:
        return "Unknown Repository"


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in the environment.")
        print("Set them as GitHub Actions secrets, or export them locally, then retry.")
        sys.exit(1)

    repo_name = _repo_name()
    ist_time = now_ist().strftime("%Y-%m-%d %H:%M:%S")

    message = (
        "----------------------------------\n"
        "✅ Trading trail Bot Connected\n"
        "Repository:\n"
        f"{repo_name}\n"
        "Telegram notifications are working successfully.\n"
        "Time:\n"
        f"{ist_time} IST\n"
        "----------------------------------"
    )

    telegram = TelegramAlert(bot_token=token, chat_id=chat_id)
    telegram.send(message, raw=True)

    print("Connectivity test message sent.")
    print(message)


if __name__ == "__main__":
    main()
