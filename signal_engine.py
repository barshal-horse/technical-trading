"""Closed-candle aggregation and deterministic technical signal generation."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class Candle:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class CandleAggregator:
    def __init__(self, symbol: str, timeframe_minutes: int = 1, max_candles: int = 200):
        if timeframe_minutes <= 0:
            raise ValueError("timeframe_minutes must be positive")
        self.symbol, self.timeframe_minutes = symbol, timeframe_minutes
        self.closed: deque[Candle] = deque(maxlen=max_candles)
        self.current: Candle | None = None

    def seed_closed(self, rows: list[dict[str, Any]]) -> None:
        """Load already closed historical candles before consuming live ticks."""
        values = []
        for row in rows:
            stamp = row["timestamp"]
            if not isinstance(stamp, datetime):
                stamp = pd.Timestamp(stamp).to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            values.append(Candle(stamp.astimezone(timezone.utc), self.symbol,
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row.get("volume", 0.0))))
        self.closed.clear()
        self.closed.extend(sorted(values, key=lambda candle: candle.timestamp)[-self.closed.maxlen:])

    def _bucket(self, stamp: datetime) -> datetime:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc)
        minute = stamp.minute - stamp.minute % self.timeframe_minutes
        return stamp.replace(minute=minute, second=0, microsecond=0)

    def update_tick(self, price: float, timestamp: datetime | None = None, volume: float = 0.0) -> Candle | None:
        if not np.isfinite(price) or price <= 0:
            raise ValueError("price must be a finite positive number")
        bucket = self._bucket(timestamp or datetime.now(timezone.utc))
        if self.current is None:
            self.current = Candle(bucket, self.symbol, price, price, price, price, volume)
            return None
        if bucket < self.current.timestamp:
            return None  # Ignore stale/out-of-order ticks; caller can reconcile upstream.
        if bucket == self.current.timestamp:
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            self.current.volume += volume
            return None
        closed = self.current
        self.closed.append(closed)
        self.current = Candle(bucket, self.symbol, price, price, price, price, volume)
        return closed

    def dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(c) for c in self.closed])


def add_indicators(candles: pd.DataFrame) -> pd.DataFrame:
    required = {"open", "high", "low", "close"}
    if not required.issubset(candles.columns):
        raise ValueError(f"candles must contain {sorted(required)}")
    df = candles.copy()
    for col, period in (("sma20", 20), ("sma50", 50), ("sma200", 200)):
        df[col] = df["close"].rolling(period, min_periods=period).mean()
    # Exponential moving averages for responsive crypto trend detection
    for col, period in (("ema20", 20), ("ema50", 50), ("ema200", 200)):
        df[col] = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    # Volume rolling average and ratio
    if "volume" in df.columns:
        vol_mean = df["volume"].rolling(20, min_periods=5).mean()
        df["vol_ratio"] = (df["volume"] / vol_mean.replace(0, np.nan)).fillna(1.0)
    else:
        df["vol_ratio"] = 1.0

    previous_close = df["close"].shift(1)
    tr = pd.concat([(df["high"] - df["low"]), (df["high"] - previous_close).abs(),
                    (df["low"] - previous_close).abs()], axis=1).max(axis=1)
    # Wilder's smoothing: EWMA with alpha = 1/period, adjust=False
    atr_alpha = 1.0 / 14
    df["atr14"] = tr.ewm(alpha=atr_alpha, adjust=False, min_periods=14).mean()
    delta = df["close"].diff()
    avg_gain = delta.clip(lower=0).ewm(alpha=atr_alpha, adjust=False, min_periods=14).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(alpha=atr_alpha, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)
    df.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100.0
    return df


def rejection_ratios(candle: Any) -> tuple[float, float]:
    candle_range = float(candle.high - candle.low)
    if candle_range <= 0:
        return 0.0, 0.0
    lower = (min(candle.open, candle.close) - candle.low) / candle_range
    upper = (candle.high - max(candle.open, candle.close)) / candle_range
    return max(0.0, lower), max(0.0, upper)


def evaluate_break_and_retest_signal(candle_df: pd.DataFrame, symbol: str = "UNKNOWN",
                                     risk_mode: str = "entry_atr") -> dict[str, Any]:
    """Return a BUY/SELL/FLAT signal. Stop modes: entry_atr or swing_atr."""
    if risk_mode not in ("entry_atr", "swing_atr"):
        raise ValueError("risk_mode must be 'entry_atr' or 'swing_atr'")
    empty = {"action": "FLAT", "symbol": symbol, "entry": None, "stop_loss": None,
             "take_profit": None, "timestamp": None, "features": {}, "reason": "insufficient data"}
    if candle_df is None or len(candle_df) < 21:
        return empty
    df = add_indicators(candle_df).dropna(subset=["sma20", "atr14"])
    if len(df) < 2:
        return empty
    curr, prev = df.iloc[-1], df.iloc[-2]
    atr, entry = float(curr.atr14), float(curr.close)
    if not np.isfinite(atr) or atr <= 0:
        empty["reason"] = "invalid ATR"
        return empty

    is_crypto = "/" in str(symbol) or str(symbol).endswith("USD") or str(symbol).endswith("USDT")
    low_ratio, high_ratio = rejection_ratios(curr)
    near_long = abs(float(curr.low) - float(curr.sma20)) <= 0.5 * atr
    near_short = abs(float(curr.high) - float(curr.sma20)) <= 0.5 * atr

    # RSI momentum bounds
    rsi_val = float(curr.rsi14) if pd.notna(curr.get("rsi14")) else 50.0
    rsi_long_ok = 42.0 <= rsi_val <= 75.0
    rsi_short_ok = 25.0 <= rsi_val <= 58.0

    # Trend filter: if SMA50/EMA50 available, align direction with intermediate trend
    sma50_val = float(curr.sma50) if pd.notna(curr.get("sma50")) else None
    trend_up = (entry >= sma50_val) if sma50_val is not None else True
    trend_down = (entry <= sma50_val) if sma50_val is not None else True

    # Volume filter
    vol_ratio = float(curr.get("vol_ratio", 1.0)) if pd.notna(curr.get("vol_ratio")) else 1.0
    vol_ok = vol_ratio >= 0.8  # Ensure not dead liquidity

    long_ok = entry > float(curr.sma20) and near_long and (low_ratio >= 0.618 or entry > float(prev.high)) and rsi_long_ok and trend_up and vol_ok
    # Crypto spot does not support short selling: suppress short entries for crypto
    short_ok = (not is_crypto) and entry < float(curr.sma20) and near_short and (high_ratio >= 0.618 or entry < float(prev.low)) and rsi_short_ok and trend_down and vol_ok

    action = "BUY" if long_ok else "SELL" if short_ok else "FLAT"
    stamp = curr.get("timestamp", curr.get("Timestamp"))
    stamp = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp) if stamp is not None else None
    result: dict[str, Any] = {"action": action, "symbol": symbol, "entry": entry if action != "FLAT" else None,
                              "stop_loss": None, "take_profit": None, "timestamp": stamp,
                              "strategy_version": "crypto-break-retest-v2" if is_crypto else "break-retest-v1",
                              "reason": "conditions met" if action != "FLAT" else "no setup",
                              "features": {"sma20": float(curr.sma20), "sma50": _finite(curr.sma50),
                                           "sma200": _finite(curr.sma200), "atr14": atr,
                                           "ma_distance_ratio": (entry - float(curr.sma20)) / atr,
                                           "atr_ratio": atr / entry, "rsi14": _finite(curr.rsi14),
                                           "lower_wick_ratio": low_ratio, "upper_wick_ratio": high_ratio,
                                           "vol_ratio": vol_ratio}}
    if action == "BUY":
        sl = (float(curr.low) - atr) if risk_mode == "swing_atr" else (entry - atr)
        # Asymmetric risk/reward: 2.0x ATR profit target
        result.update(stop_loss=sl, take_profit=entry + 2 * atr)
    elif action == "SELL":
        sl = (float(curr.high) + atr) if risk_mode == "swing_atr" else (entry + atr)
        result.update(stop_loss=sl, take_profit=entry - 2 * atr)
    return result


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
        return x if np.isfinite(x) else None
    except (TypeError, ValueError):
        return None
