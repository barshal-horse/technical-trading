import os
import time
import pandas as pd
import numpy as np
from datetime import datetime
from collections import deque
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# ==============================================================================
# 1. CONFIGURATION & BROKER CREDENTIALS
# ==============================================================================
API_KEY = "YOUR_ANGEL_ONE_API_KEY"
CLIENT_CODE = "YOUR_CLIENT_CODE"
PASSWORD = "YOUR_PIN"
TOTP_SECRET = "YOUR_TOTP_SECRET"  # Generated during 2FA setup

# Asset Configuration
SYMBOL_TOKEN = "3045"  # Example Token for SBIN-EQ on NSE
TRADING_SYMBOL = "SBIN-EQ"
EXCHANGE = "NSE"
TIMEFRAME_MINUTES = 5

# Strategy Risk Parameters
ATR_PERIOD = 14
MA_PERIOD = 20
ATR_SL_MULT = 1.0
ATR_TP_MULT = 2.0
QUANTITY = 10  # Order Quantity

# Dynamic Data Storage (In-Memory Buffer)
ticks_buffer = []
candles_deque = deque(maxlen=200)  # Maintain last 200 closed candles

# ==============================================================================
# 2. BROKER AUTHENTICATION & ORDER EXECUTION
# ==============================================================================
smartApi = SmartConnect(api_key=API_KEY)
totp = smartApi.getTOTP(TOTP_SECRET) if hasattr(smartApi, 'getTOTP') else input("Enter TOTP: ")
data = smartApi.generateSession(CLIENT_CODE, PASSWORD, totp)
AUTH_TOKEN = data['data']['jwtToken']
FEED_TOKEN = smartApi.getfeedToken()

def place_live_order(signal_type, price, sl_price, tp_price):
    """Executes Market / Bracket Order directly via Broker API."""
    buy_sell_type = "BUY" if "BUY" in signal_type else "SELL"
    
    order_params = {
        "variety": "NORMAL",
        "tradingsymbol": TRADING_SYMBOL,
        "symboltoken": SYMBOL_TOKEN,
        "transactiontype": buy_sell_type,
        "exchange": EXCHANGE,
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "quantity": str(QUANTITY)
    }
    
    try:
        order_id = smartApi.placeOrder(order_params)
        print(f"[{datetime.now()}] ORDER EXECUTED! Type: {buy_sell_type} | ID: {order_id}")
        print(f"--> Entry: {price} | Stop-Loss: {sl_price} | Take-Profit: {tp_price}")
    except Exception as e:
        print(f"Order Execution Failed: {e}")

# ==============================================================================
# 3. SIGNAL EVALUATION ENGINE
# ==============================================================================
def process_candle_and_evaluate(df):
    """Computes technical indicators and evaluates Strategy 1: Break & Retest."""
    # Compute 20 MA and ATR
    df['MA20'] = df['Close'].rolling(window=MA_PERIOD).mean()
    
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=ATR_PERIOD).mean()
    
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    # Uptrend & Downtrend Rules
    uptrend = (curr['Close'] > df['Close'].iloc[-10]) and (curr['MA20'] > df['MA20'].iloc[-2])
    downtrend = (curr['Close'] < df['Close'].iloc[-10]) and (curr['MA20'] < df['MA20'].iloc[-2])
    
    candle_range = curr['High'] - curr['Low']
    lower_wick = min(curr['Open'], curr['Close']) - curr['Low']
    upper_wick = curr['High'] - max(curr['Open'], curr['Close'])
    
    bull_rejection = (lower_wick / candle_range) >= 0.618 if candle_range > 0 else False
    bear_rejection = (upper_wick / candle_range) >= 0.618 if candle_range > 0 else False

    # Strategy 1 Trigger
    if uptrend and curr['Close'] > curr['MA20']:
        near_ma = abs(curr['Low'] - curr['MA20']) <= (0.5 * curr['ATR'])
        if near_ma and (bull_rejection or curr['Close'] > prev['High']):
            sl = curr['Close'] - (curr['ATR'] * ATR_SL_MULT)
            tp = curr['Close'] + (curr['ATR'] * ATR_TP_MULT)
            place_live_order("BUY_STRATEGY_1", curr['Close'], round(sl, 2), round(tp, 2))

    elif downtrend and curr['Close'] < curr['MA20']:
        near_ma = abs(curr['High'] - curr['MA20']) <= (0.5 * curr['ATR'])
        if near_ma and (bear_rejection or curr['Close'] < prev['Low']):
            sl = curr['Close'] + (curr['ATR'] * ATR_SL_MULT)
            tp = curr['Close'] - (curr['ATR'] * ATR_TP_MULT)
            place_live_order("SELL_STRATEGY_1", curr['Close'], round(sl, 2), round(tp, 2))

# ==============================================================================
# 4. WEBSOCKET TICK STREAM & IN-MEMORY CANDLE AGGREGATION
# ==============================================================================
current_candle = None

def on_data(wsapp, message):
    global current_candle, ticks_buffer
    
    # Extract Last Traded Price (LTP) from binary tick payload
    ltp = message['last_traded_price'] / 100.0  # Angel One sends prices in paise
    tick_time = datetime.now()
    
    # 5-Minute Bucket Floor Determination
    minute_bucket = tick_time.minute - (tick_time.minute % TIMEFRAME_MINUTES)
    candle_timestamp = tick_time.replace(minute=minute_bucket, second=0, microsecond=0)
    
    if current_candle is None or current_candle['Timestamp'] != candle_timestamp:
        # Finalize and append previous candle if completed
        if current_candle is not None:
            candles_deque.append(current_candle)
            print(f"[{current_candle['Timestamp']}] Candle Closed | O: {current_candle['Open']} H: {current_candle['High']} L: {current_candle['Low']} C: {current_candle['Close']}")
            
            # Evaluate strategy on closed candle
            if len(candles_deque) >= 30:
                df = pd.DataFrame(list(candles_deque))
                process_candle_and_evaluate(df)

        # Start new candle
        current_candle = {
            'Timestamp': candle_timestamp,
            'Open': ltp,
            'High': ltp,
            'Low': ltp,
            'Close': ltp
        }
    else:
        # Update current candle high/low/close
        current_candle['High'] = max(current_candle['High'], ltp)
        current_candle['Low'] = min(current_candle['Low'], ltp)
        current_candle['Close'] = ltp

def on_open(wsapp):
    print("WebSocket Connected Successfully! Subscribing to market stream...")
    sws.subscribe("correlation_id", 1, [{"exchangeType": 1, "tokens": [SYMBOL_TOKEN]}])

# Init WebSocket Connection
sws = SmartWebSocketV2(AUTH_TOKEN, API_KEY, CLIENT_CODE, FEED_TOKEN)
sws.on_data = on_data
sws.on_open = on_open

if __name__ == "__main__":
    print("Starting Low-Latency Algorithmic Execution System...")
    sws.connect()