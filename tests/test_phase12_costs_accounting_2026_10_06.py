"""
BUG_AUDIT_2026-10-05_PROFITABILITY.md, 2026-10-06 batch:

H13  transaction costs (STT, stamp, exchange, SEBI, GST, DP, slippage) —
     previously zero everywhere, so all paper P&L was gross. Booked at
     exit (entry stays the market fill, so stops/targets don't move).
M5   cash accounting: cash = capital + REALIZED P&L - capital in open
     positions (it used realized + unrealized, and double-counted on the
     next close); partial-exit P&L on open positions now in total_pnl;
     exposure updated on every entry; old state files self-heal on load.
M6   Morning Executor respects weekly/monthly loss and drawdown halt.
M7   diary/journal entry rows carry real probability/confidence/regime.
"""

import json
import time
from pathlib import Path

import pytest

import config
from analytics.backtest_engine import BacktestEngine
from paper_trading.paper_trading_engine import PaperTradingEngine
from paper_trading.virtual_portfolio import VirtualPortfolio
from portfolio.portfolio import PortfolioEngine, PortfolioState
from risk.transaction_costs import ZERO_COSTS, CostModel, net_exit_price, round_trip_cost
from scripts.morning_executor import check_loss_limits
from storage.trades.trade_diary import TradeDiary
from storage.trades.trade_store import TradeStore
from tests.test_paper_trading_exit_wiring import FakeScanner
from tests.test_phase11_backtest_live_flow_2026_10_06 import FakeScanner as BacktestFakeScanner
from tests.test_phase11_backtest_live_flow_2026_10_06 import _day, _series

DEFAULT = CostModel(buy_pct=0.1189, sell_pct=0.1039, sell_flat_rupees=15.93, slippage_pct=0.05)


# ==========================================================
# H13 — cost model
# ==========================================================

def test_buy_round_trip_cost_matches_published_delivery_rates():
    # 100 shares, bought at 100 (₹10,000), sold at 110 (₹11,000).
    expected = 10_000 * (0.1189 + 0.05) / 100 + 11_000 * (0.1039 + 0.05) / 100 + 15.93
    assert round_trip_cost("BUY", 100.0, 110.0, 100, DEFAULT) == pytest.approx(expected)


def test_sell_round_trip_cost_has_no_dp_charge():
    expected = 10_000 * (0.1039 + 0.05) / 100 + 9_000 * (0.1189 + 0.05) / 100
    assert round_trip_cost("SELL", 100.0, 90.0, 100, DEFAULT) == pytest.approx(expected)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_net_exit_price_books_exactly_the_cost(direction):
    exit_price = 110.0 if direction == "BUY" else 90.0
    net = net_exit_price(direction, 100.0, exit_price, 100, DEFAULT)
    gross_pnl = (exit_price - 100.0) * 100 * (1 if direction == "BUY" else -1)
    net_pnl = (net - 100.0) * 100 * (1 if direction == "BUY" else -1)
    assert gross_pnl - net_pnl == pytest.approx(round_trip_cost(direction, 100.0, exit_price, 100, DEFAULT))


def test_zero_cost_model_changes_nothing():
    assert net_exit_price("BUY", 100.0, 110.0, 100, ZERO_COSTS) == 110.0


def test_costs_can_be_switched_off_by_config(monkeypatch):
    monkeypatch.setattr(config, "CONFIG", config.AppConfig(apply_transaction_costs=False))
    assert CostModel.from_config() == ZERO_COSTS


def test_config_defaults_match_the_documented_rates():
    model = CostModel.from_config()
    assert model.buy_pct == pytest.approx(0.1189)
    assert model.sell_pct == pytest.approx(0.1039)
    assert model.sell_flat_rupees == pytest.approx(15.93)


# ==========================================================
# H13 — wired into paper trading
# ==========================================================

SIMPLE = CostModel(buy_pct=0.1, sell_pct=0.1, sell_flat_rupees=10.0, slippage_pct=0.0)


