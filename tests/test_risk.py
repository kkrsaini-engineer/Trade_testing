import pandas as pd
from risk.risk_manager import RiskManager
from decision.validation_engine import ValidationResult
from decision.decision_engine import FinalDecision


def test_risk_block():
    r = RiskManager()

    # Sahi dummy mock objects/data taiyar kiya
    validation = ValidationResult(
        passed=True,
        action="BUY",
        confidence=85.0,
        rejection_reason=None,
    )
    decision = FinalDecision(
        action="BUY",
        confidence=85.0,
        ranking=70.0,
        buy_score=80.0,
        sell_score=20.0,
        buy_probability=75.0,
        sell_probability=15.0,
        expected_return=5.0,
        expected_drawdown=2.0,
        expected_hold_days=5,
    )
    portfolio = {"equity": 1000}
    market = {"regime": "HIGH_VOLATILITY"}

    # Ek dummy row wala DataFrame banaya (RiskManager ko latest row chahiye hoti hai)
    dataframe = pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1_000_000,
                "atr": 1.5,
                "atr_14": 1.5,
                "volume_sma_20": 900_000,
            }
        ]
    )

    # Saare 5 arguments properly keyword ke sath pass kiye
    result = r.evaluate(
        validation=validation,
        decision=decision,
        dataframe=dataframe,
        portfolio=portfolio,
        market=market,
    )

    # Dictionary check karne ke bajay object attribute check kiya
    assert hasattr(result, "safe")


# ==========================================================
# RISK OVERRIDES — Phase 21 point 13: system-safety overrides
# (circuit_breaker/emergency_stop/daily_loss_lock) now tag
# diagnostics["override_category"] = "system_safety"; market-risk
# additives (event_day/vix/news) leave it None. Mirrored BUY/SELL per the
# standing rule — the override logic itself is direction-agnostic, this
# confirms it stays that way. See PHASE21_NOTES.md.
# ==========================================================

def _make_decision(action):
    return FinalDecision(
        action=action,
        confidence=85.0,
        ranking=70.0,
        buy_score=80.0,
        sell_score=20.0,
        buy_probability=75.0,
        sell_probability=15.0,
        expected_return=5.0,
        expected_drawdown=2.0,
        expected_hold_days=5,
    )


def _make_validation(action):
    return ValidationResult(
        passed=True,
        action=action,
        confidence=85.0,
        rejection_reason=None,
    )


def _make_dataframe():
    return pd.DataFrame(
        [
            {
                "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0,
                "volume": 1_000_000, "atr": 1.5, "atr_14": 1.5,
                "volume_sma_20": 900_000,
            }
        ]
    )


def _evaluate(action, portfolio=None, market=None, circuit_breaker=False):
    # Phase 22 (see PHASE22_NOTES.md): circuit_breaker is read from the
    # `market` dict now, not the price dataframe (that was the bug being
    # fixed) — so it's injected into `market` here, not `_make_dataframe`.
    r = RiskManager()
    market = dict(market or {})
    if circuit_breaker:
        market["circuit_breaker"] = True
    return r.evaluate(
        validation=_make_validation(action),
        decision=_make_decision(action),
        dataframe=_make_dataframe(),
        portfolio=portfolio or {"equity": 1000},
        market=market,
    )


def test_buy_circuit_breaker_tags_system_safety_override():
    result = _evaluate("BUY", circuit_breaker=True)
    assert result.safe is False
    assert result.total_risk == 100.0
    assert result.diagnostics["override_category"] == "system_safety"


def test_sell_circuit_breaker_tags_system_safety_override():
    result = _evaluate("SELL", circuit_breaker=True)
    assert result.safe is False
    assert result.total_risk == 100.0
    assert result.diagnostics["override_category"] == "system_safety"


def test_buy_emergency_stop_tags_system_safety_override():
    result = _evaluate("BUY", portfolio={"equity": 1000, "emergency_stop": True})
    assert result.safe is False
    assert result.diagnostics["override_category"] == "system_safety"


