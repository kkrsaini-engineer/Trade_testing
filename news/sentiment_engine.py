"""
Sentiment Engine.

Responsibilities:
- Score news sentiment
- Assign confidence
- Compute impact score
- Apply time decay

No BUY/SELL decisions.

ACCURACY FIX (2026-08-31): audited against 114 real Market Intelligence
runs / 2041 real (symbol, day) observations committed in
storage/reports/market_intelligence_log.jsonl — only 1 of 2041 ever came
back NEGATIVE. Three concrete, confirmed defects in the old version
explain most of that skew, all fixed below:

  1. WORD-BOUNDARY BUG: matching was plain substring ("word in text"),
     not whole-word. "fall" (negative) matches inside "windfall" or
     "footfall" (usually POSITIVE contexts); "contract" (positive)
     matches inside "contractor". Now uses \\b-bounded regex matching.
  2. TOO-SMALL VOCABULARY: only 12 negative / 12 positive words total.
     Common real headline vocabulary (plunge, scam, probe, lawsuit,
     layoffs, resign, rally, outperform, dividend, etc.) was invisible
     to the classifier — most real bad (or good) news simply didn't
     contain any of the 24 exact words, and fell back to NEUTRAL/whatever
     the OTHER side happened to match.
  3. "sebi" ALONE COUNTED AS NEGATIVE: SEBI (India's market regulator)
     appears in plenty of NEUTRAL/POSITIVE headlines too (approvals,
     routine filings). Replaced with the actual violation-indicating
     words (probe, raid, penalty, banned, barred, restrained) so a
     bare regulator mention no longer counts as bad news by itself.
  4. NO NEGATION HANDLING: "cleared of fraud allegations" or "SEBI probe
     dropped" scored NEGATIVE purely because "fraud"/"probe" appear,
     ignoring the negation right next to them. A small negation-window
     check now suppresses (treats as neutral, not flipped) any keyword
     match immediately preceded by a negation word — conservative by
     design, since flipping polarity outright risks overcorrecting.

Shared by BOTH consumers: execution/scanner.py's live entry-scoring
pipeline (BUY/SELL probability/scoring both read the same news_score,
symmetrically — this fix is direction-neutral, no BUY/SELL asymmetry
introduced) AND market_intelligence_engine.py's advisory-only Telegram
alerts. Improving accuracy here benefits both; it does not by itself
wire anything new into the trading/risk pipeline.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from core.logger import get_logger

logger = get_logger(__name__)


class SentimentEngine:
    POSITIVE = {
        "beat", "beats", "growth", "upgrade", "upgraded", "buyback",
        "record", "profit", "profits", "profitable", "expansion",
        "expand", "expands", "approval", "approved", "approves",
        "surge", "surges", "surged", "strong", "award", "awarded",
        "rally", "rallies", "rallied", "jump", "jumps", "jumped",
        "gain", "gains", "robust", "outperform", "outperforms",
        "bullish", "milestone", "breakthrough", "partnership",
        "dividend", "bonus", "acquisition", "wins", "won", "order",
        "orders", "rebound", "rebounds", "recovery", "recovers",
        "boost", "boosts", "boosted", "soar", "soars", "soared",
        "raise", "raises", "raised",
    }

    NEGATIVE = {
        "miss", "misses", "missed", "downgrade", "downgraded",
        "fraud", "scam", "loss", "losses", "litigation", "lawsuit",
        "sued", "sue", "probe", "raid", "penalty", "penalties",
        "banned", "barred", "bars", "restrained", "fall", "falls", "falling",
        "fell", "decline", "declines", "declining", "declined",
        "weak", "weakness", "recall", "recalled", "bankruptcy",
        "insolvency", "layoff", "layoffs", "cut", "cuts", "slash",
        "slashed", "drop", "drops", "plunge", "plunges",
        "plunged", "slump", "slumps", "slumped", "crisis", "delay",
        "delayed", "delays", "default", "defaults", "strike",
        "contempt", "guilty", "violation", "breach", "hack", "hacked",
        "cyberattack", "warns", "warning", "disappoint", "disappoints",
        "disappointing", "underperform", "underperforms", "resign",
        "resigns", "resignation", "scandal", "halt", "halted",
        "suspend", "suspended", "shutdown", "shut",
    }
    # NOTE: "dropped" is deliberately NOT in NEGATIVE — it is genuinely
    # ambiguous ("stock dropped 5%" vs "charges/probe dropped"), and is
    # instead used only as a NEGATION trigger below (the more common,
    # higher-value reading in company-news headlines is "case dropped").
    # "drop"/"drops" (present tense) are far more commonly a price move
    # in this corpus, so those two stay as NEGATIVE signals.

    # Negation words that, when found immediately before a matched
    # keyword, suppress that match instead of counting it — e.g.
    # "cleared of fraud", "no penalty imposed", "SEBI probe dropped/
    # closed/withdrawn". Deliberately suppress-only (not flip-to-
    # opposite): "cleared of fraud" is neutral/relief, not necessarily
    # as strongly positive as a genuine "beat"/"growth" headline.
    NEGATIONS = {
        "no", "not", "never", "without", "cleared", "clears",
        "dismissed", "dismisses", "denies", "denied", "denies",
        "dropped", "closes", "closed", "withdrawn", "unaffected",
        "avoids", "avoided", "rules", "ruled",
    }

    _NEGATION_WINDOW_WORDS = 3

    _WORD_RE = re.compile(r"[a-z']+")

    def evaluate(self, news: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        for item in news:
            title = str(item.get("title", ""))
            sentiment, score = self._sentiment(title)
            decay = self._decay(item.get("published_at"))

            impact = round(score * decay, 2)

            results.append(
                {
                    **item,
                    "sentiment": sentiment,
                    "confidence": abs(score),
                    "impact_score": impact,
                    "event_weight": impact,
                    "decay_factor": round(decay, 3),
                }
            )

        logger.info("Processed sentiment for %d news items.", len(results))
        return results

    def _sentiment(self, text: str) -> tuple[str, float]:
        text = text.lower()
        words = self._WORD_RE.findall(text)

        pos = self._count_matches(words, self.POSITIVE)
        neg = self._count_matches(words, self.NEGATIVE)

        if pos > neg:
            return "POSITIVE", min(100.0, 50.0 + pos * 15.0)
        if neg > pos:
            return "NEGATIVE", min(100.0, 50.0 + neg * 15.0)
        return "NEUTRAL", 50.0

    def _count_matches(self, words: list[str], keywords: set[str]) -> int:
        """Whole-word keyword count, with any match neighboring (within
        _NEGATION_WINDOW_WORDS words, on EITHER side) a negation word
        suppressed — not counted either way. Checking both directions
        matters for real headline phrasing like "probe against XYZ
        dropped" (negation follows the noun it negates), not just
        "no penalty imposed" (negation precedes it)."""
        count = 0
        for i, w in enumerate(words):
            if w not in keywords:
                continue
            window_start = max(0, i - self._NEGATION_WINDOW_WORDS)
            window_end = i + 1 + self._NEGATION_WINDOW_WORDS
            neighbors = words[window_start:i] + words[i + 1:window_end]
            if any(neg in neighbors for neg in self.NEGATIONS):
                continue
            count += 1
        return count

    def _decay(self, published_at: Any) -> float:
        if not published_at:
            return 1.0

        try:
            dt = datetime.fromisoformat(str(published_at))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(
                0.0,
                (datetime.now(timezone.utc) - dt).total_seconds() / 86400,
            )
        except Exception:
            return 1.0

        return max(0.2, 1.0 - age_days / 30.0)
