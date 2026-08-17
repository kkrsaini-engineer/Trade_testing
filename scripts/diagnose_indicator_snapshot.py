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
import math

from data.market_data import MarketDataProvider
from features.feature_engineering import FeatureEngineeringEngine
from market.market_regime import MarketRegimeEngine


def _fmt(value: float) -> str:
    """NaN-safe number formatting — some indicators (e.g. 200-period
    ones on a short fetch window, or chikou_span at the very latest
    candle) are legitimately unavailable rather than wrong."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A (not enough data for this window)"
    return f"{value:.2f}"


def _bollinger_position(close: float, upper: float, middle: float, lower: float) -> str:
    if any(math.isnan(v) for v in (close, upper, middle, lower)):
        return "N/A (not enough data for this window)."
    if close > upper:
        return f"ABOVE the upper band ({upper:.2f}) — extended to the upside."
    if close > middle:
        return f"between the MIDDLE band ({middle:.2f}) and the UPPER band ({upper:.2f})."
    if close > lower:
        return f"between the LOWER band ({lower:.2f}) and the MIDDLE band ({middle:.2f})."
    return f"BELOW the lower band ({lower:.2f}) — extended to the downside."


def _band_position(close: float, upper: float, lower: float, upper_label: str, lower_label: str) -> str:
    """Generic upper/lower band read — used for Donchian and Keltner
    channels, which (unlike Bollinger) don't have a distinct "extended
    beyond the band" case worth calling out separately in practice."""
    if any(math.isnan(v) for v in (close, upper, lower)):
        return "N/A (not enough data for this window)."
    if close > upper:
        return f"ABOVE the {upper_label} ({upper:.2f})."
    if close < lower:
        return f"BELOW the {lower_label} ({lower:.2f})."
    return f"between the {lower_label} ({lower:.2f}) and the {upper_label} ({upper:.2f})."


def _side(value: float, reference: float) -> str:
    if math.isnan(value) or math.isnan(reference):
        return "N/A (not enough data for this window)"
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


def _obv_read(value: float) -> str:
    if math.isnan(value):
        return "N/A (not enough data for this window)"
    return "net buying pressure (positive, last 20 bars)" if value > 0 else \
        "net selling pressure (negative, last 20 bars)"


def _cmf_read(value: float) -> str:
    if math.isnan(value):
        return "N/A (not enough data for this window)"
    if value > 0.1:
        return "strong buying pressure (CMF > 0.1)"
    if value < -0.1:
        return "strong selling pressure (CMF < -0.1)"
    return "neutral (CMF between -0.1 and 0.1)"


def _mfi_read(value: float) -> str:
    if math.isnan(value):
        return "N/A (not enough data for this window)"
    if value >= 80:
        return "overbought zone (>=80)"
    if value <= 20:
        return "oversold zone (<=20)"
    return "neutral zone (20-80)"


