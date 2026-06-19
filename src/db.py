import os
import sqlite3

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "screener.db")

def get_connection():
    """Returns a SQLite connection with row factory configured to dict-like rows."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database tables if they do not exist."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = get_connection()
    cursor = conn.cursor()
    
    # Create daily_signals table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scrape_timestamp TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        ticker TEXT NOT NULL,
        analyst_name TEXT,
        firm TEXT,
        from_grade TEXT,
        to_grade TEXT,
        current_price REAL,
        target_price REAL,
        upside_percentage REAL,
        filter_status TEXT DEFAULT 'pending',
        volume_spike_multiple REAL,
        is_earnings_proximate INTEGER DEFAULT 0,
        earnings_date TEXT,
        eps_actual REAL,
        eps_estimate REAL,
        consensus_buy_ratio REAL,
        consensus_hold_ratio REAL,
        consensus_sell_ratio REAL,
        consensus_divergence_score REAL,
        UNIQUE(signal_date, ticker, firm)
    )
    """)
    
    # Create analyst_history table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS analyst_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analyst_name TEXT NOT NULL,
        firm TEXT NOT NULL,
        ticker TEXT NOT NULL,
        signal_date TEXT NOT NULL,
        entry_price REAL,
        target_price REAL,
        evaluation_date TEXT,
        exit_price REAL,
        stock_return_pct REAL,
        spy_return_pct REAL,
        status TEXT DEFAULT 'Pending',
        evaluation_window_days INTEGER DEFAULT 63,
        UNIQUE(analyst_name, firm, ticker, signal_date)
    )
    """)
    
    # Create trades table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_date TEXT,
        entry_date TEXT,
        ticker TEXT,
        analyst_name TEXT,
        firm TEXT,
        analyst_winrate_at_signal REAL,
        analyst_n_at_signal INTEGER,
        target_price REAL,
        entry_price REAL,
        exit_price REAL,
        exit_date TEXT,
        exit_reason TEXT,
        stock_return_pct REAL,
        spy_return_pct REAL,
        excess_return_pct REAL,
        volume_spike_multiple REAL,
        is_earnings_proximate INTEGER DEFAULT 0,
        earnings_date TEXT,
        eps_actual REAL,
        eps_estimate REAL,
        evaluation_window_days INTEGER DEFAULT 63,
        consensus_buy_ratio REAL,
        consensus_hold_ratio REAL,
        consensus_sell_ratio REAL,
        consensus_divergence_score REAL,
        market_regime TEXT,
        alpaca_qty REAL,
        highest_price_recorded REAL,
        paper_trade_status TEXT DEFAULT 'none'
    )
    """)
    
    # Run safety migrations to add missing columns to existing DB
    new_cols_signals = [
        ("consensus_buy_ratio", "REAL"),
        ("consensus_hold_ratio", "REAL"),
        ("consensus_sell_ratio", "REAL"),
        ("consensus_divergence_score", "REAL")
    ]
    for col_name, col_type in new_cols_signals:
        try:
            cursor.execute(f"ALTER TABLE daily_signals ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # Already exists

    new_cols_trades = [
        ("consensus_buy_ratio", "REAL"),
        ("consensus_hold_ratio", "REAL"),
        ("consensus_sell_ratio", "REAL"),
        ("consensus_divergence_score", "REAL"),
        ("market_regime", "TEXT"),
        ("alpaca_qty", "REAL"),
        ("highest_price_recorded", "REAL"),
        ("paper_trade_status", "TEXT DEFAULT 'none'")
    ]
    for col_name, col_type in new_cols_trades:
        try:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass  # Already exists
            
    conn.commit()
    conn.close()
    print("Database tables initialized and migrated successfully.")

if __name__ == "__main__":
    init_db()
