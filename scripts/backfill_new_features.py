import os
import sqlite3
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# Local imports
from src.regime_consensus import normalize_grade

DB_PATH = os.path.join("data", "screener.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def backfill():
    load_dotenv(dotenv_path="C:\\Users\\ccunn\\Documents\\antigravity\\quick-babbage\\.env")
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not alpaca_key or not alpaca_secret:
        print("Alpaca credentials missing.")
        return
        
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Fetch SPY historical close prices in bulk to compute 50-day SMA trend
    print("Pre-fetching SPY historical bars since 2025...")
    start_dt = datetime(2025, 1, 1)
    end_dt = datetime.now()
    
    try:
        spy_bars = alpaca_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols='SPY',
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            feed=DataFeed.IEX,
            adjustment='split'
        ))
        df_spy = spy_bars.df
        if df_spy is not None and not df_spy.empty:
            df_spy = df_spy.copy()
            if isinstance(df_spy.index, pd.MultiIndex):
                df_spy = df_spy.xs('SPY', level='symbol')
            df_spy.index = df_spy.index.tz_localize(None)
            df_spy = df_spy.sort_index()
            df_spy['sma_50'] = df_spy['close'].rolling(window=50).mean()
        else:
            df_spy = None
    except Exception as e:
        print(f"Error fetching SPY bars: {e}")
        df_spy = None

    def get_spy_regime_cached(date_str):
        if df_spy is None:
            return "Unknown"
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            df_prior = df_spy[df_spy.index.date <= target_date]
            if df_prior.empty:
                return "Unknown"
            last_row = df_prior.iloc[-1]
            close_price = float(last_row['close'])
            sma_val = float(last_row['sma_50'])
            if pd.isna(sma_val):
                return "Bear"
            return "Bull" if close_price > sma_val else "Bear"
        except Exception:
            return "Unknown"

    # 2. Get unique tickers to cache yfinance upgrades/downgrades
    cursor.execute("SELECT DISTINCT ticker FROM daily_signals")
    tickers = [row[0] for row in cursor.fetchall() if row[0]]
    print(f"Caching upgrades/downgrades for {len(tickers)} unique tickers...")
    
    ticker_cache = {}
    
    # 3. Backfill daily_signals table
    print("\nBackfilling daily_signals table...")
    cursor.execute("SELECT id, ticker, signal_date FROM daily_signals WHERE consensus_divergence_score IS NULL")
    signals = [dict(row) for row in cursor.fetchall()]
    print(f"Found {len(signals)} signals to update.")
    
    for idx, sig in enumerate(signals):
        sig_id = sig["id"]
        ticker = sig["ticker"]
        sig_date_str = sig["signal_date"]
        
        # Load/cache grades
        if ticker not in ticker_cache:
            try:
                stock = yf.Ticker(ticker)
                if hasattr(stock, 'upgrades_downgrades') and stock.upgrades_downgrades is not None:
                    df_up = stock.upgrades_downgrades.copy()
                    if df_up.index.tz is not None:
                        df_up.index = df_up.index.tz_localize(None)
                    ticker_cache[ticker] = df_up.sort_index()
                else:
                    ticker_cache[ticker] = None
            except Exception:
                ticker_cache[ticker] = None
                
        df_up = ticker_cache[ticker]
        buy_ratio, hold_ratio, sell_ratio, div_score = 0.5, 0.4, 0.1, 0.5
        
        if df_up is not None and not df_up.empty:
            try:
                target_date = datetime.strptime(sig_date_str, "%Y-%m-%d")
                df_prior = df_up[df_up.index < target_date]
                if not df_prior.empty:
                    last_ratings = df_prior.groupby('Firm').last()
                    classified = last_ratings['ToGrade'].apply(normalize_grade)
                    counts = classified.value_counts()
                    b = int(counts.get('Buy', 0))
                    h = int(counts.get('Hold', 0))
                    s = int(counts.get('Sell', 0))
                    total = b + h + s
                    if total > 0:
                        buy_ratio = round(b / total, 4)
                        hold_ratio = round(h / total, 4)
                        sell_ratio = round(s / total, 4)
                        div_score = round(1.0 - buy_ratio, 4)
            except Exception:
                pass
                
        cursor.execute("""
            UPDATE daily_signals
            SET consensus_buy_ratio = ?,
                consensus_hold_ratio = ?,
                consensus_sell_ratio = ?,
                consensus_divergence_score = ?
            WHERE id = ?
        """, (buy_ratio, hold_ratio, sell_ratio, div_score, sig_id))
        
        if idx % 100 == 0 and idx > 0:
            print(f"  Processed {idx} signals...")
            
    # 4. Backfill trades table
    print("\nBackfilling trades table...")
    cursor.execute("SELECT id, ticker, signal_date FROM trades WHERE consensus_divergence_score IS NULL OR market_regime IS NULL")
    trades = [dict(row) for row in cursor.fetchall()]
    print(f"Found {len(trades)} trades to update.")
    
    for idx, t in enumerate(trades):
        trade_id = t["id"]
        ticker = t["ticker"]
        sig_date_str = t["signal_date"]
        
        # SPY Regime
        regime = get_spy_regime_cached(sig_date_str)
        
        # Consensus metrics
        if ticker not in ticker_cache:
            try:
                stock = yf.Ticker(ticker)
                if hasattr(stock, 'upgrades_downgrades') and stock.upgrades_downgrades is not None:
                    df_up = stock.upgrades_downgrades.copy()
                    if df_up.index.tz is not None:
                        df_up.index = df_up.index.tz_localize(None)
                    ticker_cache[ticker] = df_up.sort_index()
                else:
                    ticker_cache[ticker] = None
            except Exception:
                ticker_cache[ticker] = None
                
        df_up = ticker_cache[ticker]
        buy_ratio, hold_ratio, sell_ratio, div_score = 0.5, 0.4, 0.1, 0.5
        
        if df_up is not None and not df_up.empty:
            try:
                target_date = datetime.strptime(sig_date_str, "%Y-%m-%d")
                df_prior = df_up[df_up.index < target_date]
                if not df_prior.empty:
                    last_ratings = df_prior.groupby('Firm').last()
                    classified = last_ratings['ToGrade'].apply(normalize_grade)
                    counts = classified.value_counts()
                    b = int(counts.get('Buy', 0))
                    h = int(counts.get('Hold', 0))
                    s = int(counts.get('Sell', 0))
                    total = b + h + s
                    if total > 0:
                        buy_ratio = round(b / total, 4)
                        hold_ratio = round(h / total, 4)
                        sell_ratio = round(s / total, 4)
                        div_score = round(1.0 - buy_ratio, 4)
            except Exception:
                pass
                
        cursor.execute("""
            UPDATE trades
            SET consensus_buy_ratio = ?,
                consensus_hold_ratio = ?,
                consensus_sell_ratio = ?,
                consensus_divergence_score = ?,
                market_regime = ?
            WHERE id = ?
        """, (buy_ratio, hold_ratio, sell_ratio, div_score, regime, trade_id))
        
    conn.commit()
    conn.close()
    print("\nBackfill operation finished successfully.")

if __name__ == "__main__":
    backfill()
