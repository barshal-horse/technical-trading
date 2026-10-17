"""Thread-safe SQLite interface shared by the engine and Streamlit process."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = Path(os.getenv("TRADE_DB_PATH", "data/trading.sqlite3"))
_LOCK = threading.RLock()
_INITIALIZED = False

DEFAULT_CRYPTO_WATCHLIST = json.dumps([
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD",
    "DOGE/USD", "LTC/USD", "BCH/USD", "UNI/USD", "AAVE/USD", "SHIB/USD"
])
DEFAULT_STOCK_WATCHLIST = json.dumps([
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "SPY", "QQQ", "IWM"
])
DEFAULT_WATCHLIST = DEFAULT_CRYPTO_WATCHLIST


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS bot_control (
            id INT PRIMARY KEY, status TEXT NOT NULL, env TEXT NOT NULL,
            symbol TEXT NOT NULL, atr_mult REAL NOT NULL,
            ml_gate_enabled INTEGER NOT NULL DEFAULT 0,
            watchlist_json TEXT NOT NULL DEFAULT '["BTC/USD","ETH/USD","SOL/USD","DOGE/USD","AVAX/USD","LINK/USD"]',
            max_positions INTEGER NOT NULL DEFAULT 5,
            max_correlated_positions INTEGER NOT NULL DEFAULT 2,
            scan_enabled INTEGER NOT NULL DEFAULT 1,
            scan_interval_min INTEGER NOT NULL DEFAULT 5,
            min_signal_score REAL NOT NULL DEFAULT 0.6,
            kelly_fraction REAL NOT NULL DEFAULT 0.25,
            max_position_pct REAL NOT NULL DEFAULT 10.0
        );
        CREATE TABLE IF NOT EXISTS trade_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT NOT NULL, action TEXT NOT NULL, price REAL,
            sl REAL, tp REAL, status TEXT NOT NULL, ml_score REAL,
            features_json TEXT NOT NULL DEFAULT '{}',
            forward_return_15m REAL, realized_pnl REAL, slippage REAL,
            broker_order_id TEXT, filled_qty REAL, entry_fill_price REAL, exit_fill_price REAL,
            portfolio_id TEXT, position_size_pct REAL
        );
        CREATE TABLE IF NOT EXISTS live_candles (
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            ma20 REAL,
            atr REAL,
            volume REAL DEFAULT 0.0,
            PRIMARY KEY (symbol, timestamp)
        );
        CREATE INDEX IF NOT EXISTS idx_live_candles_symbol_time ON live_candles(symbol, timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_trade_logs_symbol_time ON trade_logs(symbol, timestamp DESC);
        """)
        initial_status = 'RUNNING' if (os.getenv('TRADING_ENABLED', 'false').lower() == 'true' or os.getenv('CI') or os.getenv('GITHUB_ACTIONS')) else 'PAUSED'
        conn.execute("INSERT OR IGNORE INTO bot_control(id,status,env,symbol,atr_mult) VALUES(1, ?, 'PAPER_ALPACA', 'BTC/USD', 1.0)", (initial_status,))
        control_columns = {row[1] for row in conn.execute("PRAGMA table_info(bot_control)")}
        for col, decl in (
            ("ml_gate_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("watchlist_json", f"TEXT NOT NULL DEFAULT '{DEFAULT_WATCHLIST}'"),
            ("max_positions", "INTEGER NOT NULL DEFAULT 15"),
            ("max_correlated_positions", "INTEGER NOT NULL DEFAULT 15"),
            ("scan_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("scan_interval_min", "INTEGER NOT NULL DEFAULT 5"),
            ("min_signal_score", "REAL NOT NULL DEFAULT 0.55"),
            ("kelly_fraction", "REAL NOT NULL DEFAULT 0.25"),
            ("max_position_pct", "REAL NOT NULL DEFAULT 15.0"),
        ):
            if col not in control_columns:
                conn.execute(f"ALTER TABLE bot_control ADD COLUMN {col} {decl}")
        candle_columns = {row[1] for row in conn.execute("PRAGMA table_info(live_candles)")}
        if "volume" not in candle_columns:
            conn.execute("ALTER TABLE live_candles ADD COLUMN volume REAL DEFAULT 0.0")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trade_logs)")}
        for name, declaration in (("features_json", "TEXT NOT NULL DEFAULT '{}'"),
                                  ("forward_return_15m", "REAL"),
                                  ("realized_pnl", "REAL"), ("slippage", "REAL"),
                                  ("broker_order_id", "TEXT"), ("filled_qty", "REAL"),
                                  ("entry_fill_price", "REAL"), ("exit_fill_price", "REAL"),
                                  ("portfolio_id", "TEXT"), ("position_size_pct", "REAL")):
            if name not in existing:
                conn.execute(f"ALTER TABLE trade_logs ADD COLUMN {name} {declaration}")
    _INITIALIZED = True


def get_control_settings() -> dict:
    init_db()
    with _LOCK, _connect() as conn:
        row = conn.execute("""
            SELECT id,status,env,symbol,atr_mult,ml_gate_enabled,
                   watchlist_json,max_positions,max_correlated_positions,
                   scan_enabled,scan_interval_min,min_signal_score,
                   kelly_fraction,max_position_pct
            FROM bot_control WHERE id=1
        """).fetchone()
        if row:
            return dict(row)
        return {
            "id": 1, "status": "PAUSED", "env": "PAPER_ALPACA", "symbol": "AAPL", "atr_mult": 1.0,
            "ml_gate_enabled": 0, "watchlist_json": DEFAULT_WATCHLIST, "max_positions": 15,
            "max_correlated_positions": 15, "scan_enabled": 1, "scan_interval_min": 5,
            "min_signal_score": 0.55, "kelly_fraction": 0.25, "max_position_pct": 15.0
        }


def update_control_settings(status: str, env: str, symbol: str, atr_mult: float,
                            ml_gate_enabled: bool | None = None,
                            watchlist_json: str | None = None,
                            max_positions: int | None = None,
                            max_correlated_positions: int | None = None,
                            scan_enabled: bool | None = None,
                            scan_interval_min: int | None = None,
                            min_signal_score: float | None = None,
                            kelly_fraction: float | None = None,
                            max_position_pct: float | None = None) -> None:
    status, env, symbol = status.upper().strip(), env.upper().strip(), symbol.strip().upper()
    if status not in {"RUNNING", "PAUSED", "STOPPED"}:
        raise ValueError("status must be RUNNING, PAUSED, or STOPPED")
    if env not in {"PAPER_ALPACA", "LIVE_ALPACA", "LIVE_ANGEL_ONE", "LIVE_GROWW"}:
        raise ValueError("unsupported broker environment")
    if not symbol or not 0.5 <= float(atr_mult) <= 3.0:
        raise ValueError("symbol is required and atr_mult must be between 0.5 and 3.0")
    init_db()
    with _LOCK, _connect() as conn:
        fields = ["status=?", "env=?", "symbol=?", "atr_mult=?"]
        vals = [status, env, symbol, float(atr_mult)]
        if ml_gate_enabled is not None:
            fields.append("ml_gate_enabled=?"); vals.append(int(bool(ml_gate_enabled)))
        if watchlist_json is not None:
            fields.append("watchlist_json=?"); vals.append(watchlist_json)
        if max_positions is not None:
            fields.append("max_positions=?"); vals.append(int(max_positions))
        if max_correlated_positions is not None:
            fields.append("max_correlated_positions=?"); vals.append(int(max_correlated_positions))
        if scan_enabled is not None:
            fields.append("scan_enabled=?"); vals.append(int(bool(scan_enabled)))
        if scan_interval_min is not None:
            fields.append("scan_interval_min=?"); vals.append(int(scan_interval_min))
        if min_signal_score is not None:
            fields.append("min_signal_score=?"); vals.append(float(min_signal_score))
        if kelly_fraction is not None:
            fields.append("kelly_fraction=?"); vals.append(float(kelly_fraction))
        if max_position_pct is not None:
            fields.append("max_position_pct=?"); vals.append(float(max_position_pct))
        vals.append(1)
        conn.execute(f"UPDATE bot_control SET {', '.join(fields)} WHERE id=?", vals)


def get_watchlist() -> list[str]:
    init_db()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT watchlist_json FROM bot_control WHERE id=1").fetchone()
        if row and row["watchlist_json"]:
            return json.loads(row["watchlist_json"])
        return json.loads(DEFAULT_WATCHLIST)


def update_watchlist(symbols: list[str]) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE bot_control SET watchlist_json=? WHERE id=1",
                     (json.dumps([s.strip().upper() for s in symbols if s.strip()]),))