@pytest.fixture
def engine_setup(tmp_path):
    portfolio = VirtualPortfolio(initial_capital=500_000.0, state_path=str(tmp_path / "portfolio.json"))
    diary = TradeDiary(base_path=str(tmp_path / "diary"))
    trade_store = TradeStore(path=str(tmp_path / "trades"))
    return portfolio, diary, trade_store


def _open(portfolio, diary, quantity=10):
    portfolio.engine.add_position(symbol="TESTCO.NS", quantity=quantity, entry_price=100.0, direction="BUY")
    diary.open_trade(
        trade_id=f"paper_TESTCO_NS_{int(time.time() * 1000)}", symbol="TESTCO.NS", direction="BUY",
        entry_price=100.0, entry_date="2026-01-01", buy_probability=70.0, buy_confidence=80.0,
        entry_reasons=["test"],
    )


def test_paper_full_exit_pnl_is_net_of_costs(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary)
    # Stop at 96 is touched (low 79) -> fills at 96.
    scanner = FakeScanner(close=80.0, atr=2.0, day_high=81.0, day_low=79.0)
    engine = PaperTradingEngine(
        scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store, cost_model=SIMPLE,
    )
    engine.run_cycle(["TESTCO.NS"], force=True)

    closed = portfolio.engine.state.closed_positions[-1]
    costs = 1000 * 0.001 + 960 * 0.001 + 10.0  # buy leg + sell leg + DP
    assert closed.realized_pnl == pytest.approx(-40.0 - costs)
    rows = trade_store.get_closed_trades()
    assert float(rows[-1]["exit_price"]) == 96.0  # journal keeps the market price
    assert f"costs Rs {costs:.2f} deducted" in rows[-1]["reasons"]


def test_paper_partial_exit_pnl_is_net_of_costs(engine_setup):
    portfolio, diary, trade_store = engine_setup
    _open(portfolio, diary, quantity=10)
    # target1 104 touched -> 5 shares at 104.
    scanner = FakeScanner(close=105.0, atr=2.0, day_high=105.5, day_low=104.5)
    engine = PaperTradingEngine(
        scanner=scanner, portfolio=portfolio, diary=diary, trade_store=trade_store, cost_model=SIMPLE,
    )
    engine.run_cycle(["TESTCO.NS"], force=True)

    pos = portfolio.engine.state.open_positions["TESTCO.NS"]
    costs = 500 * 0.001 + 520 * 0.001 + 10.0
    assert pos.realized_pnl == pytest.approx(20.0 - costs)


# ==========================================================
# H13 — same model in the backtest
# ==========================================================

def test_backtest_applies_the_shared_cost_model():
    df = _series([(100.0, 100.5, 99.5, 100.0), (101.0, 108.0, 100.8, 106.0)])
    signals = {_day(df, 4): ("X.NS", "BUY")}
    plain = BacktestEngine(scanner=BacktestFakeScanner(signals)).run(
        historical_data={"X.NS": df}, initial_capital=500_000.0, min_history=3,
    )
    costly = BacktestEngine(scanner=BacktestFakeScanner(signals)).run(
        historical_data={"X.NS": df}, initial_capital=500_000.0, min_history=3, cost_model=SIMPLE,
    )
    qty = 250  # 5% of ₹5L at 100
    expected_costs = round_trip_cost("BUY", 100.0, 107.0, qty, SIMPLE)
    assert plain.closed_trades[0]["realized_pnl"] - costly.closed_trades[0]["realized_pnl"] == pytest.approx(expected_costs)


# ==========================================================
# M5 — cash accounting
# ==========================================================

def _engine():
    return PortfolioEngine(state=PortfolioState(total_capital=500_000.0, available_capital=500_000.0))