def test_sell_emergency_stop_tags_system_safety_override():
    result = _evaluate("SELL", portfolio={"equity": 1000, "emergency_stop": True})
    assert result.safe is False
    assert result.diagnostics["override_category"] == "system_safety"


def test_buy_daily_loss_lock_tags_system_safety_override():
    r = RiskManager()
    result = _evaluate("BUY", portfolio={"equity": 1000, "daily_loss": r.MAX_DAILY_LOSS})
    assert result.safe is False
    assert result.diagnostics["override_category"] == "system_safety"


def test_sell_daily_loss_lock_tags_system_safety_override():
    r = RiskManager()
    result = _evaluate("SELL", portfolio={"equity": 1000, "daily_loss": r.MAX_DAILY_LOSS})
    assert result.safe is False
    assert result.diagnostics["override_category"] == "system_safety"


def test_buy_event_day_additive_does_not_tag_system_safety():
    result = _evaluate("BUY", market={"event_day": True})
    assert result.diagnostics["override_category"] is None
    assert result.diagnostics["event_override"] is True


def test_sell_event_day_additive_does_not_tag_system_safety():
    result = _evaluate("SELL", market={"event_day": True})
    assert result.diagnostics["override_category"] is None
    assert result.diagnostics["event_override"] is True


# ==========================================================
# Phase 26 (see PHASE26_NOTES.md, point 11) — "correlated risk-factor
# double-counting redesign": ATR+Volatility merged into one weighted
# component; sector/correlation removed from the weighted sum (still
# computed/reported); open_positions_count/open_exposure key-name bugs
# fixed to read the REAL portfolio dict keys. Mirrored BUY/SELL per the
# standing rule — none of this logic branches on direction, this
# confirms it stays that way.
# ==========================================================

