import os
import sqlite3
from datetime import datetime, timedelta

def init_db():
    os.makedirs("data", exist_ok=True)
    db_path = os.path.join("data", "screener.db")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Create analyst_history table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS analyst_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        analyst_name TEXT,
        firm TEXT,
        ticker TEXT,
        target_price REAL,
        target_date TEXT,
        status TEXT
    )
    """)
    
    # Create daily_signals table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        ticker TEXT,
        analyst_name TEXT,
        firm TEXT,
        current_price REAL,
        target_price REAL,
        upside_percentage REAL
    )
    """)
    
    # Seed historical records to calculate win-rates
    cursor.execute("SELECT COUNT(*) FROM analyst_history")
    if cursor.fetchone()[0] == 0:
        historical_seeds = [
            ("Dan Ives", "Wedbush", "AAPL", 250.0, "2025-06-15", "Won"),
            ("Dan Ives", "Wedbush", "MSFT", 500.0, "2025-07-20", "Won"),
            ("Dan Ives", "Wedbush", "TSLA", 300.0, "2025-08-11", "Lost"),
            ("Dan Ives", "Wedbush", "NVDA", 140.0, "2025-09-01", "Won"),
            ("Dan Ives", "Wedbush", "AVGO", 1800.0, "2025-10-05", "Won"), # 4/5 = 80%
            
            ("Toni Sacconaghi", "Bernstein", "AAPL", 210.0, "2025-05-10", "Lost"),
            ("Toni Sacconaghi", "Bernstein", "TSLA", 150.0, "2025-06-12", "Lost"),
            ("Toni Sacconaghi", "Bernstein", "MSFT", 420.0, "2025-09-18", "Won"), # 1/3 = 33%
            
            ("Toshiya Hari", "Goldman Sachs", "NVDA", 135.0, "2025-04-11", "Won"),
            ("Toshiya Hari", "Goldman Sachs", "AMD", 220.0, "2025-05-20", "Won"),
            ("Toshiya Hari", "Goldman Sachs", "INTC", 45.0, "2025-07-02", "Lost"),
            ("Toshiya Hari", "Goldman Sachs", "MRVL", 95.0, "2025-08-15", "Won"), # 3/4 = 75%
        ]
        cursor.executemany("""
        INSERT INTO analyst_history (analyst_name, firm, ticker, target_price, target_date, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """, historical_seeds)
        
    # Seed mock signals for yesterday (Day T) to verify T+1 pipelines
    cursor.execute("SELECT COUNT(*) FROM daily_signals")
    if cursor.fetchone()[0] == 0:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        day_t_seeds = [
            (yesterday, "AAPL", "Dan Ives", "Wedbush", 210.0, 260.0, 23.8),
            (yesterday, "TSLA", "Toni Sacconaghi", "Bernstein", 180.0, 230.0, 27.7),
            (yesterday, "NVDA", "Toshiya Hari", "Goldman Sachs", 125.0, 155.0, 24.0),
        ]
        cursor.executemany("""
        INSERT INTO daily_signals (date, ticker, analyst_name, firm, current_price, target_price, upside_percentage)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, day_t_seeds)
        
    conn.commit()
    conn.close()
    print("Database structures successfully verified and seeded.")

if __name__ == "__main__":
    init_db()
