"""Streamlit command center for multi-symbol trading. Run separately with ``streamlit run dashboard.py``."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path

import plotly.graph_objects as go
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import db_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
import time as _time
logging.Formatter.converter = _time.gmtime
st.set_page_config(page_title="⚡ Algorithmic Trading Command Center", layout="wide")
st_autorefresh(interval=3000, key="command_center_refresh")
st.title("⚡ Algorithmic Trading Command Center")
st.caption("Multi-symbol scanner and paper trading. Dashboard and engine share local SQLite WAL.")

try:
    settings = db_manager.get_control_settings()
except Exception as exc:
    st.error(f"Database unavailable: {exc}")
    st.stop()

_model_path = Path(os.getenv("PPO_MODEL_PATH", "models/ppo_trade_filter"))
_metadata_path = _model_path.with_suffix(".metadata.json")
_model_file = Path(str(_model_path) + ".zip")
try:
    _model_metadata = json.loads(_metadata_path.read_text(encoding="utf-8")) if _metadata_path.exists() else {}
except (OSError, json.JSONDecodeError):
    _model_metadata = {}
_ppo_ready = _model_metadata.get("approved_for_paper") is True and _model_file.exists()

with st.sidebar:
    st.header("Control Panel")
    with st.form("engine_control", clear_on_submit=False):
        environments = ["PAPER_ALPACA", "LIVE_ALPACA", "LIVE_ANGEL_ONE", "LIVE_GROWW"]
        statuses = ["RUNNING", "PAUSED", "STOPPED"]
        env = st.radio("Active Environment", environments,
                       index=environments.index(settings["env"]) if settings["env"] in environments else 0)
        status = st.selectbox("Engine Status", statuses,
                              index=statuses.index(settings["status"]) if settings["status"] in statuses else 1)
        symbol = st.text_input("Primary Symbol", value=settings["symbol"], placeholder="AAPL")
        atr_mult = st.slider("ATR SL Multiplier", min_value=0.5, max_value=3.0,
                             value=float(settings["atr_mult"]), step=0.1)
        ml_gate_enabled = st.checkbox("Require PPO model approval", value=bool(settings.get("ml_gate_enabled", 0)),
                                      disabled=not _ppo_ready and not bool(settings.get("ml_gate_enabled", 0)))
        st.subheader("Scanner & Risk")
        scan_enabled = st.checkbox("Enable scanner", value=bool(settings.get("scan_enabled", 1)))
        scan_interval = st.number_input("Scan interval (min)", min_value=1, max_value=60, value=int(settings.get("scan_interval_min", 5)))
        max_positions = st.number_input("Max positions", min_value=1, max_value=20, value=int(settings.get("max_positions", 5)))
        max_correlated = st.number_input("Max per sector", min_value=1, max_value=10, value=int(settings.get("max_correlated_positions", 2)))
        min_score = st.slider("Min signal score", min_value=0.0, max_value=1.0, value=float(settings.get("min_signal_score", 0.6)), step=0.05)
        kelly_frac = st.slider("Kelly fraction", min_value=0.0, max_value=1.0, value=float(settings.get("kelly_fraction", 0.25)), step=0.05)
        max_pos_pct = st.slider("Max position % equity", min_value=1.0, max_value=50.0, value=float(settings.get("max_position_pct", 10.0)), step=1.0)

        preset = st.selectbox("Watchlist Preset", ["Current", "Crypto (24/7 Active)", "US Equities"], index=0)
        current_wl = json.loads(settings.get("watchlist_json", '["BTC/USD","ETH/USD","SOL/USD"]'))
        if preset == "Crypto (24/7 Active)":
            default_wl = "BTC/USD, ETH/USD, SOL/USD, DOGE/USD, AVAX/USD, LINK/USD"
        elif preset == "US Equities":
            default_wl = "AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, SPY, QQQ, IWM"
        else:
            default_wl = ", ".join(current_wl)

        watchlist_str = st.text_area("Watchlist (comma separated)", value=default_wl, height=80)
        if not _ppo_ready:
            st.caption("PPO approval stays unavailable until a model passes holdout evaluation.")
        saved = st.form_submit_button("Save Settings", type="primary", width="stretch")
    if saved:
        try:
            watchlist = json.dumps([s.strip().upper() for s in watchlist_str.split(",") if s.strip()])
            db_manager.update_control_settings(
                status, env, symbol, atr_mult, ml_gate_enabled,
                watchlist_json=watchlist,
                max_positions=max_positions,
                max_correlated_positions=max_correlated,
                scan_enabled=scan_enabled,
                scan_interval_min=scan_interval,
                min_signal_score=min_score,
                kelly_fraction=kelly_frac,
                max_position_pct=max_pos_pct
            )
            st.success("Settings saved.")
            settings = db_manager.get_control_settings()
        except Exception as exc:
            st.error(f"Could not save settings: {exc}")
    st.divider()
    st.metric("Engine", settings["status"])
    st.metric("Environment", settings["env"])
    if settings["env"].startswith("LIVE_"):
        armed = os.getenv("TRADING_ENABLED", "false").lower() == "true" and os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        if armed and settings["env"] == "LIVE_ALPACA" and settings["status"] == "RUNNING":
            st.error("LIVE ALPACA ORDERING IS ARMED while the engine is RUNNING.")
        else:
            st.warning("Live environment selected. Order submission is locked unless both trading flags are enabled.")

try:
    snapshot = db_manager.get_dashboard_snapshot(100)
    watchlist = snapshot["watchlist"]
    logs = snapshot["logs"]
    daily_pnl = snapshot["daily_pnl"]
except Exception as exc:
    st.error(f"Could not load dashboard data: {exc}")
    st.stop()

tab_chart, tab_scanner, tab_portfolio, tab_logs, tab_learning = st.tabs(["Live Chart", "Scanner", "Portfolio", "Execution Logs", "Learning & Model"])

with tab_chart:
    selected_symbol = st.selectbox("Symbol", watchlist, index=0)
    st.subheader(f"{selected_symbol} · Closed candles")
    try:
        candles = db_manager.get_recent_candles(selected_symbol, 500)
        if candles.empty:
            st.info("No closed candles recorded yet for this symbol.")
        else:
            candles["timestamp"] = pd.to_datetime(candles["timestamp"], errors="coerce", utc=True)
            candles = candles.dropna(subset=["timestamp"])
            fig = go.Figure()
            fig.add_trace(go.Candlestick(x=candles.timestamp, open=candles.open, high=candles.high,
                low=candles.low, close=candles.close, name="OHLC"))
            fig.add_trace(go.Scatter(x=candles.timestamp, y=candles.ma20, mode="lines",
                name="SMA 20", line={"color": "#f59e0b", "width": 1.5}))
            fig.update_layout(height=650, xaxis_rangeslider_visible=False, margin={"l": 10, "r": 10, "t": 20, "b": 10},
                              yaxis_title="Price", template="plotly_dark")
            st.plotly_chart(fig, width="stretch")
            latest = candles.iloc[-1]
            a, b, c = st.columns(3)
            a.metric("Last close", f"{latest.close:.4f}")
            b.metric("SMA 20", "—" if pd.isna(latest.ma20) else f"{latest.ma20:.4f}")
            c.metric("ATR 14", "—" if pd.isna(latest.atr) else f"{latest.atr:.4f}")
    except Exception as exc:
        st.error(f"Could not load candles: {exc}")

with tab_scanner:
    st.subheader("Signal Scanner Results")
    st.caption("Signals generated by the engine are logged below. Scanner runs every N minutes based on control settings.")
    try:
        signal_logs = logs[logs["action"].isin(["BUY", "SELL"])].copy()
        if signal_logs.empty:
            st.info("No signals recorded yet. Start the engine to begin scanning.")
        else:
            cols = ["timestamp", "symbol", "action", "price", "sl", "tp", "status", "ml_score"]
            display = signal_logs[cols].copy()
            display = display.rename(columns={"ml_score": "ML Prob"})
            st.dataframe(display, width="stretch", hide_index=True)
    except Exception as exc:
        st.error(f"Could not load scanner results: {exc}")

with tab_portfolio:
    st.subheader("Portfolio Overview")
    a, b, c = st.columns(3)
    a.metric("Daily Realized P&L", f"${daily_pnl:.2f}")
    try:
        positions = db_manager.get_portfolio_positions()
        b.metric("Open Positions", f"{len(positions)}")
        c.metric("Watchlist Symbols", f"{len(watchlist)}")
        if not positions.empty:
            st.dataframe(positions, width="stretch", hide_index=True)
        else:
            st.info("No open positions.")
    except Exception as exc:
        st.error(f"Could not load portfolio: {exc}")

with tab_logs:
    st.subheader("Recent trade and signal events")
    if logs.empty:
        st.info("No signals or execution events recorded yet.")
    else:
        st.dataframe(logs, width="stretch", hide_index=True,
                     column_config={"ml_score": st.column_config.NumberColumn("ML score", format="%.3f"),
                                    "price": st.column_config.NumberColumn("Price", format="%.4f"),
                                    "sl": st.column_config.NumberColumn("Stop loss", format="%.4f"),
                                    "tp": st.column_config.NumberColumn("Take profit", format="%.4f")})
        st.download_button("Download logs as CSV", logs.to_csv(index=False).encode("utf-8"),
                           file_name="trade_logs.csv", mime="text/csv")

with tab_learning:
    st.subheader("Paper data and model readiness")
    try:
        training_rows = db_manager.get_trade_logs(5000)
        signal_rows = training_rows[
            training_rows["action"].isin(["BUY", "SELL"])
            & training_rows["features_json"].fillna("{}").ne("{}")
        ].copy()
        forward_returns = pd.to_numeric(signal_rows["forward_return_15m"], errors="coerce")
        realized_pnl = pd.to_numeric(signal_rows["realized_pnl"], errors="coerce")
        entry_notional = (pd.to_numeric(signal_rows["entry_fill_price"], errors="coerce")
                          * pd.to_numeric(signal_rows["filled_qty"], errors="coerce"))
        has_realized = realized_pnl.notna() & entry_notional.gt(0)
        signal_rows["training_return"] = forward_returns.where(~has_realized, realized_pnl / entry_notional)
        labeled = signal_rows[signal_rows["training_return"].notna()]
        closed_fills = signal_rows[has_realized]
        required_rows = 100
        a, b, c, d, e = st.columns(5)
        a.metric("Feature-bearing signals", f"{len(signal_rows):,}")
        b.metric("Matured labels", f"{len(labeled):,}", f"{max(0, required_rows-len(labeled)):,} to training minimum")
        c.metric("Closed bracket fills", f"{len(closed_fills):,}",
                 f"Realized P&L ${realized_pnl[has_realized].sum():.2f}" if len(closed_fills) else None)
        d.metric("FinBERT installed", "Yes" if importlib.util.find_spec("transformers") and importlib.util.find_spec("torch") else "No")
        e.metric("PPO gate", "Enabled" if settings.get("ml_gate_enabled", 0) else "Disabled")

        st.write(f"Deployed paper filter: **{'qualified model present' if _ppo_ready else 'not ready'}**")
        if _model_metadata:
            evaluation = _model_metadata.get("evaluation", {})
            st.caption(f"Schema: {_model_metadata.get('observation_schema', 'unknown')} · "
                       f"Holdout rows: {evaluation.get('holdout_rows', '—')} · "
                       f"Approved signals: {evaluation.get('approved_signals', '—')}")

        source = os.getenv("NEWS_SOURCE", "alpaca")
        st.write(f"News source: **{source}** · FinBERT model: **{os.getenv('FINBERT_MODEL', 'ProsusAI/finbert')}**")
        if not importlib.util.find_spec("transformers") or not importlib.util.find_spec("torch"):
            st.info("Install `requirements-ml.txt` to enable FinBERT and PPO inference/training.")

        if len(labeled):
            outcomes = pd.to_numeric(labeled["training_return"], errors="coerce").dropna()
            wins = int((outcomes > 0).sum())
            x, y, z = st.columns(3)
            x.metric("Mean training outcome", f"{outcomes.mean():.3%}")
            y.metric("Median training outcome", f"{outcomes.median():.3%}")
            z.metric("Positive outcomes", f"{wins / len(outcomes):.1%}")
            st.caption("Completed bracket P&L is normalized by entry notional; other labels use direction-adjusted 15-minute forward returns.")

        if len(labeled) < required_rows:
            st.warning(f"PPO training stays unavailable until there are at least {required_rows} matured, feature-bearing paper signals. Current count: {len(labeled)}.")
        else:
            st.success("Minimum label count reached. Training still requires a chronological holdout and a passing evaluation.")
            st.code("python train_model.py --db data/trading.sqlite3 --promote", language="powershell")
        st.caption("Weekly offline schedule: Sunday 02:00 UTC. Start it separately with `python ml_scheduler.py`; it never enables the PPO gate or submits orders.")
        st.caption("A PPO action is not a calibrated probability. Paper promotion is separate from live trading and does not enable live order submission.")
    except Exception as exc:
        st.error(f"Could not load model-training status: {exc}")