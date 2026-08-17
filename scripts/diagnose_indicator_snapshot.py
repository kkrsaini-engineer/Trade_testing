"""
DIAGNOSTIC — Real, engine-computed indicator snapshot for ONE symbol.

Runs the EXACT SAME live pipeline the bot itself uses — data/market_data.py's
MarketDataProvider (yfinance fetch) -> features/feature_engineering.py's
FeatureEngineeringEngine (the real indicator math, no reimplementation
here) -> market/market_regime.py's MarketRegimeEngine (market_regime,
needed for TrendState) — and prints the LATEST candle's raw indicator
values plus a plain-language read on each one.

Purpose: an accuracy check against a broker/charting app for a SINGLE
symbol/timeframe, using the bot's own real, unmodified code path — not
a hand-derived or external-website approximation of what the engine
"should" compute. This diagnostic reads and prints only; it does not
call any BUY/SELL/scoring logic.

Usage:
    python scripts/diagnose_indicator_snapshot.py --symbol HDFCBANK.NS --interval 1d --period 1y
    python scripts/diagnose_indicator_snapshot.py --symbol HDFCBANK.NS --interval 60m --period 1mo
"""

from __future__ import annotations

import argparse

from data.market_data import MarketDataProvider
from features.feature_engineering import FeatureEngineeringEngine
from market.market_regime import MarketRegimeEngine


def _bollinger_position(close: float, upper: float, middle: float, lower: float) -> str:
    if close > upper:
        return f"ABOVE the upper band ({upper:.2f}) — extended to the upside."
    if close > middle:
        return f"between the MIDDLE band ({middle:.2f}) and the UPPER band ({upper:.2f})."
    if close > lower:
        return f"between the LOWER band ({lower:.2f}) and the MIDDLE band ({middle:.2f})."
    return f"BELOW the lower band ({lower:.2f}) — extended to the downside."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True, help="e.g. HDFCBANK.NS")
    parser.add_argument("--interval", default="1d", help="e.g. 1d, 60m, 15m")
    parser.add_argument(
        "--period", default="1y",
        help="How far back to fetch — e.g. 1y for interval=1d, 1mo for interval=60m",
    )
    args = parser.parse_args()

    print(f"Fetching {args.symbol} @ interval={args.interval} period={args.period} ...")
    market_data = MarketDataProvider().fetch(symbol=args.symbol, interval=args.interval, period=args.period)
    print(f"Rows fetched: {len(market_data)}")

    print("Computing real technical features (same engine the live scan uses) ...")
    features = FeatureEngineeringEngine().generate(market_data)
    features = MarketRegimeEngine().evaluate(features)

    latest = features.iloc[-1]
    timestamp = latest.get("timestamp", "unknown")

    close = float(latest["close"])
    ema_20 = float(latest["ema_20"])
    rsi_14 = float(latest["rsi_14"])
    adx_14 = float(latest["adx_14"])
    bb_upper = float(latest["bb_upper"])
    bb_middle = float(latest["bb_middle"])
    bb_lower = float(latest["bb_lower"])
    market_regime = str(latest.get("market_regime", "UNKNOWN"))

    print("\n" + "=" * 60)
    print(f"{args.symbol} — latest candle: {timestamp}")
    print("=" * 60)

    print(f"\nClose price: {close:.2f}")

    ema_side = "ABOVE" if close > ema_20 else "BELOW" if close < ema_20 else "EXACTLY AT"
    print(f"20-EMA: {ema_20:.2f}  ->  candle closed {ema_side} the 20-EMA")

    if rsi_14 >= 70:
        rsi_read = "overbought zone (>=70)"
    elif rsi_14 >= 55:
        rsi_read = "bullish momentum zone (55-70)"
    elif rsi_14 <= 30:
        rsi_read = "oversold zone (<=30)"
    elif rsi_14 <= 45:
        rsi_read = "bearish momentum zone (30-45)"
    else:
        rsi_read = "neutral zone (45-55)"
    print(f"RSI(14): {rsi_14:.2f}  ->  {rsi_read}")

    if adx_14 >= 25:
        adx_read = "TRENDING regime (>=25) — a directional move is underway"
    elif adx_14 < 20:
        adx_read = "RANGE_BOUND regime (<20) — no strong directional move"
    else:
        adx_read = "BASELINE regime (20-25) — borderline"
    print(f"ADX(14): {adx_14:.2f}  ->  {adx_read}")

    bb_read = _bollinger_position(close, bb_upper, bb_middle, bb_lower)
    print(f"Bollinger Bands: upper={bb_upper:.2f} middle={bb_middle:.2f} lower={bb_lower:.2f}")
    print(f"  -> candle is {bb_read}")

    print(f"\nMarketRegimeEngine's own market_regime label: {market_regime}")

    print("\n" + "=" * 60)
    print("These are the EXACT numbers strategy/buy_strategy.py and")
    print("strategy/sell_strategy.py's real checks would see for this")
    print("candle — same engine, same data source, no manual re-derivation.")
    print("=" * 60)


if __name__ == "__main__":
    main()
