"""
Event Detection Engine.

Responsibilities:
- Detect important market events from normalized news
- Assign severity
- Normalize event structure

No sentiment analysis.
No BUY/SELL logic.
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger
from market.macro_intelligence import text_matches_any_keyword

logger = get_logger(__name__)


class EventDetectionEngine:
    """Detect structured events from news."""

    EVENT_MAP = {
        "MERGER": ["merger", "merge", "acquisition", "acquire"],
        "RESULT": ["result", "earnings", "quarter", "guidance"],
        "DIVIDEND": ["dividend"],
        "BUYBACK": ["buyback"],
        "ORDER": ["order", "contract"],
        "SEBI": ["sebi"],
        "LITIGATION": ["litigation", "lawsuit", "court"],
        "RATING_UPGRADE": ["upgrade"],
        "RATING_DOWNGRADE": ["downgrade"],
    }

    SEVERITY = {
        "MERGER": 90,
        "BUYBACK": 85,
        "RESULT": 75,
        "ORDER": 70,
        "DIVIDEND": 60,
        "RATING_UPGRADE": 65,
        "RATING_DOWNGRADE": 65,
        "SEBI": 95,
        "LITIGATION": 90,
        "GENERAL": 25,
    }

    def detect(self, news: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        for item in news:
            title = str(item.get("title", ""))
            event = self._event_type(title)

            events.append(
                {
                    **item,
                    "event_type": event,
                    "severity": self.SEVERITY[event],
                }
            )

        logger.info("Detected %d events.", len(events))
        return events

    def _event_type(self, title: str) -> str:
        # BUGFIX (2026-09-18, Phase 4 audit cleanup, same class of bug as
        # BUG_AUDIT_2026-09-18.md item #4 already fixed in
        # market/macro_intelligence.py, and news/sentiment_engine.py's own
        # earlier ACCURACY FIX): this used to be plain substring matching
        # ("word in text"), not whole-word. E.g. "order" (-> ORDER event)
        # matched inside "reorder"/"disorder"/"recorder"; "merge" (->
        # MERGER) matched inside "emerge"/"emergency"/"submerged"; "court"
        # (-> LITIGATION) matched inside "courtesy". Now uses \b-bounded
        # whole-word/whole-phrase matching via the shared helper.
        text = title.lower()

        for event, keywords in self.EVENT_MAP.items():
            if text_matches_any_keyword(text, keywords):
                return event

        return "GENERAL"
