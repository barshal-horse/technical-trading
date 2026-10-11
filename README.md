# SQLite-controlled trading research system

The system is split into independent processes. `engine.py` is the event-driven daemon; `dashboard.py` is the Streamlit command center. Both processes share `data/trading.sqlite3` through `db_manager.py`, which enables SQLite WAL mode and uses short-lived connections for cross-process reads and writes.

## Components

- `db_manager.py` — control state, trade/signal log, closed candles, and model-outcome fields.
- `engine.py` — reads control state, streams normalized ticks, aggregates closed candles, evaluates SMA/ATR and wick/retest rules, writes logs, and routes orders only when the selected environment's order locks are enabled.
- `broker_adapter.py` — Alpaca Paper, Angel One, and Groww market-data adapters with lazy optional dependencies.
- `signal_engine.py` — candle aggregation, SMA(20/50/200), ATR(14), RSI, and deterministic long/short signal output.
- `ml_enhancer.py` — optional FinBERT and versioned PPO approval filter.
- `dashboard.py` — 3-second Streamlit monitor, environment/status controls, candlestick chart, and execution log.
- `train_model.py` — chronological offline PPO training, holdout evaluation, and explicit candidate promotion.

## Start

1. Use Python 3.10+ and install dependencies from `requirements.txt`.
2. Copy `.env.example` to `.env` and fill in the selected credentials. `python-dotenv` loads it in both processes. Keep `.env` out of source control.
3. Start the dashboard in one terminal: `streamlit run dashboard.py --server.address 127.0.0.1`.
4. Start the daemon separately: `python engine.py`.
5. The initial control row is `PAUSED`, `PAPER_ALPACA`, `BTC/USD`, and ATR multiplier 1.0. Configure credentials and data subscription before selecting RUNNING. For live Alpaca observation, select `LIVE_ALPACA`; the daemon uses separately named live keys and the live endpoint but still will not submit orders by default.

The UI writes desired settings to SQLite; it does not directly control the broker. The daemon rereads control state while running and at candle closes. PAUSED and STOPPED both suppress strategy evaluation and order routing.

## 24/7 Crypto Trading & Asset Class Routing

The system natively supports 24/7 cryptocurrency trading via Alpaca's `CryptoDataStream` and `CryptoHistoricalDataClient`, while keeping equities (US stocks, Angel One, Groww) fully functional for future trading:
- **Continuous 24/7 Streams**: Crypto ticks stream uninterrupted around the clock using Alpaca's websocket stream.
- **Asset Class Auto-Detection**: Symbols formatted as pairs (`BTC/USD`, `ETH/USD`, `SOL/USD`) or known coins automatically route to Alpaca crypto clients. Stock tickers (`AAPL`, `MSFT`, `SPY`) route to equity clients.
- **Engine-Managed Protective Exits**: Alpaca does not support broker-side bracket orders or simultaneous stop+limit orders for crypto. The engine actively monitors open crypto positions tick-by-tick against their Stop-Loss (1.0x ATR) and Take-Profit (2.0x ATR) thresholds and triggers immediate market exits when hit.
- **Fractional Sizing**: Crypto orders use fractional quantities (e.g. 0.005 BTC) with Alpaca's $1.00 minimum notional enforcement.
- **Long-Only Spot Enforcement**: Short selling is disabled on spot crypto; SELL signals only exit held long positions.
- **Dashboard Presets**: One-click preset switching in Streamlit between "Crypto (24/7 Active)" and "US Equities".

## Broker readiness and live order protection

