"""
myNSE200 / db.py
-----------------
SQLite storage layer. WAL mode + busy timeout per your performance rules.
"""

import sqlite3
import contextlib
import config


def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")  # 30s busy timeout
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    with contextlib.closing(get_conn()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS instruments (
            symbol TEXT PRIMARY KEY,
            exchange TEXT,
            yf_ticker TEXT,
            name TEXT,
            sector TEXT,
            in_universe INTEGER DEFAULT 1,
            last_updated TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS candle (
            symbol TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (symbol, date)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_candle_symbol_date ON candle(symbol, date);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            symbol TEXT PRIMARY KEY,
            pe_ratio REAL,
            promoter_holding REAL,
            fii_holding REAL,
            dii_holding REAL,
            sales_growth_3y REAL,
            profit_growth_3y REAL,
            pledged_pct REAL,
            pledge_known INTEGER DEFAULT 1,
            growth_known INTEGER DEFAULT 1,
            last_updated TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            symbol TEXT,
            run_date TEXT,
            momentum_z REAL,
            trend_z REAL,
            quality_z REAL,
            growth_z REAL,
            overall_score REAL,
            recommended INTEGER,
            PRIMARY KEY (symbol, run_date)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            symbol TEXT,
            run_date TEXT,
            status INTEGER,
            overall_score REAL,
            entry_price REAL,
            stop_loss REAL,
            target_price REAL,
            risk_per_share REAL,
            quantity INTEGER,
            order_value REAL,
            reward_risk_ratio REAL,
            reasons TEXT,
            PRIMARY KEY (symbol, run_date)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            status TEXT,               -- pending | holding | closed_target | closed_stop |
                                        -- closed_time | skipped | expired
            signal_date TEXT,
            signal_price REAL,
            stop_loss REAL,
            target_price REAL,
            quantity_recommended INTEGER,
            quantity_bought INTEGER,
            buy_price REAL,
            buy_date TEXT,
            exit_price REAL,
            exit_date TEXT,
            exit_reason TEXT,
            last_updated TEXT
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS telegram_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {config.DB_PATH}")
