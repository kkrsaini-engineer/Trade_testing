"""
Shared stop-loss / target-price formula.

Single canonical implementation of the ATR-based stop/target model,
previously hand-duplicated in THREE places that had already started to
drift apart with no test able to catch it (see PHASE20_NOTES.md):

  - execution/scanner.py's `_compute_stop_loss_targets()` — the static
    stop/target shown in reports and the Holding Status notification.
  - risk/exit_strategy.py's `ExitStrategyEngine.evaluate()` — the INITIAL
    stop-loss and profit-target levels the live exit engine starts from
    (before trailing/break-even adjustments, which are a genuinely
    separate concept and stay local to that file).
  - risk/position_sizing.py's `stop_distance` calculation — the risk-
    budget input used to size a position's quantity.

All three now import from here. Change the formula ONCE, every caller
moves together.

Design note — the "Risk:Reward" this implies is a FIXED CONSTANT
-------------------------------------------------------------------
Because both the stop and each target are a fixed multiple of the SAME
ATR-based distance, the ratio between them (target distance / stop
distance) can NEVER vary symbol-to-symbol under this formula shape —
it's always exactly TARGET1_R_MULTIPLE / TARGET2_R_MULTIPLE below,
regardless of what the multiplier constants are set to. Earlier phases
displayed this as a per-trade "computed" Risk:Reward number, which
implied a discriminating metric that doesn't actually exist. Callers
should present TARGET1_R_MULTIPLE/TARGET2_R_MULTIPLE as what they are —
the model's fixed R-multiple design (e.g. "Target 1 = 1R") — not as a
per-symbol computed ratio.
"""

from __future__ import annotations

ATR_STOP = 2.0

PARTIAL_TARGET = 2.0

FINAL_TARGET = 3.5

# Floor the stop distance at 1% of price. For a very low-ATR (very calm
# / potentially thin) stock, a pure ATR-based stop can collapse to an
# uselessly tight distance that ordinary noise would breach. Applied
# EVERYWHERE now — previously only risk/position_sizing.py had this
# floor; execution/scanner.py's displayed stop and
# risk/exit_strategy.py's live exit stop did not, meaning position
# sizing was assuming a WIDER (safer, more conservative) stop distance
# than what would actually trigger a real exit for these stocks. Now
# consistent end to end.
MIN_STOP_PERCENT = 0.01

# The model's fixed R-multiples — see the module docstring above. These
# are constants BY CONSTRUCTION (target distance is always this multiple
# of the stop distance), not something computed per trade.
TARGET1_R_MULTIPLE = round(PARTIAL_TARGET / ATR_STOP, 2)  # 1.0
TARGET2_R_MULTIPLE = round(FINAL_TARGET / ATR_STOP, 2)    # 1.75


def stop_distance(close_price: float, atr: float) -> float:
    """The ATR-based stop distance (always >= 0), floored at
    MIN_STOP_PERCENT of price."""
    return max(atr * ATR_STOP, close_price * MIN_STOP_PERCENT)


def compute_stop_loss_targets(
    direction: str, close_price: float, atr: float
) -> tuple[float, float, float]:
    """Returns (stop_loss, target1, target2) for `direction` (BUY/SELL)
    starting from `close_price` (or entry_price — same formula, either
    is just "the reference price this position/scan is measured from").

    target1/target2 scale off the SAME (possibly floor-adjusted) stop
    distance as the stop itself, at the fixed TARGET1_R_MULTIPLE /
    TARGET2_R_MULTIPLE ratios — so "target1 is always exactly 1R,
    target2 is always exactly 1.75R" holds as a true invariant, even
    when the MIN_STOP_PERCENT floor is what's actually driving the
    stop distance (not raw ATR).

    Returns (0.0, 0.0, 0.0) if atr or close_price is falsy — matches the
    previous per-caller behavior (no fabricated levels from no data).
    """
    if not (atr and close_price):
        return 0.0, 0.0, 0.0

    dist = stop_distance(close_price, atr)
    partial_dist = dist * TARGET1_R_MULTIPLE
    final_dist = dist * TARGET2_R_MULTIPLE

    if direction == "SELL":
        stop_loss = close_price + dist
        target1 = close_price - partial_dist
        target2 = close_price - final_dist
    else:
        stop_loss = close_price - dist
        target1 = close_price + partial_dist
        target2 = close_price + final_dist

    return round(stop_loss, 2), round(target1, 2), round(target2, 2)
