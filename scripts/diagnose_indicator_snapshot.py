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


def _side(value: float, reference: float) -> str:
    if value > reference:
        return "ABOVE"
    if value < reference:
        return "BELOW"
    return "EXACTLY AT"


def _macd_read(macd: float, macd_signal: float, macd_histogram: float) -> str:
    cross = "MACD ABOVE signal (bullish)" if macd > macd_signal else "MACD BELOW signal (bearish)"
    momentum = "histogram widening/positive" if macd_histogram > 0 else "histogram negative/narrowing"
    return f"{cross}, {momentum}"


def _stochastic_read(stoch_k: float, stoch_d: float) -> str:
    if stoch_k >= 80:
        zone = "overbought zone (>=80)"
    elif stoch_k <= 20:
        zone = "oversold zone (<=20)"
    else:
        zone = "neutral zone (20-80)"
    cross = "%K above %D" if stoch_k > stoch_d else "%K below %D"
    return f"{zone}, {cross}"


def _williams_r_read(value: float) -> str:
    if value >= -20:
        return "overbought zone (>=-20)"
    if value <= -80:
        return "oversold zone (<=-80)"
    return "neutral zone (-80 to -20)"


def _cci_read(value: float) -> str:
    if value > 100:
        return "overbought zone (>100)"
    if value < -100:
        return "oversold zone (<-100)"
    return "neutral zone (-100 to 100)"


def _roc_read(value: float) -> str:
    return "positive momentum (price higher than 12 bars ago)" if value > 0 else \
        "negative momentum (price lower than 12 bars ago)"


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
    ema_50 = float(latest["ema_50"])
    ema_200 = float(latest["ema_200"])
    sma_20 = float(latest["sma_20"])
    sma_50 = float(latest["sma_50"])
    sma_200 = float(latest["sma_200"])
    rsi_14 = float(latest["rsi_14"])
    adx_14 = float(latest["adx_14"])
    bb_upper = float(latest["bb_upper"])
    bb_middle = float(latest["bb_middle"])
    bb_lower = float(latest["bb_lower"])
    macd = float(latest["macd"])
    macd_signal = float(latest["macd_signal"])
    macd_histogram = float(latest["macd_histogram"])
    atr_14 = float(latest["atr_14"])
    vwap = float(latest["vwap"])
    stoch_k = float(latest["stoch_k"])
    stoch_d = float(latest["stoch_d"])
    williams_r_14 = float(latest["williams_r_14"])
    cci_20 = float(latest["cci_20"])
    roc_12 = float(latest["roc_12"])
    supertrend = bool(latest["supertrend"])
    market_regime = str(latest.get("market_regime", "UNKNOWN"))

    print("\n" + "=" * 60)
    print(f"{args.symbol} — latest candle: {timestamp}")
    print("=" * 60)

    print(f"\nClose price: {close:.2f}")

    print(f"20-EMA: {ema_20:.2f}  ->  candle closed {_side(close, ema_20)} the 20-EMA")
    print(f"50-EMA: {ema_50:.2f}  ->  candle closed {_side(close, ema_50)} the 50-EMA")
    print(f"200-EMA: {ema_200:.2f}  ->  candle closed {_side(close, ema_200)} the 200-EMA")
    print(f"20-SMA: {sma_20:.2f}  ->  candle closed {_side(close, sma_20)} the 20-SMA")
    print(f"50-SMA: {sma_50:.2f}  ->  candle closed {_side(close, sma_50)} the 50-SMA")
    print(f"200-SMA: {sma_200:.2f}  ->  candle closed {_side(close, sma_200)} the 200-SMA")

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

    print(f"\nMACD: macd={macd:.2f} signal={macd_signal:.2f} histogram={macd_histogram:.2f}")
    print(f"  -> {_macd_read(macd, macd_signal, macd_histogram)}")

    print(f"ATR(14): {atr_14:.2f}  ->  average true range in price points (not a zone, a volatility size)")

    print(f"VWAP: {vwap:.2f}  ->  candle closed {_side(close, vwap)} the VWAP")

    print(f"Stochastic: %K={stoch_k:.2f} %D={stoch_d:.2f}")
    print(f"  -> {_stochastic_read(stoch_k, stoch_d)}")

    print(f"Williams %R(14): {williams_r_14:.2f}  ->  {_williams_r_read(williams_r_14)}")

    print(f"CCI(20): {cci_20:.2f}  ->  {_cci_read(cci_20)}")

    print(f"ROC(12): {roc_12:.2f}%  ->  {_roc_read(roc_12)}")

    supertrend_read = "BULLISH (price above Supertrend line)" if supertrend else "BEARISH (price below Supertrend line)"
    print(f"Supertrend(10,3): {supertrend_read}")

    print(f"\nMarketRegimeEngine's own market_regime label: {market_regime}")

    print("\n" + "=" * 60)
    print("These are the EXACT numbers strategy/buy_strategy.py and")
    print("strategy/sell_strategy.py's real checks would see for this")
    print("candle — same engine, same data source, no manual re-derivation.")
    print("=" * 60)


if __name__ == "__main__":
    main()
