---
name: technical-engine
description: Implement closed-candle technical indicators, rejection and retest patterns, and ATR-based trade risk calculations.
---

# Technical strategy engine

Use this skill for the project's OHLC candle aggregation, indicator calculations, and rule-based signal generation.

## Candle and indicator rules

- Evaluate only completed candles. Keep candle timestamps timezone-aware and aggregate ticks by event time into fixed configured intervals (such as one or five minutes), rather than assuming callback arrival time is market time.
- Maintain a bounded in-memory candle buffer (typically `deque(maxlen=200)`). Ignore or explicitly reconcile out-of-order ticks and duplicate candle closes.
- Compute SMA(20), SMA(50), SMA(200), and ATR(14) from OHLC data when enough observations exist. Use true range `max(high-low, abs(high-prev_close), abs(low-prev_close))`; document whether ATR is a rolling mean or Wilder smoothing and keep it consistent.
- Do not emit a signal while required indicator values are missing or non-finite. Avoid lookahead: every decision must use only the current closed candle and earlier data.

## Pattern and signal rules

- Rejection is based on wick divided by full candle range. A bullish lower-wick rejection or bearish upper-wick rejection qualifies at `>= 0.618`; zero-range candles do not qualify. A body-size test may be included only as a separately named rule, not confused with wick ratio.
- A structure retest qualifies when distance to the configured 20-period average or a documented swing support/resistance level is at most `0.5 * ATR`.
- For break-and-retest signals, define the trend and break conditions explicitly (for example, close above SMA20 plus higher highs for long setups; mirrored conditions for shorts). Keep buy and sell logic symmetric where strategy rules allow.
- Return a structured signal with action, symbol, closed-candle timestamp, entry reference, stop, target, reason/features, and strategy version. Return an explicit no-signal result when conditions fail.

## Risk calculations

Use the strategy's configured risk model consistently. The project specification describes a stop beyond the relevant swing extremity by one ATR and a target two ATR from entry; simpler examples use entry minus/plus one ATR. Treat these as distinct configurable modes and do not silently substitute one for the other. Validate long/short price ordering, positive finite ATR, tick-size rounding, and minimum distance rules. Signal calculations are not a substitute for broker-side risk controls or position-size limits.

Do not place orders from this module. Keep it deterministic and testable with supplied candles; do not add unrequested tests or execution behavior.
