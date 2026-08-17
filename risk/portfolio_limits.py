"""
Portfolio-level circuit-breaker constants — single source of truth for
drawdown-based allocation throttling and daily-loss trading limits.

Phase 21/22 background (see PHASE21_NOTES.md / PHASE22_NOTES.md): before
this module existed, drawdown and daily-loss thresholds were each
hardcoded independently in THREE places (risk/portfolio_rules.py,
risk/risk_manager.py, decision/validation_engine.py) with values that had
already drifted out of sync (daily-loss: 3% / 5.0 / 3%, using two
different unit conventions). All three now import from here.

This module is intentionally dependency-free (no imports from elsewhere
in this codebase) so it can be safely imported from both risk/ and
portfolio/ without creating a circular import.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# DRAWDOWN THROTTLING — graduated allocation scale-down by portfolio
# drawdown severity (peak-to-trough, on mark-to-market portfolio equity).
# Replaces the old binary >20% reject / 15-20% warning-only gate with 4
# bands. The HALT band fully blocks new trades (same effect as the old
# hard reject, just at a lower, earlier threshold); the others scale
# allocation_allowed down instead of an all-or-nothing cutoff.
# ---------------------------------------------------------------------------

DRAWDOWN_BAND_NORMAL = 0.05            # 0-5%: no throttling
DRAWDOWN_BAND_REDUCED = 0.10           # 5-10%: reduced
DRAWDOWN_BAND_HEAVILY_REDUCED = 0.15   # 10-15%: heavily reduced
# >15% -> HALT (reject new trades entirely)

DRAWDOWN_MULTIPLIER_NORMAL = 1.0
DRAWDOWN_MULTIPLIER_REDUCED = 0.75
DRAWDOWN_MULTIPLIER_HEAVILY_REDUCED = 0.50
DRAWDOWN_MULTIPLIER_HALT = 0.0


def drawdown_band_label(portfolio_drawdown: float) -> str:
    """Human-readable band name for a given portfolio drawdown (0.0-1.0
    fraction). Used for warnings/rejection reasons and pass/fail checks."""

    if portfolio_drawdown is None:
        return "normal"

    if portfolio_drawdown > DRAWDOWN_BAND_HEAVILY_REDUCED:
        return "halt"

    if portfolio_drawdown > DRAWDOWN_BAND_REDUCED:
        return "heavily_reduced"

    if portfolio_drawdown > DRAWDOWN_BAND_NORMAL:
        return "reduced"

    return "normal"


def drawdown_multiplier(portfolio_drawdown: float) -> float:
    """Allocation multiplier for a given portfolio drawdown. HALT band
    (>15%) returns 0.0 — callers must still separately reject the trade;
    this multiplier alone does not block anything."""

    band = drawdown_band_label(portfolio_drawdown)

    return {
        "normal": DRAWDOWN_MULTIPLIER_NORMAL,
        "reduced": DRAWDOWN_MULTIPLIER_REDUCED,
        "heavily_reduced": DRAWDOWN_MULTIPLIER_HEAVILY_REDUCED,
        "halt": DRAWDOWN_MULTIPLIER_HALT,
    }[band]


# ---------------------------------------------------------------------------
# DAILY-LOSS TRADING LIMITS — graduated 4-stage hierarchy, on the same
# mark-to-market portfolio equity basis as drawdown above. Single source
# of truth for the portfolio-level new-trade gate (portfolio_rules.py),
# the validation-engine risk check (validation_engine.py), and the
# per-signal risk override (risk_manager.py).
# ---------------------------------------------------------------------------

DAILY_LOSS_WARNING = 0.02          # warn only, no restriction
DAILY_LOSS_RISK_REDUCTION = 0.03   # reduce allocation (like drawdown bands)
DAILY_LOSS_TRADING_HALT = 0.04     # reject new trades
DAILY_LOSS_EMERGENCY = 0.05        # hard override: total_risk=100, safe=False

DAILY_LOSS_MULTIPLIER_NORMAL = 1.0
DAILY_LOSS_MULTIPLIER_RISK_REDUCTION = 0.50


def daily_loss_stage(daily_loss: float) -> str:
    """Stage name for a given daily_loss (0.0-1.0 fraction, 0.0 if the
    portfolio is flat/up for the day — this is a LOSS magnitude, never
    negative by construction of the caller)."""

    if daily_loss is None:
        return "normal"

    if daily_loss >= DAILY_LOSS_EMERGENCY:
        return "emergency"

    if daily_loss >= DAILY_LOSS_TRADING_HALT:
        return "trading_halt"

    if daily_loss >= DAILY_LOSS_RISK_REDUCTION:
        return "risk_reduction"

    if daily_loss >= DAILY_LOSS_WARNING:
        return "warning"

    return "normal"


def daily_loss_multiplier(daily_loss: float) -> float:
    """Allocation multiplier applied at the risk_reduction stage only —
    trading_halt/emergency are rejected outright by the caller, not scaled."""

    stage = daily_loss_stage(daily_loss)

    if stage == "risk_reduction":

        return DAILY_LOSS_MULTIPLIER_RISK_REDUCTION

    return DAILY_LOSS_MULTIPLIER_NORMAL


def is_emergency_condition(portfolio_drawdown: float, daily_loss: float) -> bool:
    """True when either graduated system has reached its most severe
    stage — the real trigger source for `emergency_stop` (Phase 22; see
    PHASE22_NOTES.md point 13). Portfolio-level "extreme loss condition",
    not a market-risk factor."""

    return (
        drawdown_band_label(portfolio_drawdown) == "halt"
        or daily_loss_stage(daily_loss) == "emergency"
    )