def _full_dataframe(**overrides):
    row = {
        "atr_14": 1.0, "close": 100.0,
        "gap_up": False, "gap_down": False,
        "volatility_state": "LOW", "bb_width": 0.0,
        "market_regime": "SIDEWAYS",
        "volume_sma_20": 1_000_000, "spread": 0.0, "turnover": 1.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _full_portfolio(**overrides):
    portfolio = {
        "open_positions": {}, "exposure": 0.0,
        "sector_exposure": 0.0, "correlation": 0.0,
        "total_capital": 1_000_000.0, "available_capital": 1_000_000.0,
    }
    portfolio.update(overrides)
    return portfolio


def _full_evaluate(action, dataframe=None, portfolio=None, market=None):
    r = RiskManager()
    return r.evaluate(
        validation=_make_validation(action),
        decision=_make_decision(action),
        dataframe=dataframe if dataframe is not None else _full_dataframe(),
        portfolio=portfolio if portfolio is not None else _full_portfolio(),
        market=market if market is not None else {"overnight": False, "event_day": False, "news_impact": 0.0, "vix": 15.0},
    )


def test_buy_weighted_total_risk_matches_new_weight_scheme():
    # Hand-computed with the Phase 26 weights (price_volatility=0.24,
    # gap=0.09, overnight=0.09, news=0.12, liquidity=0.12, market=0.14,
    # portfolio=0.14, capital=0.06):
    #   price_volatility=15 (LOW volatility_state, bb_width=0 -> 15;
    #     atr_percent=1.0% -> atr_risk=10; max(10,15)=15)
    #   gap=10, overnight=10, news=10, liquidity=0
    #   market=30 (SIDEWAYS +10, vix=15 -> no addition; base 20+10=30)
    #   portfolio=0 (0 open positions, 0 exposure), capital=10 (full cash)
    #   total = 15*.24 + 10*.09*2 + 10*.12 + 0*.12 + 30*.14 + 0*.14 + 10*.06
    #         = 3.6 + 1.8 + 1.2 + 0 + 4.2 + 0 + 0.6 = 11.4
    result = _full_evaluate("BUY")
    assert result.total_risk == 11.4
    assert result.diagnostics["risk_components"]["price_volatility"] == 15.0
    assert "sector" not in result.diagnostics["risk_components"]
    assert "correlation" not in result.diagnostics["risk_components"]


def test_sell_weighted_total_risk_matches_new_weight_scheme():
    result = _full_evaluate("SELL")
    assert result.total_risk == 11.4
    assert result.diagnostics["risk_components"]["price_volatility"] == 15.0


def test_buy_price_volatility_is_max_of_atr_and_volatility_when_atr_higher():
    # atr_percent = 4/100 = 4% -> atr_risk=70 (band <=5.0); volatility_state
    # LOW/bb_width=0 -> volatility_risk=15. max(70, 15) = 70.
    df = _full_dataframe(atr_14=4.0, close=100.0)
    result = _full_evaluate("BUY", dataframe=df)
    assert result.diagnostics["price_volatility_risk"] == 70.0
    assert result.diagnostics["risk_components"]["price_volatility"] == 70.0


def test_sell_price_volatility_is_max_of_atr_and_volatility_when_volatility_higher():
    # atr_percent = 1% -> atr_risk=10; volatility_state HIGH, bb_width=0.35
    # -> 70 + 15 (bb_width>0.30) = 85. max(10, 85) = 85.
    df = _full_dataframe(volatility_state="HIGH", bb_width=0.35)
    result = _full_evaluate("SELL", dataframe=df)
    assert result.diagnostics["price_volatility_risk"] == 85.0
    assert result.diagnostics["risk_components"]["price_volatility"] == 85.0


def test_buy_sector_and_correlation_still_reported_but_do_not_change_total_risk():
    baseline = _full_evaluate("BUY")
    concentrated = _full_evaluate(
        "BUY", portfolio=_full_portfolio(sector_exposure=0.60, correlation=0.95),
    )
    # Both still computed/reported (audit visibility)...
    assert concentrated.diagnostics["sector_risk"] > baseline.diagnostics["sector_risk"]
    assert concentrated.diagnostics["correlation_risk"] > baseline.diagnostics["correlation_risk"]
    # ...but total_risk is UNCHANGED — no longer double-counted here
    # (decision/validation_engine.py is the sole hard-gate for these
    # two facts, see risk_manager.py's Phase 26 comment).
    assert concentrated.total_risk == baseline.total_risk


def test_sell_sector_and_correlation_still_reported_but_do_not_change_total_risk():
    baseline = _full_evaluate("SELL")
    concentrated = _full_evaluate(
        "SELL", portfolio=_full_portfolio(sector_exposure=0.60, correlation=0.95),
    )
    assert concentrated.diagnostics["sector_risk"] > baseline.diagnostics["sector_risk"]
    assert concentrated.diagnostics["correlation_risk"] > baseline.diagnostics["correlation_risk"]
    assert concentrated.total_risk == baseline.total_risk


def test_buy_open_positions_and_exposure_read_from_real_portfolio_keys():
    # Phase 26: was reading "open_positions_count"/"open_exposure" —
    # keys no real producer ever set. Now reads "open_positions" (a
    # dict, len()'d) / "exposure" (the actual keys
    # PortfolioEngine.snapshot()/VirtualPortfolio.snapshot() produce).
    baseline = _full_evaluate("BUY")
    heavy = _full_evaluate(
        "BUY",
        portfolio=_full_portfolio(
            open_positions={f"SYM{i}": {} for i in range(15)}, exposure=0.95,
        ),
    )
    assert heavy.diagnostics["risk_components"]["portfolio"] == 75.0  # 35 (>=15 positions) + 40 (>=0.90 exposure)
    assert heavy.total_risk > baseline.total_risk


def test_sell_open_positions_and_exposure_read_from_real_portfolio_keys():
    baseline = _full_evaluate("SELL")
    heavy = _full_evaluate(
        "SELL",
        portfolio=_full_portfolio(
            open_positions={f"SYM{i}": {} for i in range(15)}, exposure=0.95,
        ),
    )
    assert heavy.total_risk > baseline.total_risk