def get_risk_params() -> dict:
    init_db()
    with _LOCK, _connect() as conn:
        row = conn.execute("""
            SELECT max_positions, max_correlated_positions, scan_enabled,
                   scan_interval_min, min_signal_score, kelly_fraction, max_position_pct
            FROM bot_control WHERE id=1
        """).fetchone()
        if row:
            return dict(row)
        return {
            "max_positions": 5, "max_correlated_positions": 2, "scan_enabled": 1,
            "scan_interval_min": 5, "min_signal_score": 0.6, "kelly_fraction": 0.25,
            "max_position_pct": 10.0
        }


def _sanitize_for_json(val: Any) -> Any:
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, dict):
        return {k: _sanitize_for_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_sanitize_for_json(v) for v in val]
    return val


def log_trade(symbol: str, action: str, price: float | None, sl: float | None,
              tp: float | None, status: str, ml_score: float | None,
              features: dict | None = None, forward_return_15m: float | None = None,
              realized_pnl: float | None = None, slippage: float | None = None,
              portfolio_id: str | None = None, position_size_pct: float | None = None) -> int:
    init_db()
    safe_features = _sanitize_for_json(features or {})
    with _LOCK, _connect() as conn:
        cur = conn.execute("""INSERT INTO trade_logs
            (symbol,action,price,sl,tp,status,ml_score,features_json,forward_return_15m,realized_pnl,slippage,
             portfolio_id,position_size_pct)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, action, price, sl, tp, status, ml_score,
             json.dumps(safe_features), forward_return_15m, realized_pnl, slippage,
             portfolio_id, position_size_pct))
        return int(cur.lastrowid)


def update_forward_return(log_id: int, value: float) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE trade_logs SET forward_return_15m=? WHERE id=?", (float(value), int(log_id)))


def update_trade_order_submission(log_id: int, order_id: str, status: str) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE trade_logs SET broker_order_id=?,status=? WHERE id=?",
                     (str(order_id), str(status), int(log_id)))


def get_reconcilable_orders(limit: int = 100) -> list[dict]:
    init_db()
    limit = max(1, min(int(limit), 500))
    with _LOCK, _connect() as conn:
        rows = conn.execute("""SELECT id,symbol,action,price,broker_order_id,status
            FROM trade_logs WHERE broker_order_id IS NOT NULL
            AND status NOT LIKE 'CLOSED:%' AND status NOT LIKE 'CANCELLED:%'
            AND status NOT LIKE 'REJECTED:%' AND status NOT LIKE 'EXPIRED:%'
            AND status NOT IN ('CANCELED','CANCELLED','REJECTED','EXPIRED')
            ORDER BY id ASC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]


