import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_manager
from signal_engine import add_indicators, evaluate_break_and_retest_signal

symbols = ['BTC/USD', 'ETH/USD', 'SOL/USD', 'DOGE/USD', 'AVAX/USD', 'LINK/USD']
print(f"{'SYMBOL':<10} | {'CLOSE':<12} | {'EMA20':<12} | {'EMA50':<12} | {'RSI':<6} | {'ATR':<10} | {'ACTION':<6} | {'PATTERN'}")
print("-" * 90)

for sym in symbols:
    candles = db_manager.get_recent_candles(sym, 100)
    if len(candles) < 21:
        print(f"{sym:<10} | insufficient candles ({len(candles)})")
        continue
    df = add_indicators(candles)
    sig = evaluate_break_and_retest_signal(df, symbol=sym)
    last = df.iloc[-1]
    print(f"{sym:<10} | {last.close:<12.4f} | {last.ema20:<12.4f} | {last.ema50:<12.4f} | {last.rsi14:<6.1f} | {last.atr14:<10.4f} | {sig['action']:<6} | {sig['reason']}")