@pytest.mark.parametrize("direction,price", [("BUY", 110.0), ("SELL", 90.0)])
def test_unrealized_gain_is_not_cash_and_close_does_not_double_count(direction, price):
    engine = _engine()
    engine.add_position("X", quantity=100, entry_price=100.0, direction=direction)
    engine.update_position("X", current_price=price)
    engine.mark_to_market()

    assert engine.state.total_pnl == pytest.approx(1000.0)
    assert engine.state.available_capital == pytest.approx(490_000.0)  # paper gain is not cash yet

    engine.close_position("X", exit_price=price)

    assert engine.state.total_pnl == pytest.approx(1000.0)        # was 2000 (double count)
    assert engine.state.available_capital == pytest.approx(501_000.0)


def test_partial_exit_pnl_on_open_position_is_in_total_pnl():
    engine = _engine()
    engine.add_position("X", quantity=100, entry_price=100.0, direction="BUY")
    engine.partial_exit("X", quantity=50, exit_price=110.0)   # +500 booked
    engine.update_position("X", current_price=110.0)
    engine.mark_to_market()

    assert engine.state.total_pnl == pytest.approx(1000.0)    # 500 booked + 500 unrealized
    assert engine.state.available_capital == pytest.approx(500_000.0 + 500.0 - 5_000.0)


def test_exposure_updates_on_every_entry():
    engine = _engine()
    engine.add_position("X", quantity=100, entry_price=100.0, direction="BUY")
    assert engine.state.exposure == pytest.approx(0.02)
    engine.add_position("Y", quantity=100, entry_price=200.0, direction="BUY")
    assert engine.state.exposure == pytest.approx(0.06)


def test_drifted_state_file_self_heals_on_load(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "total_capital": 500_000.0,
        "available_capital": 491_000.0,   # wrong: includes 1000 of unrealized
        "used_capital": 10_000.0,
        "total_pnl": 0.0,                 # wrong: misses 300 booked on the open position
        "open_positions": {"X": {
            "symbol": "X", "quantity": 100, "entry_price": 100.0, "current_price": 110.0,
            "direction": "BUY", "unrealized_pnl": 1000.0, "unrealized_pnl_percent": 10.0,
            "realized_pnl": 300.0, "highest_price": 110.0, "lowest_price": 100.0,
            "max_profit_percent": 10.0, "max_drawdown_percent": 0.0, "status": "OPEN", "updated_at": 0.0,
        }},
        "closed_positions": [],
    }))
    state = VirtualPortfolio(initial_capital=500_000.0, state_path=str(path)).engine.state
    assert state.available_capital == pytest.approx(500_000.0 + 300.0 - 10_000.0)
    assert state.total_pnl == pytest.approx(300.0 + 1000.0)


# ==========================================================
# M6 — loss limits at the open
# ==========================================================

@pytest.mark.parametrize("snapshot,ok", [
    ({"weekly_loss": 0.02, "monthly_loss": 0.05, "max_drawdown": 0.04}, True),
    ({"weekly_loss": 0.07}, False),
    ({"monthly_loss": 0.13}, False),
    ({"max_drawdown": 0.16}, False),
    ({"daily_loss": 0.09}, True),   # daily deliberately not checked at 9:16 (stale)
    ({}, True),
])
def test_check_loss_limits(snapshot, ok):
    assert check_loss_limits(snapshot)[0] is ok


# ==========================================================
# M7 — real entry values recorded
# ==========================================================

def test_entry_rows_carry_real_scan_values():
    executor = Path("scripts/morning_executor.py").read_text()
    report = Path("scripts/generate_full_report.py").read_text()
    assert '"probability": round(r.probability, 2)' in report
    assert '"confidence": round(r.confidence, 2)' in report
    assert '"market_regime": d.get("market_regime")' in report
    assert 'buy_probability=float(c.get("probability") or 0.0)' in executor
    assert 'buy_probability=0.0, buy_confidence=0.0' not in executor
    assert '"regime": "N/A", "confidence": 0.0' not in executor
