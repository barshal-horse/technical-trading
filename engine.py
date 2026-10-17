"""Standalone SQLite-controlled multi-symbol event-driven trading daemon.

Run separately from the dashboard: ``python engine.py``. It starts PAUSED by default.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import time
from pathlib import Path
from typing import Any

import pandas as pd

import db_manager
from broker_adapter import (BrokerError, TradingDisabledError,
                            UnsupportedProtectionError, create_broker_from_env,
                            is_crypto_symbol, normalize_crypto_symbol)
from ml_enhancer import RLSignalFilter, get_market_sentiment, warmup_market_sentiment
from risk_manager import PortfolioRiskManager, load_risk_params_from_db
from signal_engine import CandleAggregator, add_indicators, evaluate_break_and_retest_signal

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
logging.Formatter.converter = time.gmtime
logger = logging.getLogger("engine")


def _candle_dict(candle) -> dict[str, Any]:
    return {"timestamp": candle.timestamp.isoformat(), "symbol": candle.symbol,
            "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.volume}


class MultiSymbolDaemon:
    def __init__(self, timeframe_minutes: int = 5):
        self.timeframe_minutes = timeframe_minutes
        self.filter = RLSignalFilter()
        self.risk_manager = PortfolioRiskManager()
        self.aggregators: dict[str, CandleAggregator] = {}
        self.pending_outcomes: dict[str, list[tuple[int, str, float, int]]] = {}
        self.closed_counts: dict[str, int] = {}
        self._last_control_check = 0.0
        self._last_control: dict | None = None
        self._sentiment_cache: dict[str, dict] = {}  # symbol -> {checked_at, score, available}
        self._last_scan = 0.0
        self._active_symbols: set[str] = set()  # symbols with pending/filled orders
        self._active_orders: dict[str, str] = {}  # symbol -> broker_order_id
        self._active_crypto_positions: dict[str, dict] = {}  # symbol -> {log_id, symbol, entry, sl, tp, qty, action}
        self._shutdown_event = asyncio.Event()
        self._stream_task: asyncio.Task | None = None
        self._current_broker = None

    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                def _handle_shutdown():
                    logger.info("Termination signal received; requesting graceful engine shutdown...")
                    self._shutdown_event.set()
                    if self._stream_task and not self._stream_task.done():
                        self._stream_task.cancel()
                loop.add_signal_handler(sig, _handle_shutdown)
            except NotImplementedError:
                pass  # Windows doesn't support signal handlers in the same way

    async def _cleanup(self) -> None:
        logger.info("Shutting down gracefully...")
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        if self._current_broker:
            try:
                await self._current_broker.disconnect()
            except Exception:
                logger.exception("Error during broker disconnect")

    async def run(self) -> None:
        self._setup_signal_handlers()
        if os.getenv("FINBERT_ENABLED", "true").lower() == "true" and os.getenv("NEWS_SOURCE", "alpaca").lower() != "disabled":
            try:
                await asyncio.to_thread(warmup_market_sentiment)
                logger.info("FinBERT sentiment model loaded and cached")
            except Exception:
                logger.exception("FinBERT warm-up failed; paper execution remains technical-only until sentiment is available")
        auto_run = os.getenv("TRADING_ENABLED", "false").lower() == "true" or bool(os.getenv("CI")) or bool(os.getenv("GITHUB_ACTIONS"))
        if auto_run:
            ctrl = db_manager.get_control_settings()
            if ctrl["status"] != "RUNNING":
                db_manager.update_control_settings(status="RUNNING", env=ctrl["env"], symbol=ctrl["symbol"], atr_mult=ctrl["atr_mult"])
                logger.info("Auto-enabled RUNNING status for automated/headless environment")

        logger.info("Multi-symbol trading daemon started; initial control state: %s", db_manager.get_control_settings())
        try:
            while not self._shutdown_event.is_set():
                control = db_manager.get_control_settings()
                if control["status"] in {"PAUSED", "STOPPED"}:
                    await asyncio.sleep(1.0)
                    continue
                try:
                    self._stream_task = asyncio.create_task(self._run_stream(control))
                    await self._stream_task
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    if not self._shutdown_event.is_set():
                        from broker_adapter import BrokerError
                        if isinstance(exc, BrokerError) and ("ALPACA_API_KEY" in str(exc) or "credentials" in str(exc).lower()):
                            logger.critical("Fatal broker credential error: %s", exc)
                            raise SystemExit(1)
                        logger.exception("Market session failed; reconnecting after delay")
                        await asyncio.sleep(2.0)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Engine received stop request; exiting main loop...")
        finally:
            await self._cleanup()

    async def _run_stream(self, initial_control: dict) -> None:
        env = initial_control["env"]
        broker = create_broker_from_env(env)
        self._current_broker = broker
        await broker.connect()

        control = db_manager.get_control_settings()
        symbols = [s.strip().upper() for s in json.loads(control.get("watchlist_json", "[]")) if s.strip()]
        if not symbols:
            symbols = [
                "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD",
                "DOGE/USD", "LTC/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "SHIB/USD"
            ]
        scan_interval = control.get("scan_interval_min", 5)
        logger.info("Starting multi-symbol stream for %d symbols: %s", len(symbols), symbols)

        # Reload active crypto positions from DB for software SL/TP monitoring
        self._active_crypto_positions = {}
        for row in db_manager.get_active_crypto_positions():
            sym = row["symbol"]
            entry = row["price"] or row["entry_fill_price"] or 0.0
            sl = row["sl"]
            self._active_crypto_positions[sym] = {
                "log_id": row["id"],
                "symbol": sym,
                "entry": entry,
                "sl": sl,
                "tp": row["tp"],
                "qty": row["filled_qty"] or float(os.getenv("ORDER_QUANTITY", "1")),
                "action": row["action"],
                "atr": abs(entry - sl) if (sl is not None and entry != sl) else (entry * 0.015),
                "highest_price": entry,
                "breakeven_locked": False,
                "trailing_active": False,
            }
        if self._active_crypto_positions:
            logger.info("Loaded %d active crypto positions for software exit monitoring: %s",
                        len(self._active_crypto_positions), list(self._active_crypto_positions.keys()))

        self.aggregators = {}
        self.pending_outcomes = {}
        self.closed_counts = {}
        for sym in symbols:
            self.aggregators[sym] = CandleAggregator(symbol=sym, timeframe_minutes=self.timeframe_minutes, max_candles=200)
            self.pending_outcomes[sym] = []
            self.closed_counts[sym] = 0

        try:
            historical_batch = await broker.get_recent_candles_batch(symbols, self.timeframe_minutes, 200)
            for sym, historical in historical_batch.items():
                if historical:
                    self.aggregators[sym].seed_closed(historical)
                    history_frame = add_indicators(pd.DataFrame(historical))
                    for _, row in history_frame.iterrows():
                        stamp = row["timestamp"].isoformat()
                        ma20 = float(row["sma20"]) if pd.notna(row["sma20"]) else None
                        atr = float(row["atr14"]) if pd.notna(row["atr14"]) else None
                        db_manager.insert_candle(sym, stamp, row["open"], row["high"], row["low"],
                                                 row["close"], ma20, atr, row.get("volume", 0))
                    logger.info("Loaded %d closed historical candles for %s", len(historical), sym)
        except Exception:
            logger.exception("Historical warm-up unavailable; collecting live candles")

        # Cross-Run Ephemeral Continuity: Query open broker crypto positions and adopt any unmonitored trades
        try:
            broker_positions = await broker.get_positions()
            for b_pos in broker_positions:
                b_sym = b_pos.get("symbol", "")
                b_qty = float(b_pos.get("qty", 0.0))
                if not is_crypto_symbol(b_sym) or b_qty <= 0:
                    continue
                norm_sym = normalize_crypto_symbol(b_sym)
                if norm_sym not in self._active_crypto_positions:
                    entry = float(b_pos.get("avg_entry_price", 0.0))
                    curr_px = float(b_pos.get("current_price", 0.0))
                    if entry <= 0:
                        entry = curr_px or await broker.get_latest_price(norm_sym)

                    atr = None
                    if norm_sym in self.aggregators:
                        df_agg = self.aggregators[norm_sym].dataframe()
                        if len(df_agg) >= 14:
                            df_ind = add_indicators(df_agg)
                            last_atr = df_ind.iloc[-1].get("atr14")
                            if pd.notna(last_atr) and float(last_atr) > 0:
                                atr = float(last_atr)

                    atr_mult = float(control.get("atr_mult", 1.0))
                    if atr is not None and atr > 0:
                        est_sl = round(entry - (1.5 * atr_mult * atr), 4 if entry > 10 else 6)
                        est_tp = round(entry + (2.0 * atr_mult * atr), 4 if entry > 10 else 6)
                    else:
                        est_sl = round(entry * 0.985, 4 if entry > 10 else 6)
                        est_tp = round(entry * 1.03, 4 if entry > 10 else 6)

                    log_id = db_manager.log_trade(
                        norm_sym, "BUY", entry, est_sl, est_tp,
                        "ENTRY_FILLED:ADOPTED", 0.5,
                        features={"adopted": True, "broker_qty": b_qty}
                    )
                    db_manager.update_trade_order_state(
                        log_id, "ENTRY_FILLED:ADOPTED", filled_qty=b_qty, entry_fill_price=entry
                    )
                    self._active_crypto_positions[norm_sym] = {
                        "log_id": log_id,
                        "symbol": norm_sym,
                        "entry": entry,
                        "sl": est_sl,
                        "tp": est_tp,
                        "qty": b_qty,
                        "action": "BUY",
                        "atr": atr or abs(entry - est_sl),
                        "highest_price": max(entry, curr_px),
                        "breakeven_locked": False,
                        "trailing_active": False,
                    }
                    logger.warning(
                        "Adopted active broker crypto position for %s: qty=%.6f entry=%.4f sl=%.4f tp=%.4f",
                        norm_sym, b_qty, entry, est_sl, est_tp
                    )

            # Immediately evaluate software exits on startup for all active crypto positions
            for sym in list(self._active_crypto_positions.keys()):
                try:
                    px = await broker.get_latest_price(sym)
                    await self._check_software_exits(broker, sym, px)
                except Exception as e:
                    logger.warning("Startup software exit check failed for %s: %s", sym, e)
        except Exception as exc:
            logger.exception("Failed to query and adopt broker positions on startup: %s", exc)

        stream = broker.stream_ticks(symbols)
        next_control_check = time.monotonic()
        next_order_reconcile = time.monotonic() + 5.0
        pending_tick = asyncio.create_task(anext(stream))
        try:
            while True:
                done, _ = await asyncio.wait({pending_tick}, timeout=0.5)
                if time.monotonic() >= next_order_reconcile:
                    await self._reconcile_orders(broker)
                    for sym in list(self._active_crypto_positions.keys()):
                        try:
                            px = await broker.get_latest_price(sym)
                            await self._check_software_exits(broker, sym, px)
                        except Exception as e:
                            logger.debug("Periodic exit check price fetch failed for %s: %s", sym, e)
                    next_order_reconcile = time.monotonic() + 5.0
                if time.monotonic() - self._last_scan >= scan_interval * 60:
                    await self._run_scanner(broker, control)
                    self._last_scan = time.monotonic()
                if not done:
                    if time.monotonic() >= next_control_check:
                        control = db_manager.get_control_settings()
                        next_control_check = time.monotonic() + 0.5
                        if control["status"] != "RUNNING" or control["env"] != env:
                            logger.info("Control changed; closing current market stream")
                            return
                        new_symbols = [s.strip().upper() for s in json.loads(control.get("watchlist_json", "[]")) if s.strip()]
                        if set(new_symbols) != set(symbols):
                            added = set(new_symbols) - set(symbols)
                            removed = set(symbols) - set(new_symbols)
                            if added:
                                logger.info("Watchlist added symbols: %s", sorted(added))
                            if removed:
                                logger.info("Watchlist removed symbols: %s", sorted(removed))
                            logger.info("Watchlist changed; restarting stream to apply changes")
                            return
                    continue
                try:
                    tick = pending_tick.result()
                except StopAsyncIteration as exc:
                    raise RuntimeError("Market data stream closed; reconnecting") from exc
                pending_tick = asyncio.create_task(anext(stream))

                if time.monotonic() >= next_control_check:
                    control = db_manager.get_control_settings()
                    next_control_check = time.monotonic() + 0.5
                    if control["status"] != "RUNNING" or control["env"] != env:
                        logger.info("Control changed; closing current market stream")
                        return

                symbol = tick.get("symbol", "")
                if symbol not in self.aggregators:
                    continue
                current_price = float(tick["price"])
                await self._check_software_exits(broker, symbol, current_price)
                aggregator = self.aggregators[symbol]
                closed = aggregator.update_tick(current_price, tick.get("timestamp"), float(tick.get("volume", 0)))
                if closed is not None:
                    self.closed_counts[symbol] += 1
                    await self._on_candle_close(symbol, aggregator, closed, control, broker)
        finally:
            if not pending_tick.done():
                pending_tick.cancel()
            await asyncio.gather(pending_tick, return_exceptions=True)
            await stream.aclose()

    async def _on_candle_close(self, symbol: str, aggregator: CandleAggregator,
                                candle, control: dict, broker) -> None:
        candles = aggregator.dataframe()
        if len(candles) >= 14:
            indicators = add_indicators(candles)
            last = indicators.iloc[-1]
            ma20 = float(last.sma20) if last.sma20 == last.sma20 else None
            atr = float(last.atr14) if last.atr14 == last.atr14 else None
        else:
            ma20 = atr = None
        db_manager.insert_candle(symbol, candle.timestamp.isoformat(), candle.open, candle.high, candle.low,
                                 candle.close, ma20, atr, candle.volume)

        still_pending = []
        horizon_bars = max(1, round(15 / self.timeframe_minutes))
        for log_id, action, entry, created_count in self.pending_outcomes[symbol]:
            if self.closed_counts[symbol] - created_count >= horizon_bars:
                direction = 1.0 if action == "BUY" else -1.0
                db_manager.update_forward_return(log_id, direction * (candle.close - entry) / entry)
            else:
                still_pending.append((log_id, action, entry, created_count))
        self.pending_outcomes[symbol] = still_pending

        logger.info("Closed %s candle %s O=%.4f H=%.4f L=%.4f C=%.4f MA20=%s ATR=%s",
                    symbol, candle.timestamp.isoformat(), candle.open, candle.high, candle.low,
                    candle.close, ma20, atr)

    async def _run_scanner(self, broker, control: dict) -> None:
        """Scan all symbols for signals and submit top candidates respecting risk limits."""
        logger.info("Scanner: running scan for %d symbols", len(self.aggregators))
        if not control.get("scan_enabled"):
            logger.info("Scanner: scan_enabled is false, skipping")
            return
        risk_params = load_risk_params_from_db()
        risk_params.update({
            "max_positions": int(os.getenv("MAX_POSITIONS", control.get("max_positions", 15))),
            "max_correlated_positions": int(os.getenv("MAX_CORRELATED_POSITIONS", control.get("max_correlated_positions", 15))),
            "min_signal_score": float(os.getenv("MIN_SIGNAL_SCORE", control.get("min_signal_score", 0.55))),
            "kelly_fraction": float(os.getenv("KELLY_FRACTION", control.get("kelly_fraction", 0.30))),
            "max_position_pct": float(os.getenv("MAX_POSITION_PCT", control.get("max_position_pct", 15.0))),
            "max_portfolio_heat_pct": float(os.getenv("MAX_PORTFOLIO_HEAT_PCT", 10.0)),
            "min_cash_buffer_pct": float(os.getenv("MIN_CASH_BUFFER_PCT", 10.0)),
        })
        self.risk_manager.max_positions = risk_params["max_positions"]
        self.risk_manager.max_correlated = risk_params["max_correlated_positions"]
        self.risk_manager.kelly_fraction = risk_params["kelly_fraction"]
        self.risk_manager.max_position_pct = risk_params["max_position_pct"]
        self.risk_manager.max_portfolio_heat_pct = risk_params["max_portfolio_heat_pct"]
        self.risk_manager.min_cash_buffer_pct = risk_params["min_cash_buffer_pct"]

        try:
            positions = await broker.get_positions()
        except Exception:
            logger.exception("Failed to fetch positions for scanner")
            return

        candidates = []
        for symbol in self.aggregators.keys():
            # Use DB candles which include historical + live data
            candles = db_manager.get_recent_candles(symbol, 50)
            if len(candles) < 21:
                logger.debug("Scanner: %s has only %d candles, skipping", symbol, len(candles))
                continue
            # Compute indicators on-the-fly from raw OHLCV (DB may not have MA20/ATR for all symbols)
            df = add_indicators(candles)
            signal = evaluate_break_and_retest_signal(df, symbol=symbol)
            if signal["action"] not in {"BUY", "SELL"}:
                continue
            last = df.iloc[-1]
            atr = float(last["atr14"]) if pd.notna(last.get("atr14")) else None
            ma20 = float(last["sma20"]) if pd.notna(last.get("sma20")) else None
            if atr is None or ma20 is None or atr <= 0:
                continue
            signal["stop_loss"] = (signal["entry"] - float(control["atr_mult"]) * atr
                                   if signal["action"] == "BUY" else
                                   signal["entry"] + float(control["atr_mult"]) * atr)
            signal["timestamp"] = last["timestamp"].isoformat() if hasattr(last["timestamp"], "isoformat") else str(last["timestamp"])

            # Compute volume ratio
            volumes = df["volume"].tail(20)
            avg_vol = volumes.mean() if len(volumes) > 0 else 0
            vol_ratio = float(volumes.iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

            # Compute MTF alignment score (simplified)
            mtf_score = 1.0  # will be enhanced in future

            # ML approval probability
            features = signal.get("features", {})
            sentiment, sentiment_available = await self._read_sentiment(symbol=symbol, broker=broker)
            market_features = {"ma_distance_ratio": features.get("ma_distance_ratio", 0.0),
                               "atr_ratio": features.get("atr_ratio", 0.0),
                               "rsi14": features.get("rsi14", 50.0),
                               "sentiment_score": sentiment,
                               "sentiment_available": sentiment_available}
            self.filter.enabled = bool(control.get("ml_gate_enabled", False))
            decision = self.filter.should_execute_trade(signal, market_features)
            ml_prob = decision.get("confidence") if decision.get("confidence") is not None else (1.0 if decision.get("approved") else 0.0)

            composite_score = 0.4 * (1.0 if signal["action"] != "FLAT" else 0.0) + \
                              0.2 * min(vol_ratio / 1.5, 1.0) + \
                              0.2 * mtf_score + \
                              0.2 * (ml_prob if ml_prob is not None else 0.5)

            logger.info("Scanner: %s %s @ %.4f composite=%.3f vol_ratio=%.2f ml_prob=%.2f",
                        signal["action"], symbol, signal["entry"], composite_score, vol_ratio, ml_prob)

            if composite_score >= risk_params["min_signal_score"]:
                signal["composite_score"] = composite_score
                signal["ml_approval_prob"] = ml_prob
                signal["vol_ratio"] = vol_ratio
                signal["market_features"] = market_features
                candidates.append(signal)

        candidates.sort(key=lambda s: s["composite_score"], reverse=True)

        equity = await self._get_equity(broker)
        # Dynamic Risk Headroom: evaluate top candidates against live portfolio heat and cash reserves
        for signal in candidates:
            approved, qty, reason = self.risk_manager.can_open_position(
                signal, positions, equity=equity, risk_params=risk_params)
            if not approved:
                log_id = db_manager.log_trade(
                    signal["symbol"], signal["action"], signal["entry"], signal["stop_loss"],
                    signal["take_profit"], f"BLOCKED:{reason}", signal.get("ml_approval_prob"),
                    {**signal.get("features", {}), **signal.get("market_features", {})})
                self.pending_outcomes[signal["symbol"]].append(
                    (log_id, signal["action"], signal["entry"], self.closed_counts[signal["symbol"]]))
                continue

            log_id = db_manager.log_trade(
                signal["symbol"], signal["action"], signal["entry"], signal["stop_loss"],
                signal["take_profit"], "SIGNAL", signal.get("ml_approval_prob"),
                {**signal.get("features", {}), **signal.get("market_features", {})})
            self.pending_outcomes[signal["symbol"]].append(
                (log_id, signal["action"], signal["entry"], self.closed_counts[signal["symbol"]]))

            def _round_price(price: float) -> float:
                return round(price, 2)
            
            # Validate and adjust SL/TP to meet broker requirements
            fallback_price = signal["entry"]
            latest_price = fallback_price
            try:
                latest_price = await broker.get_latest_price(signal["symbol"])
                logger.debug("Scanner: %s latest_price=%.4f, signal_entry=%.4f, signal_sl=%.4f", 
                             signal["symbol"], latest_price, signal["entry"], signal["stop_loss"])
            except Exception as e:
                logger.warning("Failed to get latest price for %s: %s, using signal entry %.4f", 
                               signal["symbol"], e, fallback_price)
                latest_price = fallback_price
            
            sl = signal["stop_loss"]
            tp = signal["take_profit"]
            
            # Minimum distance from base_price: max of $0.01 or 0.1% of price (handles price movement)
            min_distance = max(0.01, latest_price * 0.001)
            
            if signal["action"] == "BUY":
                min_sl = latest_price - min_distance
                max_tp = latest_price + min_distance
                if sl > min_sl:
                    sl = min_sl
                    logger.warning("Adjusted stop_loss for %s BUY: %.4f -> %.4f (min_distance=%.4f)", signal["symbol"], signal["stop_loss"], sl, min_distance)
                if tp < max_tp:
                    tp = max_tp
                    logger.warning("Adjusted take_profit for %s BUY: %.4f -> %.4f (min_distance=%.4f)", signal["symbol"], signal["take_profit"], tp, min_distance)
            else:  # SELL
                min_sl = latest_price + min_distance
                max_tp = latest_price - min_distance
                logger.debug("Scanner: %s SELL min_sl=%.4f (latest+%.4f), signal_sl=%.4f, min_tp=%.4f, signal_tp=%.4f", 
                             signal["symbol"], min_sl, min_distance, sl, max_tp, tp)
                if sl < min_sl:
                    sl = min_sl
                    logger.warning("Adjusted stop_loss for %s SELL: %.4f -> %.4f (min_distance=%.4f)", signal["symbol"], signal["stop_loss"], sl, min_distance)
                if tp > max_tp:
                    tp = max_tp
                    logger.warning("Adjusted take_profit for %s SELL: %.4f -> %.4f (min_distance=%.4f)", signal["symbol"], signal["take_profit"], tp, min_distance)
            
            # Also ensure SL/TP respect signal's ATR-based logic
            atr_mult = float(control.get("atr_mult", 1.0))
            atr = signal.get("features", {}).get("atr14", 0)
            if atr and atr > 0:
                if signal["action"] == "SELL":
                    min_atr_sl = signal["entry"] + atr_mult * atr
                    if sl < min_atr_sl:
                        sl = min_atr_sl
                        logger.debug("Scanner: %s enforcing ATR-based min SL: %.4f", signal["symbol"], sl)
                else:  # BUY
                    max_atr_sl = signal["entry"] - atr_mult * atr
                    if sl > max_atr_sl:
                        sl = max_atr_sl
                        logger.debug("Scanner: %s enforcing ATR-based max SL: %.4f", signal["symbol"], sl)
            
            sl = _round_price(sl)
            tp = _round_price(tp)
            logger.debug("Scanner: %s final sl=%.4f tp=%.4f", signal["symbol"], sl, tp)
            
            # Prevent duplicate signals for same symbol
            if signal["symbol"] in self._active_orders:
                logger.info("Scanner: %s already has active order %s, skipping", 
                            signal["symbol"], self._active_orders[signal["symbol"]])
                continue
            
            try:
                is_crypto = is_crypto_symbol(signal["symbol"])
                if is_crypto:
                    result = await broker.place_order(signal["symbol"], signal["action"], qty, order_type="market")
                    atr_val = signal.get("features", {}).get("atr14", 0.0)
                    if not atr_val or atr_val <= 0:
                        atr_val = abs(signal["entry"] - sl) if sl else signal["entry"] * 0.02
                    self._active_crypto_positions[signal["symbol"]] = {
                        "log_id": log_id,
                        "symbol": signal["symbol"],
                        "entry": signal["entry"],
                        "sl": sl,
                        "tp": tp,
                        "qty": qty,
                        "action": signal["action"],
                        "atr": atr_val,
                        "highest_price": signal["entry"],
                        "breakeven_locked": False,
                        "trailing_active": False,
                    }
                else:
                    result = await broker.place_order(signal["symbol"], signal["action"], qty,
                        order_type="market", stop_loss=sl, take_profit=tp)
                db_manager.update_trade_order_submission(log_id, result["order_id"],
                                                          f"SUBMITTED:{result['order_id']}")
                logger.warning("Scanner order submitted via %s: %s qty=%s order_id=%s (crypto=%s)",
                               control["env"], signal["symbol"], qty, result["order_id"], is_crypto)
                self._active_orders[signal["symbol"]] = result["order_id"]
            except (TradingDisabledError, UnsupportedProtectionError, BrokerError) as exc:
                db_manager.log_trade(signal["symbol"], signal["action"], signal["entry"], signal["stop_loss"],
                                     signal["take_profit"], f"BLOCKED:{exc}", signal.get("ml_approval_prob"))
                logger.error("Scanner order not submitted: %s", exc)
            except Exception as exc:
                db_manager.log_trade(signal["symbol"], signal["action"], signal["entry"], signal["stop_loss"],
                                     signal["take_profit"], f"ERROR:{exc}", signal.get("ml_approval_prob"))
                logger.exception("Unexpected scanner order routing error")

    async def _check_software_exits(self, broker, symbol: str, current_price: float) -> None:
        """Evaluate open software-managed crypto positions for Dynamic Trailing Stop, Breakeven Lock, Stop Loss, or Take Profit."""
        pos = self._active_crypto_positions.get(symbol)
        if not pos:
            return
        sl = pos.get("sl")
        tp = pos.get("tp")
        qty = pos.get("qty", 0.0)
        entry = pos.get("entry", current_price)
        log_id = pos.get("log_id")
        atr = pos.get("atr", 0.0)
        if not atr or atr <= 0:
            atr = abs(entry - sl) if sl else entry * 0.02

        if pos.get("action") == "BUY":
            # Track highest high watermark
            highest_price = max(pos.get("highest_price", entry), current_price)
            pos["highest_price"] = highest_price

            # 1. Breakeven Lock: Once price reaches entry + 1.2 * atr, lock SL to entry + 0.1 * atr
            if not pos.get("breakeven_locked", False) and current_price >= entry + 1.2 * atr:
                new_sl = max(sl if sl is not None else 0.0, entry + 0.1 * atr)
                pos["sl"] = new_sl
                pos["breakeven_locked"] = True
                logger.warning("BREAKEVEN LOCKED for %s: price=%.4f entry=%.4f locked_sl=%.4f (+1.2 ATR achieved)",
                               symbol, current_price, entry, new_sl)

            # 2. Dynamic Trailing Chandelier Stop: When price expands >= 2.0 * atr, trail at highest - 1.5 * atr
            if current_price >= entry + 2.0 * atr:
                pos["trailing_active"] = True

            if pos.get("trailing_active", False):
                trail_sl = highest_price - 1.5 * atr
                if sl is None or trail_sl > pos.get("sl", 0.0):
                    pos["sl"] = trail_sl
                    logger.warning("TRAILING STOP RATCHETED for %s: high=%.4f trail_sl=%.4f (price=%.4f)",
                                   symbol, highest_price, trail_sl, current_price)

            # 3. Climax Take Profit: If price expands >= 4.5 * atr, lock in parabolic extension
            climax_tp = entry + 4.5 * atr
            if tp is None or climax_tp < tp:
                pos["tp"] = climax_tp

            triggered_reason = None
            if pos.get("sl") is not None and current_price <= pos["sl"]:
                if pos.get("trailing_active"):
                    triggered_reason = "TRAILING_STOP"
                elif pos.get("breakeven_locked"):
                    triggered_reason = "BREAKEVEN_STOP"
                else:
                    triggered_reason = "STOP_LOSS"
            elif pos.get("tp") is not None and current_price >= pos["tp"]:
                triggered_reason = "TAKE_PROFIT"

            if triggered_reason:
                target_price = pos.get("tp") if triggered_reason == "TAKE_PROFIT" else pos.get("sl")
                logger.info("SOFTWARE %s TRIGGERED for %s: price=%.4f (entry=%.4f, target=%.4f)",
                            triggered_reason, symbol, current_price, entry, target_price)
                try:
                    # Query actual available position from broker to account for fees or fractional rounding
                    exit_qty = qty
                    try:
                        open_positions = await broker.get_positions()
                        matching = next((p for p in open_positions if p["symbol"] == symbol or normalize_crypto_symbol(p["symbol"]) == normalize_crypto_symbol(symbol)), None)
                        if matching is not None:
                            exit_qty = float(matching.get("qty", 0.0))
                        else:
                            # Position already closed on broker
                            logger.info("Position for %s already closed on broker", symbol)
                            self._active_crypto_positions.pop(symbol, None)
                            self._active_orders.pop(symbol, None)
                            db_manager.record_software_exit(log_id, f"CLOSED:{triggered_reason}", current_price, (current_price - entry) * qty, None)
                            return
                    except Exception as e:
                        logger.warning("Could not refresh open position for %s: %s; using recorded qty %.6f", symbol, e, qty)

                    if exit_qty <= 0:
                        self._active_crypto_positions.pop(symbol, None)
                        self._active_orders.pop(symbol, None)
                        return

                    result = await broker.place_order(symbol, "SELL", exit_qty, order_type="market")
                    pnl = (current_price - entry) * exit_qty
                    db_manager.record_software_exit(log_id, f"CLOSED:{triggered_reason}", current_price, pnl, result.get("order_id"))
                    self._active_crypto_positions.pop(symbol, None)
                    self._active_orders.pop(symbol, None)
                    logger.warning("Software %s exit executed for %s: qty=%.6f order_id=%s realized_pnl=%.2f",
                                   triggered_reason, symbol, exit_qty, result.get("order_id"), pnl)
                except Exception as exc:
                    logger.exception("Failed to execute software %s exit order for %s: %s", triggered_reason, symbol, exc)

    async def _get_equity(self, broker) -> float:
        """Fetch actual account equity from broker. Falls back to env var if unavailable."""
        try:
            if hasattr(broker, "get_account_equity"):
                return await broker.get_account_equity()
        except Exception:
            logger.exception("Failed to fetch equity from broker, using fallback")
        return float(os.getenv("ACCOUNT_EQUITY", "10000"))

    async def _reconcile_orders(self, broker) -> None:
        """Update paper journal rows with Alpaca parent/exit fills and realized P&L."""
        get_details = getattr(broker, "get_order_details", None)
        if get_details is None:
            return
        for row in db_manager.get_reconcilable_orders():
            try:
                details = await get_details(row["broker_order_id"])
                order_status = str(details.get("status", "unknown")).lower()
                filled_qty = details.get("filled_qty")
                entry_price = details.get("filled_avg_price")
                filled_qty = float(filled_qty) if filled_qty is not None else None
                entry_price = float(entry_price) if entry_price is not None else None
                reference_price = float(row["price"]) if row.get("price") else None
                slippage = None
                if entry_price and reference_price and reference_price > 0:
                    slippage = ((entry_price - reference_price) / reference_price
                                if row["action"] == "BUY" else
                                (reference_price - entry_price) / reference_price)

                filled_legs = [leg for leg in details.get("legs", [])
                               if str(leg.get("status", "")).lower() == "filled"
                               and leg.get("filled_avg_price") is not None
                               and leg.get("filled_qty") is not None]
                if filled_legs and entry_price and filled_qty:
                    direction = 1.0 if row["action"] == "BUY" else -1.0
                    exit_qty = sum(float(leg["filled_qty"]) for leg in filled_legs)
                    realized_pnl = sum((float(leg["filled_avg_price"]) - entry_price)
                                       * float(leg["filled_qty"]) * direction for leg in filled_legs)
                    exit_price = (sum(float(leg["filled_avg_price"]) * float(leg["filled_qty"])
                                       for leg in filled_legs) / exit_qty) if exit_qty else None
                    exit_status = "+".join(str(leg["status"]).lower() for leg in filled_legs)
                    db_manager.update_trade_order_state(row["id"], f"CLOSED:{exit_status}", filled_qty,
                        entry_price, exit_price, realized_pnl, slippage)
                    logger.info("Reconciled closed paper order %s: qty=%s realized_pnl=%.4f",
                                row["broker_order_id"], exit_qty, realized_pnl)
                    self._active_orders.pop(row["symbol"], None)
                elif order_status in {"canceled", "cancelled", "rejected", "expired"} and not (filled_qty or 0):
                    db_manager.update_trade_order_state(row["id"], f"{order_status.upper()}",
                                                        filled_qty, entry_price, slippage=slippage)
                    self._active_orders.pop(row["symbol"], None)
                elif entry_price and filled_qty:
                    db_manager.update_trade_order_state(row["id"], "ENTRY_FILLED", filled_qty,
                                                        entry_price, slippage=slippage)
            except Exception:
                logger.exception("Could not reconcile paper order %s", row["broker_order_id"])

    async def _read_sentiment(self, symbol: str, broker) -> tuple[float, bool]:
        """Fetch headlines from Alpaca or a local JSON file and cache the score per symbol."""
        if os.getenv("FINBERT_ENABLED", "true").lower() != "true":
            return float("nan"), False
        cache_seconds = max(30, int(os.getenv("NEWS_CACHE_SECONDS", "300")))
        now = time.monotonic()

        cached = self._sentiment_cache.get(symbol)
        if cached and now - cached["checked_at"] < cache_seconds:
            return cached["score"], cached["available"]

        source = os.getenv("NEWS_SOURCE", "alpaca").strip().lower()
        try:
            if source == "alpaca":
                fetch_news = getattr(broker, "get_recent_news", None)
                if fetch_news is None:
                    raise RuntimeError("Alpaca News API is unavailable for the selected broker.")
                headlines = await fetch_news(symbol, lookback_minutes=int(os.getenv("NEWS_LOOKBACK_MINUTES", "120")), limit=50)
            elif source == "file":
                path = Path(os.getenv("NEWS_HEADLINES_FILE", "data/news_headlines.json"))
                if not path.exists():
                    headlines = []
                else:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    headlines = raw.get("headlines", []) if isinstance(raw, dict) else raw
            elif source == "disabled":
                headlines = []
            else:
                raise ValueError("NEWS_SOURCE must be alpaca, file, or disabled")
            if not isinstance(headlines, list):
                raise ValueError("news source must return a list of headline strings")
            headlines = [str(item).strip() for item in headlines if str(item).strip()]
            if not headlines:
                result = (float("nan"), False)
            else:
                score = await asyncio.to_thread(get_market_sentiment, headlines[-50:])
                result = (float(score), True)
        except Exception:
            logger.exception("Sentiment scoring failed for %s; sentiment unavailable", symbol)
            result = (float("nan"), False)

        self._sentiment_cache[symbol] = {"checked_at": now, "score": result[0], "available": result[1]}
        return result


def main() -> None:
    db_manager.init_db()
    daemon = MultiSymbolDaemon(timeframe_minutes=int(os.getenv("TIMEFRAME_MINUTES", "5")))
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Trading daemon stopped by operator")


if __name__ == "__main__":
    main()