def update_trade_order_state(log_id: int, status: str, filled_qty: float | None = None,
                             entry_fill_price: float | None = None, exit_fill_price: float | None = None,
                             realized_pnl: float | None = None, slippage: float | None = None) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("""UPDATE trade_logs SET status=?,
            filled_qty=COALESCE(?,filled_qty), entry_fill_price=COALESCE(?,entry_fill_price),
            exit_fill_price=COALESCE(?,exit_fill_price), realized_pnl=COALESCE(?,realized_pnl),
            slippage=COALESCE(?,slippage) WHERE id=?""",
            (str(status), filled_qty, entry_fill_price, exit_fill_price, realized_pnl, slippage, int(log_id)))


def get_active_crypto_positions() -> list[dict]:
    """Return open/submitted crypto trades that need active software SL/TP monitoring."""
    init_db()
    with _LOCK, _connect() as conn:
        rows = conn.execute("""
            SELECT id, symbol, action, price, sl, tp, broker_order_id,
                   filled_qty, entry_fill_price, status
            FROM trade_logs
            WHERE (status LIKE 'SUBMITTED%' OR status LIKE 'ENTRY_FILLED%')
              AND (symbol LIKE '%/%' OR symbol LIKE '%USD' OR symbol LIKE '%USDT')
              AND sl IS NOT NULL AND tp IS NOT NULL
            ORDER BY id DESC
        """).fetchall()
        return [dict(row) for row in rows]


def record_software_exit(log_id: int | None, exit_status: str, exit_price: float,
                         realized_pnl: float, exit_order_id: str | None = None) -> None:
    """Record software stop-loss or take-profit execution."""
    init_db()
    if log_id is None:
        return
    with _LOCK, _connect() as conn:
        conn.execute("""
            UPDATE trade_logs
            SET status = ?, exit_fill_price = ?, realized_pnl = ?,
                broker_order_id = CASE WHEN ? IS NOT NULL THEN broker_order_id || ':' || ? ELSE broker_order_id END
            WHERE id = ?
        """, (str(exit_status), float(exit_price), float(realized_pnl), exit_order_id, exit_order_id, int(log_id)))


