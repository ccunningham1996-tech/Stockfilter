import os
import sqlite3

DB_PATH = os.path.join("data", "screener.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def backfill():
    if not os.path.exists(DB_PATH):
        print("Database not found!")
        return

    conn = get_connection()
    cursor = conn.cursor()

    # Query trades where analyst_winrate_at_signal is NULL
    cursor.execute("""
        SELECT id, ticker, analyst_name, firm, signal_date 
        FROM trades 
        WHERE analyst_winrate_at_signal IS NULL
    """)
    trades = [dict(row) for row in cursor.fetchall()]

    print(f"Found {len(trades)} trades with NULL win rate to backfill.")

    backfilled_count = 0
    for t in trades:
        t_id = t["id"]
        analyst = t["analyst_name"]
        firm = t["firm"]
        sig_date = t["signal_date"]

        # Get win rate strictly before signal_date
        cursor.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as won
            FROM analyst_history
            WHERE analyst_name = ?
              AND firm = ?
              AND evaluation_date < ?
              AND status IN ('Won', 'Lost')
        """, (analyst, firm, sig_date))
        
        row = cursor.fetchone()
        total = row['total'] if row['total'] is not None else 0
        won = row['won'] if row['won'] is not None else 0

        # Only update if there was at least 1 closed rating
        if total > 0:
            win_rate = round(float(won / total), 4)
            print(f"  Trade ID {t_id} ({t['ticker']}) analyst {analyst} ({firm}) backfilled: win_rate={win_rate*100:.1f}%, n={total}")
            cursor.execute("""
                UPDATE trades
                SET analyst_winrate_at_signal = ?,
                    analyst_n_at_signal = ?
                WHERE id = ?
            """, (win_rate, total, t_id))
            backfilled_count += 1
        else:
            # If 0 closed ratings, we can explicitly set it to 0.0 or leave as NULL. Let's explicitly set to 0.0 or leave it to show 0%
            print(f"  Trade ID {t_id} ({t['ticker']}) has 0 closed ratings as of {sig_date}. Setting win_rate=0.0")
            cursor.execute("""
                UPDATE trades
                SET analyst_winrate_at_signal = 0.0,
                    analyst_n_at_signal = 0
                WHERE id = ?
            """, (t_id,))
            backfilled_count += 1

    conn.commit()
    conn.close()
    print(f"\nBackfilled {backfilled_count} trades successfully.")

if __name__ == "__main__":
    backfill()
