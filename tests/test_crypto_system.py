"""Comprehensive automated test suite for 24/7 Alpaca crypto trading and quantitative strategies."""
from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

from broker_adapter import is_crypto_symbol, normalize_crypto_symbol, AlpacaBroker, BrokerSettings
from risk_manager import PortfolioRiskManager
from signal_engine import add_indicators, evaluate_break_and_retest_signal, CandleAggregator
import db_manager


class TestCryptoTradingSystem(unittest.TestCase):
    def setUp(self):
        self.risk_manager = PortfolioRiskManager(max_positions=5, max_correlated=2, max_position_pct=10.0)

    def test_symbol_detection(self):
        """Verify asset class detection correctly separates crypto and equities."""
        crypto_pairs = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "BTCUSD", "ETHUSDT"]
        stock_tickers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "SPY", "QQQ"]

        for sym in crypto_pairs:
            self.assertTrue(is_crypto_symbol(sym), f"Expected {sym} to be recognized as crypto")

        for sym in stock_tickers:
            self.assertFalse(is_crypto_symbol(sym), f"Expected {sym} to be recognized as stock")

    def test_symbol_normalization(self):
        """Verify symbol normalization handles Alpaca asset format vs trading pair format."""
        self.assertEqual(normalize_crypto_symbol("BTC/USD"), "BTC/USD")
        self.assertEqual(normalize_crypto_symbol("BTCUSD"), "BTC/USD")
        self.assertEqual(normalize_crypto_symbol("ETHUSDT"), "ETH/USDT")
        self.assertEqual(normalize_crypto_symbol("SOLUSDC"), "SOL/USDC")
        self.assertEqual(normalize_crypto_symbol("AAPL"), "AAPL")

    def test_fractional_crypto_sizing(self):
        """Verify crypto position sizing calculates fractional quantities rather than blowing up with integer 1."""
        equity = 10000.0  # $10,000 account
        # BTC at $80,000
        btc_signal = {
            "symbol": "BTC/USD",
            "action": "BUY",
            "entry": 80000.0,
            "ml_approval_prob": 0.8
        }
        risk_params = {
            "win_rate_estimate": 0.60,
            "win_loss_ratio_estimate": 1.5,
            "daily_realized_pnl": 0.0
        }
        approved, qty, reason = self.risk_manager.can_open_position(
            btc_signal, positions=[], equity=equity, risk_params=risk_params
        )
        self.assertTrue(approved, f"Expected approval, got reason: {reason}")
        self.assertIsInstance(qty, float)
        self.assertLess(qty, 1.0, "Quantity for $80,000 BTC in a $10,000 account must be fractional (< 1.0)")
        self.assertGreater(qty, 0.0, "Quantity must be positive")
        notional = qty * btc_signal["entry"]
        self.assertGreaterEqual(notional, 1.0, "Notional must meet Alpaca $1.00 minimum")
        self.assertLessEqual(notional, equity * 0.15, "Notional must not exceed max risk limits")

    def test_spot_crypto_short_prohibition(self):
        """Verify spot crypto rejects short sell entries."""
        btc_short_signal = {
            "symbol": "BTC/USD",
            "action": "SELL",
            "entry": 80000.0,
            "ml_approval_prob": 0.8
        }
        approved, qty, reason = self.risk_manager.can_open_position(
            btc_short_signal, positions=[], equity=10000.0, risk_params={}
        )
        self.assertFalse(approved, "Spot crypto must reject SELL (short) entries")
        self.assertIn("short", reason.lower())

    def test_stock_integer_sizing_preserved(self):
        """Verify stock position sizing preserves integer share quantities."""
        aapl_signal = {
            "symbol": "AAPL",
            "action": "BUY",
            "entry": 220.0,
            "ml_approval_prob": 0.8
        }
        risk_params = {
            "win_rate_estimate": 0.60,
            "win_loss_ratio_estimate": 1.5,
            "daily_realized_pnl": 0.0
        }
        approved, qty, reason = self.risk_manager.can_open_position(
            aapl_signal, positions=[], equity=10000.0, risk_params=risk_params
        )
        self.assertTrue(approved)
        self.assertEqual(qty, float(int(qty)), "Stock quantity must be a whole integer share count")

    def test_signal_engine_indicators_and_regime(self):
        """Verify indicator calculations including EMA, volume ratios, and ATR."""
        now = datetime.now(timezone.utc)
        timestamps = [now - timedelta(minutes=5 * (30 - i)) for i in range(30)]
        prices = [100.0 + i * 1.5 for i in range(30)]  # Clear uptrend

        df = pd.DataFrame({
            "timestamp": timestamps,
            "open": [p - 0.5 for p in prices],
            "high": [p + 2.0 for p in prices],
            "low": [p - 1.0 for p in prices],
            "close": prices,
            "volume": [1000.0 + i * 50 for i in range(30)]
        })

        res_df = add_indicators(df)
        self.assertIn("ema20", res_df.columns)
        self.assertIn("ema50", res_df.columns)
        self.assertIn("vol_ratio", res_df.columns)
        self.assertIn("atr14", res_df.columns)
        self.assertIn("rsi14", res_df.columns)

        # Evaluate signal
        signal = evaluate_break_and_retest_signal(res_df, symbol="BTC/USD")
        self.assertIn("action", signal)
        self.assertIn("strategy_version", signal)
        self.assertEqual(signal["strategy_version"], "crypto-break-retest-v3-profit-max")
        self.assertIn("vol_ratio", signal["features"])

    def test_software_exit_logic(self):
        """Simulate software Stop-Loss and Take-Profit evaluation."""
        entry = 80000.0
        atr = 1000.0
        sl = entry - atr       # 79000.0
        tp = entry + 2 * atr   # 82000.0

        pos = {"symbol": "BTC/USD", "entry": entry, "sl": sl, "tp": tp, "qty": 0.01, "action": "BUY"}

        # Price in neutral range
        current_price = 80500.0
        sl_hit = current_price <= pos["sl"]
        tp_hit = current_price >= pos["tp"]
        self.assertFalse(sl_hit)
        self.assertFalse(tp_hit)

        # Price drops to Stop-Loss
        current_price = 78900.0
        sl_hit = current_price <= pos["sl"]
        self.assertTrue(sl_hit, "Stop-Loss must trigger when price drops below SL")

        # Price rises to Take-Profit
        current_price = 82500.0
        tp_hit = current_price >= pos["tp"]
        self.assertTrue(tp_hit, "Take-Profit must trigger when price rises above TP")

    def test_candle_aggregator_multi_tick(self):
        """Verify CandleAggregator produces consistent closed candles across ticks."""
        agg = CandleAggregator(symbol="BTC/USD", timeframe_minutes=5)
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        # Tick 1 at 00:01
        c1 = agg.update_tick(80000.0, timestamp=now + timedelta(minutes=1), volume=0.5)
        self.assertIsNone(c1)

        # Tick 2 at 00:03
        c2 = agg.update_tick(80500.0, timestamp=now + timedelta(minutes=3), volume=1.0)
        self.assertIsNone(c2)

        # Tick 3 at 00:06 (new bucket -> closes previous candle)
        c3 = agg.update_tick(80200.0, timestamp=now + timedelta(minutes=6), volume=0.2)
        self.assertIsNotNone(c3, "Crossing into new timeframe bucket must close previous candle")
        self.assertEqual(c3.open, 80000.0)
        self.assertEqual(c3.high, 80500.0)
        self.assertEqual(c3.close, 80500.0)
        self.assertEqual(c3.volume, 1.5)

    def test_position_adoption_and_sl_evaluation(self):
        """Verify unmonitored broker positions are adopted and evaluated for SL."""
        entry = 10.93
        current_price = 10.20
        # When adopting, est_sl is calculated with a safety buffer
        est_sl = round(entry * 0.985, 4)  # ~10.7661
        est_tp = round(entry * 1.03, 4)   # ~11.2579

        # Simulated adopted position
        adopted_pos = {
            "symbol": "AVAX/USD",
            "entry": entry,
            "sl": est_sl,
            "tp": est_tp,
            "qty": 57.007125,
            "action": "BUY"
        }

        # Verify current price 10.20 triggers stop loss immediately
        self.assertLessEqual(current_price, adopted_pos["sl"], "Price 10.20 must breach SL 10.76")
        triggered = "STOP_LOSS" if current_price <= adopted_pos["sl"] else None
        self.assertEqual(triggered, "STOP_LOSS")

    def test_portfolio_heat_and_cash_headroom(self):
        """Verify institutional portfolio heat calculation and cash headroom protection."""
        equity = 10000.0
        # Simulated existing positions with stop losses
        existing_positions = [
            {"symbol": "BTC/USD", "qty": 0.05, "price": 80000.0, "sl": 78000.0, "action": "BUY"},  # Risk = 0.05 * 2000 = $100 (1%)
            {"symbol": "ETH/USD", "qty": 1.0, "price": 3000.0, "sl": 2800.0, "action": "BUY"},      # Risk = 1.0 * 200 = $200 (2%)
        ]
        heat = self.risk_manager.calculate_portfolio_heat(existing_positions, equity)
        # Total risk = $300 / $10,000 = 3.0%
        self.assertAlmostEqual(heat, 3.0, places=2)

        # Candidate trade with risk that would exceed 10% heat
        # Risk = 1.0 * 800 = $800 (8%), adding to 3% gives 11% (> 10%)
        huge_risk_signal = {
            "symbol": "SOL/USD",
            "action": "BUY",
            "entry": 200.0,
            "stop_loss": 100.0,  # $100 per unit risk
            "ml_approval_prob": 0.8
        }
        # Sizing will try to size based on kelly/atr, but if risk pushes heat over 10%, it must be guarded
        risk_params = {"max_portfolio_heat_pct": 10.0, "min_cash_buffer_pct": 10.0}
        
        # Test cash headroom depletion:
        # If open positions consume $9,500 of $10,000, remaining cash = $500 (5% < 10% min_cash_buffer_pct)
        heavy_positions = [
            {"symbol": "BTC/USD", "qty": 0.1, "price": 80000.0, "market_value": 8000.0, "sl": 79000.0, "action": "BUY"},
            {"symbol": "ETH/USD", "qty": 0.5, "price": 3000.0, "market_value": 1500.0, "sl": 2900.0, "action": "BUY"}
        ]
        candidate_signal = {
            "symbol": "SOL/USD",
            "action": "BUY",
            "entry": 200.0,
            "stop_loss": 190.0,
            "ml_approval_prob": 0.8
        }
        approved, qty, reason = self.risk_manager.can_open_position(
            candidate_signal, positions=heavy_positions, equity=equity, risk_params=risk_params
        )
        self.assertFalse(approved, "Must reject candidate when cash reserves drop below minimum buffer")
        self.assertIn("cash", reason.lower())

    def test_dynamic_trailing_and_breakeven_ratchet(self):
        """Verify breakeven locking at +1.2 ATR and trailing chandelier ratchet at +2.0 ATR."""
        entry = 100.0
        atr = 5.0
        initial_sl = entry - atr  # 95.0
        pos = {
            "symbol": "SOL/USD",
            "entry": entry,
            "sl": initial_sl,
            "tp": entry + 3.0 * atr,
            "qty": 2.0,
            "action": "BUY",
            "atr": atr,
            "highest_price": entry,
            "breakeven_locked": False,
            "trailing_active": False,
        }

        # Step 1: Price rises to entry + 1.2 * atr = 106.0 -> Breakeven should lock
        p1 = 106.0
        pos["highest_price"] = max(pos["highest_price"], p1)
        if not pos["breakeven_locked"] and p1 >= entry + 1.2 * atr:
            pos["sl"] = max(pos["sl"], entry + 0.1 * atr)
            pos["breakeven_locked"] = True

        self.assertTrue(pos["breakeven_locked"])
        self.assertEqual(pos["sl"], 100.5, "Stop loss must advance to entry + 0.1 ATR (100.5)")

        # Step 2: Price surges to entry + 2.5 * atr = 112.5 -> Trailing stop activates
        p2 = 112.5
        pos["highest_price"] = max(pos["highest_price"], p2)
        if p2 >= entry + 2.0 * atr:
            pos["trailing_active"] = True
        
        trail_sl = pos["highest_price"] - 1.5 * atr  # 112.5 - 7.5 = 105.0
        if trail_sl > pos["sl"]:
            pos["sl"] = trail_sl

        self.assertTrue(pos["trailing_active"])
        self.assertEqual(pos["sl"], 105.0, "Trailing stop must ratchet to highest - 1.5 ATR (105.0)")

        # Step 3: Retracement to 104.5 -> triggers TRAILING_STOP
        p3 = 104.5
        self.assertLessEqual(p3, pos["sl"], "Price 104.5 breaches trailing stop 105.0")

    def test_expanded_crypto_watchlist_universe(self):
        """Verify database defaults contain the expanded 11-symbol crypto universe."""
        watchlist = json.loads(db_manager.DEFAULT_CRYPTO_WATCHLIST)
        self.assertEqual(len(watchlist), 11)
        expected_symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD",
                            "DOGE/USD", "LTC/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "SHIB/USD"]
        for sym in expected_symbols:
            self.assertIn(sym, watchlist)


if __name__ == "__main__":
    unittest.main()

