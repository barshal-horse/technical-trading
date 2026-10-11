#!/usr/bin/env python3
"""One-time migration: single-symbol -> multi-symbol schema.

Run once: python migrate_multi_symbol.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_PATH = Path(os.getenv("TRADE_DB_PATH", "data/trading.sqlite3"))
DEFAULT_WATCHLIST = json.dumps([
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "SPY", "QQQ", "IWM"
])


def migrate() -> None:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run engine.py first to initialize.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Migrate live_candles to compound PK (symbol, timestamp)
        print("Migrating live_candles table...")
        cursor = conn.execute("SELECT * FROM live_candles LIMIT 1")
        row = cursor.fetchone()
        if row:
            # Get existing data (assume AAPL as default for legacy rows)
            old_rows = conn.execute("SELECT * FROM live_candles").fetchall()
            
            # Create new table with compound PK
            conn.execute("""
                CREATE TABLE live_candles_new (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    ma20 REAL,
                    atr REAL,
                    PRIMARY KEY (symbol, timestamp)
                )
            """)
            
            # Migrate data with default symbol 'AAPL' for legacy rows
            for r in old_rows:
                conn.execute("""
                    INSERT INTO live_candles_new (symbol, timestamp, open, high, low, close, ma20, atr)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ("AAPL", r["timestamp"], r["open"], r["high"], r["low"], 
                      r["close"], r["ma20"], r["atr"]))
            
            conn.execute("DROP TABLE live_candles")
            conn.execute("ALTER TABLE live_candles_new RENAME TO live_candles")
            print(f"  Migrated {len(old_rows)} candles to multi-symbol schema")
        else:
            # Table empty, just recreate with new schema
            conn.execute("DROP TABLE live_candles")
            conn.execute("""
                CREATE TABLE live_candles (
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    ma20 REAL,
                    atr REAL,
                    PRIMARY KEY (symbol, timestamp)
                )
            """)
            print("  Created new multi-symbol live_candles table (was empty)")

        # 2. Add indices for query performance
        print("Adding indices...")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_candles_symbol_time ON live_candles(symbol, timestamp DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trade_logs_symbol_time ON trade_logs(symbol, timestamp DESC)")
        print("  Indices created")

        # 3. Add new columns to bot_control
        print("Updating bot_control table...")
        control_cols = {row[1] for row in conn.execute("PRAGMA table_info(bot_control)")}
        
        new_control_cols = {
            "watchlist_json": f"TEXT NOT NULL DEFAULT '{DEFAULT_WATCHLIST}'",
            "max_positions": "INTEGER NOT NULL DEFAULT 5",
            "max_correlated_positions": "INTEGER NOT NULL DEFAULT 2",
            "scan_enabled": "INTEGER NOT NULL DEFAULT 1",
            "scan_interval_min": "INTEGER NOT NULL DEFAULT 5",
            "min_signal_score": "REAL NOT NULL DEFAULT 0.6",
            "kelly_fraction": "REAL NOT NULL DEFAULT 0.25",
            "max_position_pct": "REAL NOT NULL DEFAULT 10.0",
        }
        
        for col, decl in new_control_cols.items():
            if col not in control_cols:
                conn.execute(f"ALTER TABLE bot_control ADD COLUMN {col} {decl}")
                print(f"  Added {col}")
        
        # Ensure watchlist_json is set for existing row
        conn.execute("UPDATE bot_control SET watchlist_json=? WHERE id=1 AND (watchlist_json IS NULL OR watchlist_json='')", 
                     (DEFAULT_WATCHLIST,))

        # 4. Add new columns to trade_logs
        print("Updating trade_logs table...")
        log_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_logs)")}
        
        new_log_cols = {
            "portfolio_id": "TEXT",
            "position_size_pct": "REAL",
        }
        
        for col, decl in new_log_cols.items():
            if col not in log_cols:
                conn.execute(f"ALTER TABLE trade_logs ADD COLUMN {col} {decl}")
                print(f"  Added {col}")

        conn.commit()
        print("\nMigration complete!")
        
        # Verify
        print("\nVerification:")
        print(f"  live_candles PK: {conn.execute('SELECT sql FROM sqlite_master WHERE name=\"live_candles\"').fetchone()[0]}")
        print(f"  bot_control cols: {[r[1] for r in conn.execute('PRAGMA table_info(bot_control)')]}")
        print(f"  trade_logs cols: {[r[1] for r in conn.execute('PRAGMA table_info(trade_logs)')]}")


if __name__ == "__main__":
    migrate()