def insert_candle(symbol: str, timestamp: str, open: float, high: float, low: float,
                  close: float, ma20: float | None, atr: float | None, volume: float = 0.0) -> None:
    init_db()
    with _LOCK, _connect() as conn:
        conn.execute("""INSERT INTO live_candles(symbol,timestamp,open,high,low,close,ma20,atr,volume)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,timestamp) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
            ma20=excluded.ma20, atr=excluded.atr, volume=excluded.volume""",
            (symbol.upper(), str(timestamp), float(open), float(high), float(low), float(close), ma20, atr, float(volume)))


def get_recent_candles(symbol: str, limit: int = 100) -> pd.DataFrame:
    init_db()
    limit = max(1, min(int(limit), 5000))
    with _LOCK, _connect() as conn:
        return pd.read_sql_query("""
            SELECT timestamp,open,high,low,close,ma20,atr,volume
            FROM live_candles WHERE symbol=?
            ORDER BY timestamp DESC LIMIT ?
        """, conn, params=(symbol.upper(), limit)).sort_values("timestamp").reset_index(drop=True)


def get_all_recent_candles(limit: int = 100) -> pd.DataFrame:
    init_db()
    limit = max(1, min(int(limit), 5000))
    with _LOCK, _connect() as conn:
        return pd.read_sql_query("""
            SELECT symbol,timestamp,open,high,low,close,ma20,atr,volume
            FROM live_candles
            ORDER BY symbol, timestamp DESC LIMIT ?
        """, conn, params=(limit,)).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def get_trade_logs(limit: int = 50) -> pd.DataFrame:
    init_db()
    limit = max(1, min(int(limit), 5000))
    with _LOCK, _connect() as conn:
        return pd.read_sql_query("SELECT * FROM trade_logs ORDER BY id DESC LIMIT ?", conn, params=(limit,))


def get_portfolio_positions() -> pd.DataFrame:
    """Get current open positions with unrealized P&L from trade_logs."""
    init_db()
    with _LOCK, _connect() as conn:
        return pd.read_sql_query("""
            SELECT symbol, action, price as entry_price, sl, tp,
                   broker_order_id, filled_qty, entry_fill_price,
                   realized_pnl, position_size_pct,
                   MAX(timestamp) as last_update
            FROM trade_logs
            WHERE status LIKE 'SUBMITTED:%' OR status LIKE 'ENTRY_FILLED%' OR status = 'SIGNAL'
            GROUP BY symbol, broker_order_id
            ORDER BY timestamp DESC
        """, conn)


def get_daily_realized_pnl() -> float:
    """Get today's realized P&L (UTC)."""
    init_db()
    with _LOCK, _connect() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(realized_pnl), 0) as pnl
            FROM trade_logs
            WHERE realized_pnl IS NOT NULL
            AND date(timestamp) = date('now')
        """).fetchone()
        return float(row["pnl"]) if row else 0.0


def get_dashboard_snapshot(limit_logs: int = 100) -> dict:
    """Get consistent snapshot of watchlist, logs, and daily PnL in one transaction."""
    init_db()
    limit_logs = max(1, min(int(limit_logs), 5000))
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            watchlist_row = conn.execute("SELECT watchlist_json FROM bot_control WHERE id=1").fetchone()
            watchlist = json.loads(watchlist_row["watchlist_json"]) if watchlist_row and watchlist_row["watchlist_json"] else json.loads(DEFAULT_WATCHLIST)
            
            logs = pd.read_sql_query("SELECT * FROM trade_logs ORDER BY id DESC LIMIT ?", conn, params=(limit_logs,))
            
            daily_pnl_row = conn.execute("""
                SELECT COALESCE(SUM(realized_pnl), 0) as pnl
                FROM trade_logs
                WHERE realized_pnl IS NOT NULL
                AND date(timestamp) = date('now')
            """).fetchone()
            daily_pnl = float(daily_pnl_row["pnl"]) if daily_pnl_row else 0.0
            
            return {"watchlist": watchlist, "logs": logs, "daily_pnl": daily_pnl}
        finally:
            conn.execute("COMMIT")


init_db()