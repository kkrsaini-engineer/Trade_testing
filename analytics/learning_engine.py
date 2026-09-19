"""
PHASE 2 — MODULE 2: LEARNING ENGINE (observation only)

Reads closed trades (storage/trades/trades_master.csv) plus the scan
history (reports/full_report.csv) and records — historically, append-only
— which rules/sectors/regimes/news/fundamentals correlate with winning vs
losing trades.

This module NEVER changes strategy parameters. It only observes and
stores observations for analytics/optimizer.py to later turn into
recommendations (which a human still has to approve and apply manually).
"""

from __future__ import annotations

import csv
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from storage.trades.trade_store import TradeStore


class LearningEngine:

    OBSERVATIONS_PATH = "storage/reports/learning_observations.jsonl"

    def __init__(self, trade_store: TradeStore | None = None, report_path: str = "reports/full_report.csv"):
        self.trade_store = trade_store or TradeStore()
        self.report_path = report_path

    def _load_report_rows(self) -> list[dict[str, Any]]:
        path = Path(self.report_path)
        if not path.exists():
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _exclude_bug_affected(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Filter out trades listed in storage/trades/learning_exclusions.json
        — symbols whose exit prices were recovered from real historical
        data after the original NaN bug forced their exit. These were
        NOT genuine strategy-driven exits (they were forced closes due
        to a since-fixed max_positions bug), so they must not silently
        skew win-rate / accuracy / effectiveness statistics."""
        exclusions_path = Path("storage/trades/learning_exclusions.json")
        if not exclusions_path.exists():
            return closed
        try:
            with open(exclusions_path) as f:
                excluded_symbols = set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return closed
        return [t for t in closed if t.get("symbol") not in excluded_symbols]

    def observe(self) -> dict[str, Any]:
        """Run one observation pass over closed trades and store the
        result (append-only, historical — never overwrites prior runs)."""
        closed = self.trade_store.get_closed_trades()
        closed = self._exclude_bug_affected(closed)
        report_rows = self._load_report_rows()
        report_by_symbol = {r.get("Stock"): r for r in report_rows}

        observation = {
            "timestamp": time.time(),
            "closed_trades_observed": len(closed),
            "sector_performance": self._sector_performance(closed, report_by_symbol),
            "regime_performance": self._regime_performance(closed, report_by_symbol),
            "news_effectiveness": self._news_effectiveness(closed, report_by_symbol),
            "fundamental_effectiveness": self._fundamental_effectiveness(closed, report_by_symbol),
            "technical_effectiveness": self._technical_effectiveness(closed, report_by_symbol),
            "rule_effectiveness": self._rule_effectiveness(closed, report_by_symbol),
            "redundant_rule_pairs": self._redundant_rule_pairs(report_rows),
            "threshold_sensitivity": self._threshold_sensitivity(closed, report_by_symbol),
            "buy_accuracy": self._accuracy(closed, "BUY"),
            "sell_accuracy": self._accuracy(closed, "SELL"),
            "best_worst_trade": self._best_worst_trade(closed),
            "exit_reason_breakdown": self._exit_reason_breakdown(closed),
        }

        self._append_observation(observation)

        return observation

    def _best_worst_trade(self, closed: list[dict]) -> dict[str, Any]:
        """Across ALL closed trades (BUY+SELL), the single best and
        worst by percent return — sign-aligned the same way _accuracy()
        does (a SELL winner always shows positive %)."""
        entries = []
        for t in closed:
            if t.get("status") != "CLOSED":
                continue
            rupee = self._pnl(t)
            pct_raw = self._pnl_percent(t)
            if math.isnan(rupee) or math.isnan(pct_raw):
                continue
            pct = abs(pct_raw) if rupee > 0 else -abs(pct_raw)
            entries.append((t.get("symbol"), t.get("direction"), round(pct, 2)))
        if not entries:
            return {}
        best = max(entries, key=lambda e: e[2])
        worst = min(entries, key=lambda e: e[2])
        return {
            "best": {"symbol": best[0], "direction": best[1], "pct": best[2]},
            "worst": {"symbol": worst[0], "direction": worst[1], "pct": worst[2]},
        }

    @staticmethod
    def _classify_exit_reason(reasons_text: str) -> str:
        text = (reasons_text or "").lower()
        if "target" in text:
            return "Target Hit"
        if "trailing" in text or "break-even" in text or "stop" in text:
            return "Trailing/Stop"
        if "holding period" in text or "time" in text:
            return "Time Exit"
        if "risk" in text or "emergency" in text or "reversal" in text or "volatility" in text or "news" in text:
            return "Risk Exit"
        return "Other"

    def _exit_reason_breakdown(self, closed: list[dict]) -> dict[str, int]:
        counts: Counter = Counter()
        for t in closed:
            if t.get("status") != "CLOSED":
                continue
            category = self._classify_exit_reason(t.get("reasons", ""))
            counts[category] += 1
        return dict(counts.most_common())

    def _accuracy(self, closed: list[dict], direction: str) -> dict[str, Any]:
        trades = [t for t in closed if t.get("direction") == direction and t.get("status") == "CLOSED"]
        if not trades:
            return {"trades": 0, "win_rate": None}

        pnls_rupees = [self._pnl(t) for t in trades]
        pnls_pct_raw = [self._pnl_percent(t) for t in trades]
        # Align percent SIGN with the rupee PnL's sign (the reliable
        # ground truth for win/loss) — for SELL trades, the stored
        # percent can follow a raw price-change convention (negative
        # when price falls, which is PROFITABLE for a short), which
        # reads as backwards/confusing. A winner should always show a
        # positive %, a loser always negative, regardless of storage.
        pnls_pct = [
            abs(pct) if rupee > 0 else -abs(pct)
            for rupee, pct in zip(pnls_rupees, pnls_pct_raw)
        ]
        # CRITICAL: a NaN pnl fails BOTH "> 0" and "<= 0" checks (a known
        # floating-point quirk), silently vanishing from win/loss
        # counting while still counting toward total trades — this was
        # the exact cause of wins+losses not summing to total trades.
        # Classify explicitly instead of relying on comparison operators.
        wins_idx, losses_idx, data_issue_idx = [], [], []
        for i, p in enumerate(pnls_rupees):
            if math.isnan(p):
                data_issue_idx.append(i)
            elif p > 0:
                wins_idx.append(i)
            else:
                losses_idx.append(i)

        winner_pcts = [pnls_pct[i] for i in wins_idx if not math.isnan(pnls_pct[i])]
        loser_pcts = [pnls_pct[i] for i in losses_idx if not math.isnan(pnls_pct[i])]

        gross_profit = sum(p for p in pnls_rupees if not math.isnan(p) and p > 0)
        gross_loss = abs(sum(p for p in pnls_rupees if not math.isnan(p) and p < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

        classifiable = len(wins_idx) + len(losses_idx)

        all_trade_rows = self.trade_store.get_all_trades()
        holding_days_list = [
            d for t in trades
            if (d := self._estimate_holding_days(t, all_trade_rows)) is not None
        ]
        winner_holding = [
            d for i in wins_idx
            if (d := self._estimate_holding_days(trades[i], all_trade_rows)) is not None
        ]
        loser_holding = [
            d for i in losses_idx
            if (d := self._estimate_holding_days(trades[i], all_trade_rows)) is not None
        ]

        return {
            "trades": len(trades),
            "win_rate": round(len(wins_idx) / classifiable * 100, 2) if classifiable else None,
            "wins": len(wins_idx),
            "losses": len(losses_idx),
            "data_quality_issues": len(data_issue_idx),
            "avg_winner_pct": round(sum(winner_pcts) / len(winner_pcts), 2) if winner_pcts else None,
            "avg_loser_pct": round(sum(loser_pcts) / len(loser_pcts), 2) if loser_pcts else None,
            "largest_winner_pct": round(max(winner_pcts), 2) if winner_pcts else None,
            "largest_loser_pct": round(min(loser_pcts), 2) if loser_pcts else None,
            "avg_holding_days": round(sum(holding_days_list) / len(holding_days_list), 1) if holding_days_list else None,
            "avg_winner_holding_days": round(sum(winner_holding) / len(winner_holding), 1) if winner_holding else None,
            "avg_loser_holding_days": round(sum(loser_holding) / len(loser_holding), 1) if loser_holding else None,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": profit_factor,
        }

    @staticmethod
    def _estimate_holding_days(closed_trade: dict, all_trade_rows: list[dict]) -> float | None:
        """OPEN and CLOSE rows aren't linked by a shared trade id (each
        save_trade() call gets its own auto-assigned id) — so holding
        days is ESTIMATED by matching this closed trade's symbol to the
        most recent prior OPEN row for that same symbol. Reasonable for
        this system since it doesn't hold multiple overlapping
        positions in the same symbol, but genuinely an estimate, not
        an exact link — documented here rather than presented as exact."""
        symbol = closed_trade.get("symbol")
        try:
            close_ts = float(closed_trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if not symbol or not close_ts:
            return None

        candidate_open_ts = None
        for row in all_trade_rows:
            if row.get("symbol") != symbol or row.get("action") != "OPEN":
                continue
            try:
                open_ts = float(row.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if open_ts < close_ts and (candidate_open_ts is None or open_ts > candidate_open_ts):
                candidate_open_ts = open_ts

        if candidate_open_ts is None:
            return None
        return round((close_ts - candidate_open_ts) / 86400, 2)

    @staticmethod
    def _wins_and_rate(pnls: list[float]) -> tuple[int, float | None]:
        """(wins, win_rate) with NaN-pnl entries excluded from BOTH —
        see _win_rate()'s BUGFIX note below for why this can't just be
        `sum(1 for p in pnls if p > 0)` / `len(pnls)` directly. `trades`
        (the total, NaN included) is reported separately by callers,
        matching _accuracy()'s existing trades-vs-classifiable split."""
        valid = [p for p in pnls if not math.isnan(p)]
        wins = sum(1 for p in valid if p > 0)
        win_rate = round(wins / len(valid) * 100, 2) if valid else None
        return wins, win_rate

    def _sector_performance(self, closed, report_by_symbol) -> dict[str, Any]:
        # BUGFIX (2026-09-18, Phase 3 — see BUG_AUDIT_2026-09-18.md item
        # #13): was inlining "sum(1 for p in pnls if p > 0) / len(pnls)"
        # directly — a NaN pnl silently counted in the denominator
        # without ever counting as a win, deflating win_rate. Now uses
        # _wins_and_rate(), which excludes NaN entries the same way
        # _accuracy() does.
        by_sector: dict[str, list[float]] = {}
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            sector = r.get("Sector") if r else None
            if not sector:
                continue
            by_sector.setdefault(sector, []).append(self._pnl(t))
        result = {}
        for sector, pnls in by_sector.items():
            wins, win_rate = self._wins_and_rate(pnls)
            result[sector] = {"trades": len(pnls), "wins": wins, "win_rate": win_rate}
        return result

    def _regime_performance(self, closed, report_by_symbol) -> dict[str, Any]:
        by_regime: dict[str, list[float]] = {}
        for t in closed:
            regime = t.get("regime")
            if not regime:
                continue
            by_regime.setdefault(regime, []).append(self._pnl(t))
        # BUGFIX 2026-09-18 (see _sector_performance() above): same
        # NaN-exclusion fix, same reason.
        result = {}
        for regime, pnls in by_regime.items():
            wins, win_rate = self._wins_and_rate(pnls)
            result[regime] = {"trades": len(pnls), "wins": wins, "win_rate": win_rate}
        return result

    def _news_effectiveness(self, closed, report_by_symbol) -> dict[str, Any]:
        with_news, without_news = [], []
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            if not r:
                continue
            # "No news" is represented as NewsScore == 0.0 in the current
            # architecture (buy_scoring.py hardcodes 0.0 when has_news is
            # False) — NOT the old neutral-50 convention this used to
            # check for. Verified against production data: NewsScore only
            # ever takes 100.0 (positive) or 0.0 (no news) in practice.
            raw = str(r.get("NewsScore") or "").strip()
            has_news = raw not in ("", "0", "0.0")
            (with_news if has_news else without_news).append(self._pnl(t))
        return {
            "with_news_win_rate": self._win_rate(with_news),
            "without_news_win_rate": self._win_rate(without_news),
            "with_news_trades": len(with_news),
            "without_news_trades": len(without_news),
        }

    def _fundamental_effectiveness(self, closed, report_by_symbol) -> dict[str, Any]:
        strong, weak = [], []
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            if not r:
                continue
            try:
                fscore = float(r.get("FundamentalScore") or 0)
            except ValueError:
                continue
            (strong if fscore >= 60 else weak).append(self._pnl(t))
        return {
            "strong_fundamentals_win_rate": self._win_rate(strong),
            "weak_fundamentals_win_rate": self._win_rate(weak),
        }

    def _technical_effectiveness(self, closed, report_by_symbol) -> dict[str, Any]:
        high_tech, low_tech = [], []
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            if not r:
                continue
            key = "BuyTier2Score" if t.get("direction") == "BUY" else "SellTier2Score"
            try:
                tscore = float(r.get(key) or 0)
            except ValueError:
                continue
            (high_tech if tscore >= 60 else low_tech).append(self._pnl(t))
        return {
            "high_technical_win_rate": self._win_rate(high_tech),
            "low_technical_win_rate": self._win_rate(low_tech),
            "high_technical_trades": len(high_tech),
            "low_technical_trades": len(low_tech),
        }

    def _rule_effectiveness(self, closed, report_by_symbol) -> dict[str, Any]:
        """Per-INDIVIDUAL-rule win-rate correlation — e.g. does the
        specific 'rsi_oversold' rule actually predict wins, not just
        the aggregate technical score? Uses the full ~39-rule checklist
        persisted in BuyTechnicalChecks/SellTechnicalChecks (see
        execution/scanner.py's buy_technical_checks/sell_technical_checks
        diagnostics). Fills the roadmap's "Rule effectiveness" item,
        which the 5 dimension-specific methods above only covered
        implicitly at an aggregate level."""
        rule_pnls: dict[str, dict[str, list[float]]] = {}
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            if not r:
                continue
            direction = t.get("direction")
            key = "BuyTechnicalChecks" if direction == "BUY" else "SellTechnicalChecks"
            raw = r.get(key)
            if not raw:
                continue
            try:
                checks: dict[str, bool] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            pnl = self._pnl(t)
            for rule_name, passed in checks.items():
                bucket = rule_pnls.setdefault(rule_name, {"passed": [], "failed": []})
                (bucket["passed"] if passed else bucket["failed"]).append(pnl)

        result: dict[str, Any] = {}
        for rule_name, buckets in sorted(rule_pnls.items()):
            passed_wr = self._win_rate(buckets["passed"])
            failed_wr = self._win_rate(buckets["failed"])
            differential = (
                round(passed_wr - failed_wr, 2)
                if passed_wr is not None and failed_wr is not None
                else None
            )
            result[rule_name] = {
                "win_rate_when_passed": passed_wr,
                "win_rate_when_failed": failed_wr,
                "sample_passed": len(buckets["passed"]),
                "sample_failed": len(buckets["failed"]),
                "differential": differential,
            }
        return result

    def _redundant_rule_pairs(self, report_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flags pairs of rules that almost always agree (both pass or
        both fail together) across ALL scanned candidates — not just
        closed trades, since redundancy is about how rules relate to
        EACH OTHER, not to outcomes, so using every scan gives far more
        signal. Two rules that agree >=90% of the time contribute
        little marginal information beyond one another."""
        AGREEMENT_THRESHOLD = 0.90
        MIN_SAMPLE = 20

        per_rule: dict[str, list[bool]] = {}
        for r in report_rows:
            raw = r.get("BuyTechnicalChecks")
            if not raw:
                continue
            try:
                checks: dict[str, bool] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for rule_name, passed in checks.items():
                per_rule.setdefault(rule_name, []).append(bool(passed))

        # Only compare rules observed the same number of times (aligned rows)
        rule_names = [name for name, vals in per_rule.items() if len(vals) >= MIN_SAMPLE]
        redundant_pairs = []
        for i, name_a in enumerate(rule_names):
            vals_a = per_rule[name_a]
            for name_b in rule_names[i + 1:]:
                vals_b = per_rule[name_b]
                n = min(len(vals_a), len(vals_b))
                if n < MIN_SAMPLE:
                    continue
                agree = sum(1 for j in range(n) if vals_a[j] == vals_b[j])
                agreement_rate = agree / n
                if agreement_rate >= AGREEMENT_THRESHOLD:
                    redundant_pairs.append({
                        "rule_a": name_a,
                        "rule_b": name_b,
                        "agreement_rate": round(agreement_rate * 100, 2),
                        "sample_size": n,
                    })
        return sorted(redundant_pairs, key=lambda p: p["agreement_rate"], reverse=True)

    def _threshold_sensitivity(self, closed, report_by_symbol) -> dict[str, Any]:
        """Buckets closed trades by how much margin they passed the
        qualify threshold by (score - threshold), and reports win-rate
        per margin band. If trades that barely passed (small margin)
        win much less often than comfortable passes, that's concrete
        evidence the threshold should be raised — not just a generic
        'consider adjusting' note."""
        bands = {"0-5": [], "5-10": [], "10-20": [], "20+": []}
        for t in closed:
            r = report_by_symbol.get(t.get("symbol"))
            if not r:
                continue
            direction = t.get("direction")
            score_key = "BuyOverallScore" if direction == "BUY" else "SellOverallScore"
            threshold_key = "BuyThreshold" if direction == "BUY" else "SellThreshold"
            try:
                score = float(r.get(score_key) or 0)
                threshold = float(r.get(threshold_key) or 0)
            except ValueError:
                continue
            margin = score - threshold
            if margin < 0:
                continue  # shouldn't happen (wouldn't have traded), skip defensively
            pnl = self._pnl(t)
            if margin < 5:
                bands["0-5"].append(pnl)
            elif margin < 10:
                bands["5-10"].append(pnl)
            elif margin < 20:
                bands["10-20"].append(pnl)
            else:
                bands["20+"].append(pnl)

        return {band: {"trades": len(pnls), "win_rate": self._win_rate(pnls)} for band, pnls in bands.items()}

    @staticmethod
    def _pnl(trade: dict) -> float:
        try:
            return float(trade.get("realized_pnl") or 0)
        except ValueError:
            return 0.0

    @staticmethod
    def _pnl_percent(trade: dict) -> float:
        try:
            return float(trade.get("realized_pnl_percent") or 0)
        except ValueError:
            return 0.0

    @staticmethod
    def _win_rate(pnls: list[float]) -> float | None:
        # BUGFIX (2026-09-18, Phase 3 — see BUG_AUDIT_2026-09-18.md item
        # #13): a NaN pnl fails BOTH "p > 0" and any "<= 0"/"< 0" check
        # (a known floating-point quirk) — _accuracy() above already
        # documents and correctly excludes NaN trades from its win_rate
        # denominator for exactly this reason. This shared helper (used
        # by _news_effectiveness/_fundamental_effectiveness/
        # _technical_effectiveness/_rule_effectiveness/
        # _threshold_sensitivity) did NOT do the same: a NaN entry still
        # counted in `len(pnls)` while never counting as a win, silently
        # DEFLATING every one of those win rates. Filter NaN out first,
        # exactly like _accuracy() does, so a NaN-pnl trade is excluded
        # from the win-rate calculation entirely instead of being
        # counted as a guaranteed loss.
        valid = [p for p in pnls if not math.isnan(p)]
        if not valid:
            return None
        return round(sum(1 for p in valid if p > 0) / len(valid) * 100, 2)

    def _append_observation(self, observation: dict) -> None:
        path = Path(self.OBSERVATIONS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(observation, default=str) + "\n")

    def get_history(self) -> list[dict[str, Any]]:
        path = Path(self.OBSERVATIONS_PATH)
        if not path.exists():
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    engine = LearningEngine()
    obs = engine.observe()
    print(json.dumps(obs, indent=2, default=str))
