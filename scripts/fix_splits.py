import os
import sqlite3
import pandas as pd
import yfinance as yf
from datetime import datetime

# Database config
DB_PATH = os.path.join("data", "screener.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def fix_splits():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = get_connection()
    cursor = conn.cursor()

    # Get all distinct tickers from the tables
    tickers = set()
    for table in ["daily_signals", "analyst_history", "trades"]:
        try:
            cursor.execute(f"SELECT DISTINCT ticker FROM {table}")
            tickers.update([row[0] for row in cursor.fetchall() if row[0]])
        except sqlite3.OperationalError:
            pass

    print(f"Found {len(tickers)} distinct tickers in the database.")

    for ticker in sorted(list(tickers)):
        try:
            stock = yf.Ticker(ticker)
            splits = stock.splits
            if splits is None or splits.empty:
                continue
                
            # Filter splits from 2025-01-01 onwards
            splits = splits[splits.index >= "2025-01-01"]
            if splits.empty:
                continue

            print(f"\nTicker: {ticker} has splits:")
            for dt, ratio in splits.items():
                print(f"  {dt.date()}: {ratio}x")

            # 1. Process daily_signals
            cursor.execute("SELECT id, signal_date, current_price, target_price FROM daily_signals WHERE ticker = ?", (ticker,))
            signals = [dict(row) for row in cursor.fetchall()]
            for sig in signals:
                sig_date = datetime.strptime(sig["signal_date"], "%Y-%m-%d").date()
                cum_ratio = 1.0
                for dt, ratio in splits.items():
                    if sig_date < dt.date():
                        cum_ratio *= ratio
                
                if cum_ratio > 1.0 and sig["target_price"] is not None:
                    curr = sig["current_price"] or 1.0
                    target = sig["target_price"]
                    # If target price is pre-split, it will be much larger than post-split price
                    # Heuristic: if target_price is > 2.5x the post-split current price, it is pre-split.
                    if target > 2.5 * curr:
                        new_target = round(target / cum_ratio, 2)
                        new_upside = round(((new_target - curr) / curr) * 100, 2)
                        print(f"  [daily_signals ID {sig['id']}] {sig['signal_date']}: target_price {target} -> {new_target} (upside {new_upside}%)")
                        cursor.execute("""
                            UPDATE daily_signals
                            SET target_price = ?, upside_percentage = ?
                            WHERE id = ?
                        """, (new_target, new_upside, sig["id"]))

            # 2. Process analyst_history
            cursor.execute("SELECT id, signal_date, entry_price, target_price FROM analyst_history WHERE ticker = ?", (ticker,))
            ratings = [dict(row) for row in cursor.fetchall()]
            for r in ratings:
                sig_date = datetime.strptime(r["signal_date"], "%Y-%m-%d").date()
                cum_ratio = 1.0
                for dt, ratio in splits.items():
                    if sig_date < dt.date():
                        cum_ratio *= ratio

                if cum_ratio > 1.0 and r["target_price"] is not None:
                    entry = r["entry_price"] or 1.0
                    target = r["target_price"]
                    if target > 2.5 * entry:
                        new_target = round(target / cum_ratio, 2)
                        print(f"  [analyst_history ID {r['id']}] {r['signal_date']}: target_price {target} -> {new_target}")
                        cursor.execute("""
                            UPDATE analyst_history
                            SET target_price = ?
                            WHERE id = ?
                        """, (new_target, r["id"]))

            # 3. Process trades
            cursor.execute("SELECT id, signal_date, entry_price, target_price FROM trades WHERE ticker = ?", (ticker,))
            trades = [dict(row) for row in cursor.fetchall()]
            for t in trades:
                sig_date = datetime.strptime(t["signal_date"], "%Y-%m-%d").date()
                cum_ratio = 1.0
                for dt, ratio in splits.items():
                    if sig_date < dt.date():
                        cum_ratio *= ratio

                if cum_ratio > 1.0 and t["target_price"] is not None:
                    entry = t["entry_price"] or 1.0
                    target = t["target_price"]
                    if target > 2.5 * entry:
                        new_target = round(target / cum_ratio, 2)
                        print(f"  [trades ID {t['id']}] {t['signal_date']}: target_price {target} -> {new_target}")
                        cursor.execute("""
                            UPDATE trades
                            SET target_price = ?
                            WHERE id = ?
                        """, (new_target, t["id"]))

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

    conn.commit()
    conn.close()
    print("\nDatabase stock-split correction completed.")

if __name__ == "__main__":
    fix_splits()
