"""
AI News Engine.

Responsibilities:
- Clean news
- Remove duplicates
- Extract entities
- Detect events
- Prepare normalized news for sentiment engine

No sentiment scoring.
No BUY/SELL logic.
"""

from __future__ import annotations

import re
from typing import Any

from core.logger import get_logger
from market.macro_intelligence import text_matches_any_keyword

logger = get_logger(__name__)


class AINewsEngine:
    """Clean and normalize raw news."""

    EVENT_KEYWORDS = {
        "MERGER": ["merge", "merger", "acquisition", "acquire"],
        "RESULT": ["quarter", "earnings", "result", "guidance"],
        "DIVIDEND": ["dividend"],
        "BUYBACK": ["buyback"],
        "LITIGATION": ["court", "lawsuit", "litigation"],
        "SEBI": ["sebi"],
        "ORDER": ["order", "contract"],
        "RATING": ["upgrade", "downgrade", "rating"],
    }

    def process(
        self,
        news: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Normalize news items.
        """
        if not news:
            return []

        cleaned: list[dict[str, Any]] = []
        seen_titles: set[str] = set()

        for item in news:
            title = str(item.get("title", "")).strip()

            if not title:
                continue

            key = title.lower()

            if key in seen_titles:
                continue

            seen_titles.add(key)

            cleaned.append(
                {
                    "title": title,
                    "publisher": item.get("publisher"),
                    "published_at": item.get("published_at"),
                    "link": item.get("link"),
                    "entities": self._extract_entities(title),
                    "event": self._detect_event(title),
                }
            )

        logger.info("Processed %d news items.", len(cleaned))

        return cleaned

    def _extract_entities(self, text: str) -> list[str]:
        """
        Simple entity extraction based on capitalized words.
        """
        entities = re.findall(r"\b[A-Z][A-Za-z0-9&.-]*\b", text)
        return sorted(set(entities))

    def _detect_event(self, text: str) -> str:
        """
        Detect the primary event type.

        BUGFIX (2026-09-18, Phase 4 audit cleanup, same class of bug as
        BUG_AUDIT_2026-09-18.md item #4 already fixed in
        market/macro_intelligence.py, and news/sentiment_engine.py's own
        earlier ACCURACY FIX): this used to be plain substring matching
        ("keyword in lower"), not whole-word -- e.g. "order" (-> ORDER)
        matched inside "reorder"/"disorder", "court" (-> LITIGATION)
        matched inside "courtesy". Now uses \\b-bounded whole-word/
        whole-phrase matching via the shared helper.
        """
        lower = text.lower()

        for event, keywords in self.EVENT_KEYWORDS.items():
            if text_matches_any_keyword(lower, keywords):
                return event

        return "GENERAL"
