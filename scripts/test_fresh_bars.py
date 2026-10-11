import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

client = CryptoHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
now = datetime.now(timezone.utc)

for sym in ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD"]:
    lookback = 200 * 5  # 1000 minutes
    req = CryptoBarsRequest(
        symbol_or_symbols=sym,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=now - timedelta(minutes=lookback),
        end=now,
        limit=1000
    )
    res = client.get_crypto_bars(req)
    df = res.df.reset_index()
    last = df.iloc[-1]
    print(f"{sym:<10}: {len(df):>3} bars | Earliest: {df.iloc[0]['timestamp']} | Latest: {last['timestamp']} | Close: {last['close']}")