`TRADING_ENABLED` and `LIVE_TRADING_ENABLED` both default to `false`. Live Alpaca order submission requires the `LIVE_ALPACA` environment, both flags enabled, the dashboard environment set to LIVE_ALPACA, and engine status RUNNING. Alpaca uses separate `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY` credentials and its live endpoint; paper uses paper credentials and the paper endpoint. [Alpaca TradingClient docs](https://alpaca.markets/sdks/python/api_reference/trading/trading-client.html)

Alpaca stock orders accept both SL and TP as an attached bracket. For crypto orders, Alpaca supports simple orders (`time_in_force=TimeInForce.GTC`), and the engine handles SL and TP in software. The Angel One and Groww adapters support market data and account/position reads, but order placement currently fails closed when SL/TP are requested: their linked protective-exit and fill-reconciliation lifecycle has not been completed. Groww documents OCO smart orders, but that alone does not reconcile partial entry fills with exit quantities. [Groww Smart Orders](https://groww.in/trade-api/docs/python-sdk/smart-orders) Do not remove these guards until entry fills, partial fills, exit attachment, cancellation, and restart reconciliation are handled.

Alpaca paper uses `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`; live uses `ALPACA_LIVE_API_KEY` / `ALPACA_LIVE_SECRET_KEY`. Groww needs a current access token or API key/secret plus the instrument exchange token for streaming. Angel One needs API key, client code, password, TOTP secret, and symbol token. SmartAPI/Groww SDKs are optional imports; install only the broker package in use.

## Logging and offline model data

Every generated signal, filter result, broker-blocked order, and closed candle is stored in SQLite. Features are serialized into `trade_logs.features_json`. Alpaca News API is the default headline source; use `NEWS_SOURCE=file` to supply a JSON list in `NEWS_HEADLINES_FILE`, or `NEWS_SOURCE=disabled` to turn news off. Headlines are cached and scored by FinBERT in a worker thread. If news or FinBERT is unavailable, sentiment is explicitly marked unavailable. The PPO observation records that state instead of disguising it as a real neutral score.

Install ML packages separately with `pip install -r requirements-ml.txt`. FinBERT is loaded once at daemon startup when `FINBERT_ENABLED=true`; set `FINBERT_LOCAL_ONLY=true` after its weights have been cached to avoid network checks during startup. The engine reconciles Alpaca bracket parent and exit-leg fills, storing entry/exit prices, filled quantity, realized P&L, and entry slippage. Run `python train_model.py --db data/trading.sqlite3` after at least 100 matured signal outcomes have accumulated. The trainer prefers completed bracket P&L normalized by entry notional and falls back to direction-adjusted 15-minute signal returns for other candidates. It reserves the latest 20% chronologically for holdout evaluation, saves the candidate separately, and promotes it to `PPO_MODEL_PATH` only when `--promote` is explicit and the holdout passes minimum approval-count, positive-expectancy, baseline-return, and drawdown checks. Rows without reconciled fills use a configurable slippage assumption; forward-return labels do not simulate bracket exits. Start `python ml_scheduler.py` for weekly offline training (Sunday 02:00 UTC by default). Qualified models may be promoted, but the scheduler never enables the PPO gate; toggle it in the dashboard after reviewing evaluation metadata. The engine fails closed if model files, evaluation metadata, the observation schema, or required market features are invalid. PPO's discrete action is not reported as a calibrated confidence probability.

The versioned PPO state is `trade-state-v2`: normalized MA distance, normalized ATR ratio, normalized RSI, FinBERT polarity, and a sentiment-available flag. The fixed bounds and scaling are recorded in model metadata and shared by the trainer and inference code. The bearish BUY veto uses `BEARISH_BUY_THRESHOLD` (default `-0.5`) only when FinBERT produced a valid sentiment score.

The dashboard's **Learning & Model** tab shows label readiness, outcome summaries, dependency/model status, and the exact training command. Training remains offline and cannot submit orders.

## Notes

- ATR uses a simple 14-candle rolling mean of true range.
- Rejection wick ratio uses `>= 0.618`; zero-range candles do not qualify.
- Trend and retest calculations use completed candles only. The engine waits for at least 21 candles before signal evaluation.
- `live_candles.timestamp` is the primary key, matching the single-active-symbol control model. Use a compound `(symbol, timestamp)` key before running simultaneous symbols.
