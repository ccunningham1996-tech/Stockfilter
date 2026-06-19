import os
import time
import sqlite3
import ssl
import pandas as pd
import requests
from io import StringIO
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# Local imports
from src.db import get_connection, init_db
from src.outcome_tracker import run_outcome_tracker
from src.filter import run_filter

# Bypass SSL Verification for Wikipedia scraper on Windows
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

def get_sp500_tickers():
    """Fetches the list of S&P 500 tickers from Wikipedia using a browser User-Agent."""
    print("Fetching S&P 500 ticker list from Wikipedia...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        table = pd.read_html(StringIO(response.text))
        df = table[0]
        tickers = df['Symbol'].tolist()
        # Clean tickers (replace dots with dashes for yfinance compatibility)
        tickers = [t.replace('.', '-') for t in tickers]
        return tickers
    except Exception as e:
        print(f"Error fetching S&P 500 tickers from Wikipedia: {str(e)[:200]}")
        # Fallback list of popular tickers if Wikipedia is down
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META", "BRK-B", "JNJ", "V"]

def fetch_ticker_upgrades_yfinance(symbol, stock):
    """Fetches upgrades history for a symbol from yfinance."""
    try:
        df_ud = stock.upgrades_downgrades
        if df_ud is None or df_ud.empty:
            return []
            
        df_ud = df_ud.reset_index()
        items = []
        for idx, row in df_ud.iterrows():
            action = str(row.get('Action', '')).lower()
            if 'up' not in action and 'raise' not in action:
                continue
                
            grade_date = row['GradeDate']
            if isinstance(grade_date, str):
                grade_date = pd.to_datetime(grade_date)
            grade_date = grade_date.tz_localize(None)
            
            target = row.get('currentPriceTarget', 0.0)
            
            items.append({
                "symbol": symbol,
                "company": row.get('Firm'),
                "fromGrade": row.get('FromGrade'),
                "toGrade": row.get('ToGrade'),
                "action": "upgrade",
                "gradeTime": int(grade_date.timestamp()),
                "targetPrice": float(target) if target else 0.0
            })
        return items
    except Exception as e:
        print(f"Error fetching upgrades from yfinance for {symbol}: {e}")
        return []

def get_historical_earnings_yfinance(df_earnings, signal_date):
    """
    Checks if there is an earnings event within +/- 5 calendar days of the signal date
    using a pre-fetched yfinance earnings DataFrame.
    """
    if df_earnings is None or df_earnings.empty:
        return 0, None, None, None
        
    try:
        # Calculate distance to signal date
        min_diff = 9999
        closest_row = None
        closest_date_val = None
        
        for idx, row in df_earnings.iterrows():
            earnings_date = idx.date()
            diff = abs((earnings_date - signal_date).days)
            if diff < min_diff:
                min_diff = diff
                closest_row = row
                closest_date_val = earnings_date
                
        if closest_row is not None and min_diff <= 5:
            earnings_date_str = closest_date_val.strftime("%Y-%m-%d")
            eps_actual = closest_row.get("Reported EPS")
            eps_estimate = closest_row.get("EPS Estimate")
            
            if pd.isna(eps_actual) or pd.isna(eps_estimate):
                return 0, earnings_date_str, None, None
                
            beat = float(eps_actual) >= float(eps_estimate)
            return (1 if beat else 0), earnings_date_str, float(eps_actual), float(eps_estimate)
            
    except Exception as e:
        print(f"Error matching historical earnings: {e}")
        
    return 0, None, None, None

def run_seeder():
    init_db()  # Ensure database and tables exist
    
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not all([alpaca_key, alpaca_secret]):
        raise ValueError("Alpaca API credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env.")
        
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get tickers that are already seeded to skip them on resume
    cursor.execute("SELECT DISTINCT ticker FROM daily_signals")
    seeded_tickers = set(row[0] for row in cursor.fetchall())
    
    tickers = get_sp500_tickers()
    try:
        wsm_idx = tickers.index('WSM')
        tickers_to_seed = tickers[wsm_idx:]
    except ValueError:
        tickers_to_seed = tickers
    
    print(f"Seeding historical analyst upgrades for {len(tickers_to_seed)} S&P 500 tickers over the past 12 months...")
    
    cutoff_date = datetime.now() - timedelta(days=365)
    seeded_count = 0
    
    for idx, ticker in enumerate(tickers_to_seed):
        if ticker in seeded_tickers:
            print(f"[{idx+1}/{len(tickers_to_seed)}] Skipping {ticker} (already seeded)")
            continue
            
        print(f"\n[{idx+1}/{len(tickers_to_seed)}] Fetching history for {ticker}...")
        
        import yfinance as yf
        raw_upgrades = []
        df_earnings = None
        
        # 1. Fetch upgrades from yfinance
        try:
            stock = yf.Ticker(ticker)
            raw_upgrades = fetch_ticker_upgrades_yfinance(ticker, stock)
        except Exception as e:
            print(f"Error fetching yfinance upgrades for {ticker}: {e}")
            continue
            
        # 2. Fetch earnings calendar from yfinance (safely caught)
        try:
            df_earnings = stock.earnings_dates
            if df_earnings is not None and not df_earnings.empty:
                df_earnings = df_earnings.copy()
                df_earnings.index = pd.to_datetime(df_earnings.index.date)
        except Exception as e:
            print(f"Warning: Could not fetch earnings dates for {ticker} from yfinance: {e}")
            df_earnings = None
            
        print(f" -> Found {len(raw_upgrades)} upgrades history.")
        
        if not raw_upgrades:
            continue
            
        # 3. Fetch Alpaca bars for the past 16 months in one go
        # This provides a 10x speedup by avoiding querying bars per upgrade row!
        time.sleep(1.1)  # Pace Alpaca calls
        try:
            start_date_bars = datetime.now() - timedelta(days=480)
            bars = alpaca_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Day,
                start=datetime.combine(start_date_bars.date(), datetime.min.time()),
                end=datetime.combine(datetime.now().date(), datetime.max.time()),
                feed=DataFeed.IEX,
                adjustment='split'
            ))
            
            df_bars = bars.df
            if df_bars is None or df_bars.empty:
                print(f" -> No price bars found on Alpaca for {ticker}. Skipping.")
                continue
                
            df_bars = df_bars.copy()
            # Handle MultiIndex
            if isinstance(df_bars.index, pd.MultiIndex):
                if ticker in df_bars.index.levels[0]:
                    df_bars = df_bars.xs(ticker, level='symbol')
                else:
                    first_symbol = df_bars.index.levels[0][0]
                    df_bars = df_bars.xs(first_symbol, level='symbol')
                    
            df_bars.index = df_bars.index.tz_localize(None)
            df_bars = df_bars.sort_index()
        except Exception as e:
            print(f"Error fetching historical bars for {ticker}: {e}")
            continue
            
        ticker_seeded = 0
        
        for item in raw_upgrades:
            grade_time = item.get("gradeTime")
            if not grade_time:
                continue
                
            upgrade_dt = datetime.fromtimestamp(grade_time)
            if upgrade_dt < cutoff_date:
                continue  # Out of 12-month window
                
            firm = item.get("company")
            target = item.get("targetPrice")
            signal_date_str = upgrade_dt.strftime("%Y-%m-%d")
            
            if not firm:
                continue
                
            # Check if record already exists to prevent duplicate calls
            cursor.execute("""
                SELECT COUNT(*) FROM analyst_history 
                WHERE analyst_name = 'N/A' AND firm = ? AND ticker = ? AND signal_date = ?
            """, (firm, ticker, signal_date_str))
            if cursor.fetchone()[0] > 0:
                continue
                
            # Get entry price and date from our pre-fetched bars
            # Locate first trading session on/after signal date
            df_after = df_bars[df_bars.index.date >= upgrade_dt.date()]
            if df_after.empty:
                continue
                
            signal_session_bar = df_after.iloc[0]
            entry_price = float(signal_session_bar['open'])
            actual_signal_date = df_after.index[0].date()
            actual_signal_date_str = actual_signal_date.strftime("%Y-%m-%d")
            
            # Find the index of actual_signal_date in df_bars
            try:
                date_indices = df_bars.index.date
                match_indices = [i for i, d in enumerate(date_indices) if d == actual_signal_date]
                if not match_indices:
                    continue
                bar_idx = match_indices[0]
            except Exception:
                continue
                
            # Calculate evaluation date (63 trading days elapsed)
            if bar_idx + 62 < len(df_bars):
                eval_bar_date = df_bars.index[bar_idx + 62].date()
                evaluation_date_str = eval_bar_date.strftime("%Y-%m-%d")
            else:
                est_date = actual_signal_date + timedelta(days=90)
                evaluation_date_str = est_date.strftime("%Y-%m-%d")
                
            # Check earnings proximity
            is_earnings_prox, earn_date, eps_act, eps_est = get_historical_earnings_yfinance(
                df_earnings, actual_signal_date
            )
            
            # 1. Insert into analyst_history
            cursor.execute("""
                INSERT OR IGNORE INTO analyst_history (
                    analyst_name, firm, ticker, signal_date, entry_price, target_price, evaluation_date, status,
                    evaluation_window_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 63)
            """, (
                "N/A", firm, ticker, actual_signal_date_str, entry_price, target, evaluation_date_str, "Pending"
            ))
            
            # 2. Also insert into daily_signals as pending to simulate incoming pipeline logs
            upside = round(((target - entry_price) / entry_price) * 100, 2) if target and entry_price else None
            scrape_timestamp = datetime.now().isoformat()
            
            cursor.execute("""
                INSERT OR IGNORE INTO daily_signals (
                    scrape_timestamp, signal_date, ticker, analyst_name, firm,
                    from_grade, to_grade, current_price, target_price, upside_percentage,
                    filter_status, is_earnings_proximate, earnings_date, eps_actual, eps_estimate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scrape_timestamp, actual_signal_date_str, ticker, "N/A", firm,
                item.get("fromGrade"), item.get("toGrade"), entry_price, target, upside,
                "pending", is_earnings_prox, earn_date, eps_act, eps_est
            ))
            
            ticker_seeded += 1
            seeded_count += 1
            
        print(f" -> Seeded {ticker_seeded} historical signals.")
        conn.commit()
        
    conn.close()
    print(f"\nHistory seeding finished. Seeded {seeded_count} total records.")
    
    # Run filter engine on seeded signals
    print("Running filter engine on seeded historical signals...")
    run_filter()
    
    # Run evaluation on completed ratings (63 trading days elapsed)
    print("Evaluating pending ratings and trades where 63 trading days have elapsed...")
    run_outcome_tracker()

if __name__ == "__main__":
    run_seeder()