def _pivot_read(close: float, pivot: float, resistance_1: float, support_1: float) -> str:
    if any(math.isnan(v) for v in (close, pivot, resistance_1, support_1)):
        return "N/A (not enough data for this window)."
    if close > resistance_1:
        return f"ABOVE R1 ({resistance_1:.2f}) — broke above the first resistance."
    if close > pivot:
        return f"between Pivot ({pivot:.2f}) and R1 ({resistance_1:.2f})."
    if close > support_1:
        return f"between S1 ({support_1:.2f}) and Pivot ({pivot:.2f})."
    return f"BELOW S1 ({support_1:.2f}) — broke below the first support."


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
    obv = float(latest["obv"])
    cmf_20 = float(latest["cmf_20"])
    mfi_14 = float(latest["mfi_14"])
    volume_sma_20 = float(latest["volume_sma_20"])
    volume = float(latest["volume"])
    tenkan_sen = float(latest["tenkan_sen"])
    kijun_sen = float(latest["kijun_sen"])
    senkou_span_a = float(latest["senkou_span_a"])
    senkou_span_b = float(latest["senkou_span_b"])
    chikou_span = float(latest["chikou_span"])
    cloud_trend = str(latest.get("cloud_trend", "UNKNOWN"))
    pivot = float(latest["pivot"])
    resistance_1 = float(latest["resistance_1"])
    support_1 = float(latest["support_1"])
    dc_upper = float(latest["dc_upper"])
    dc_middle = float(latest["dc_middle"])
    dc_lower = float(latest["dc_lower"])
    kc_upper = float(latest["kc_upper"])
    kc_middle = float(latest["kc_middle"])
    kc_lower = float(latest["kc_lower"])
    market_regime = str(latest.get("market_regime", "UNKNOWN"))

    print("\n" + "=" * 60)
    print(f"{args.symbol} — latest candle: {timestamp}")
    print("=" * 60)

    print(f"\nClose price: {close:.2f}")

    print(f"20-EMA: {_fmt(ema_20)}  ->  candle closed {_side(close, ema_20)} the 20-EMA")
    print(f"50-EMA: {_fmt(ema_50)}  ->  candle closed {_side(close, ema_50)} the 50-EMA")
    print(f"200-EMA: {_fmt(ema_200)}  ->  candle closed {_side(close, ema_200)} the 200-EMA")
    print(f"20-SMA: {_fmt(sma_20)}  ->  candle closed {_side(close, sma_20)} the 20-SMA")
    print(f"50-SMA: {_fmt(sma_50)}  ->  candle closed {_side(close, sma_50)} the 50-SMA")
    print(f"200-SMA: {_fmt(sma_200)}  ->  candle closed {_side(close, sma_200)} the 200-SMA")

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
    print(f"Bollinger Bands: upper={_fmt(bb_upper)} middle={_fmt(bb_middle)} lower={_fmt(bb_lower)}")
    print(f"  -> candle is {bb_read}")

    print(f"\nMACD: macd={macd:.2f} signal={macd_signal:.2f} histogram={macd_histogram:.2f}")
    print(f"  -> {_macd_read(macd, macd_signal, macd_histogram)}")

    print(f"ATR(14): {atr_14:.2f}  ->  average true range in price points (not a zone, a volatility size)")

    print(f"VWAP: {vwap:.2f}  ->  candle closed {_side(close, vwap)} the VWAP")
    print("  -> NOTE: this VWAP is cumulative from the start of the fetched")
    print("     window (not reset each trading session), so it will NOT match")
    print("     a broker's intraday session VWAP on anything but the first day.")

    print(f"Stochastic: %K={stoch_k:.2f} %D={stoch_d:.2f}")
    print(f"  -> {_stochastic_read(stoch_k, stoch_d)}")

    print(f"Williams %R(14): {williams_r_14:.2f}  ->  {_williams_r_read(williams_r_14)}")

    print(f"CCI(20): {cci_20:.2f}  ->  {_cci_read(cci_20)}")

    print(f"ROC(12): {roc_12:.2f}%  ->  {_roc_read(roc_12)}")

    supertrend_read = "BULLISH (price above Supertrend line)" if supertrend else "BEARISH (price below Supertrend line)"
    print(f"Supertrend(10,3): {supertrend_read}")
    print("  -> NOTE: only the direction is stored by the engine, not the")
    print("     numeric Supertrend line value, so compare direction only,")
    print("     not the exact price level shown on a broker's chart.")

    print(f"\nVolume: {volume:,.0f}  vs  20-bar average: {_fmt(volume_sma_20)}")
    obv_line = "" if math.isnan(obv) else f"{obv:,.0f}  ->  "
    print(f"OBV (20-bar signed volume sum): {obv_line}{_obv_read(obv)}")

    print(f"CMF(20): {_fmt(cmf_20)}  ->  {_cmf_read(cmf_20)}")

    print(f"MFI(14): {_fmt(mfi_14)}  ->  {_mfi_read(mfi_14)}")

    print(f"\nIchimoku: Tenkan-sen={_fmt(tenkan_sen)}  Kijun-sen={_fmt(kijun_sen)}")
    print(f"  Senkou Span A={_fmt(senkou_span_a)}  Senkou Span B={_fmt(senkou_span_b)}")
    chikou_note = "" if math.isnan(chikou_span) else f"{chikou_span:.2f}"
    if math.isnan(chikou_span):
        chikou_note = "N/A (needs 26 FUTURE candles — expected to be empty at the latest candle)"
    print(f"  Chikou Span={chikou_note}")
    print(f"  -> Cloud trend (engine's own read): {cloud_trend}")

    print(f"\nPivot: {_fmt(pivot)}  R1={_fmt(resistance_1)}  S1={_fmt(support_1)}")
    print(f"  -> candle is {_pivot_read(close, pivot, resistance_1, support_1)}")

    dc_read = _band_position(close, dc_upper, dc_lower, "20-bar Donchian high", "20-bar Donchian low")
    print(f"Donchian(20): upper={_fmt(dc_upper)} middle={_fmt(dc_middle)} lower={_fmt(dc_lower)}")
    print(f"  -> candle is {dc_read}")

    kc_read = _band_position(close, kc_upper, kc_lower, "Keltner upper band", "Keltner lower band")
    print(f"Keltner(20): upper={_fmt(kc_upper)} middle={_fmt(kc_middle)} lower={_fmt(kc_lower)}")
    print(f"  -> candle is {kc_read}")

    print(f"\nMarketRegimeEngine's own market_regime label: {market_regime}")

    print("\n" + "=" * 60)
    print("These are the EXACT numbers strategy/buy_strategy.py and")
    print("strategy/sell_strategy.py's real checks would see for this")
    print("candle — same engine, same data source, no manual re-derivation.")
    print("=" * 60)


if __name__ == "__main__":
    main()
