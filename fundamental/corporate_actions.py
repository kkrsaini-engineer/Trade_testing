"""
Corporate Actions Engine.

Responsibilities:
- Normalize corporate actions
- Categorize events
- Assign impact weights
- No BUY/SELL decisions

STATUS (2026-09-18, Phase 4 dead-code cleanup, per BUG_AUDIT_2026-09-18.md
"Dead code" section): confirmed via repo-wide grep that `CorporateActionsEngine`
has zero live callers anywhere in the pipeline today. Read through
`evaluate()` in full looking for a functional defect (the class it's grouped
with in the audit, `fundamental/fundamental.py`'s `FundamentalEngine`, DOES
have a real weight-drift bug -- see that module) and found none here: the
IMPACT_WEIGHTS table, normalization, and score clamping are all internally
consistent. Left unchanged rather than "fixed" for a bug that doesn't exist,
and kept in place (not deleted) per team decision, in case corporate-actions
scoring is wired into the live decision pipeline in the future.
"""

from __future__ import annotations

from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)


class CorporateActionsEngine:
    """Normalize and score corporate actions."""

    IMPACT_WEIGHTS = {
        "DIVIDEND": 10,
        "SPLIT": 15,
        "BONUS": 15,
        "RIGHTS": -5,
        "BUYBACK": 25,
        "MERGER": 20,
        "DEMERGER": 15,
        "CEO_CHANGE": -5,
        "CFO_CHANGE": -3,
        "BOARD_CHANGE": -2,
        "LARGE_ORDER": 20,
        "LITIGATION": -30,
        "SEBI": -35,
        "INCOME_TAX": -20,
        "CREDIT_RATING_UPGRADE": 20,
        "CREDIT_RATING_DOWNGRADE": -25,
    }

    def evaluate(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Evaluate corporate actions.

        Returns:
            {
                "overall_score": float,
                "events": [...]
            }
        """
        if not actions:
            return {
                "overall_score": 50.0,
                "events": [],
            }

        normalized: list[dict[str, Any]] = []
        total = 0.0

        for action in actions:
            event = str(action.get("type", "")).strip().upper()
            weight = self.IMPACT_WEIGHTS.get(event, 0)

            normalized.append(
                {
                    "type": event,
                    "date": action.get("date"),
                    "description": action.get("description"),
                    "impact": weight,
                }
            )

            total += weight

        score = max(0.0, min(100.0, 50.0 + total))

        logger.info("Processed %d corporate actions.", len(normalized))

        return {
            "overall_score": round(score, 2),
            "events": normalized,
        }
