import os
import time
import sqlite3
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
from dotenv import load_dotenv
from io import StringIO
import requests
import ssl

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# Bypass SSL verification for Wikipedia
ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

DB_PATH = "data/screener.db"

def get_all_tickers():
    tickers = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    
    # 1. S&P 500
    print("Fetching S&P 500 tickers from Wikipedia...")
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        r = requests.get(url, headers=headers, timeout=15)
        df = pd.read_html(StringIO(r.text))[0]
        tickers.extend(df['Symbol'].tolist())
    except Exception as e:
        print("Error S&P 500:", e)
        
    # 2. S&P 400 (MidCap)
    print("Fetching S&P 400 (MidCap) tickers from Wikipedia...")
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
        r = requests.get(url, headers=headers, timeout=15)
        df = pd.read_html(StringIO(r.text))[0]
        tickers.extend(df['Symbol'].tolist())
    except Exception as e:
        print("Error S&P 400:", e)
        
    # 3. S&P 600 (SmallCap)
    print("Fetching S&P 600 (SmallCap) tickers from Wikipedia...")
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"
        r = requests.get(url, headers=headers, timeout=15)
        df = pd.read_html(StringIO(r.text))[0]
        tickers.extend(df['Symbol'].tolist())
    except Exception as e:
        print("Error S&P 600:", e)
        
    # Clean and de-duplicate
    cleaned = []
    for t in tickers:
        t_clean = str(t).strip().replace('.', '-')
        if t_clean and t_clean not in cleaned:
            cleaned.append(t_clean)
    return cleaned

def get_winrate_as_of(analyst_name, firm, as_of_date):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as won
        FROM analyst_history
        WHERE analyst_name = ?
          AND firm = ?
          AND evaluation_date < ?
          AND status IN ('Won', 'Lost')
    """, [analyst_name, firm, as_of_date])
    row = cursor.fetchone()
    total = row['total'] if row['total'] is not None else 0
    won = row['won'] if row['won'] is not None else 0
    conn.close()
    if total == 0:
        return 0.0, 0
    return won / total, total

def main():
    print("=== Simulating S&P Composite 1500 (Large/Mid/Small Caps) ===")
    print("Window: June 22 - July 10, 2026")
    
    start_date = datetime(2026, 6, 22)
    end_date = datetime(2026, 7, 10)
    
    tickers = get_all_tickers()
    if not tickers:
        print("Could not retrieve tickers.")
        return
        
    print(f"Loaded {len(tickers)} unique tickers. Scanning yfinance for upgrades...")
    
    # 1. Scan yfinance upgrades
    all_upgrades = []
    scanned_count = 0
    for ticker in tickers:
        scanned_count += 1
        if scanned_count % 100 == 0:
            print(f"  Scanned {scanned_count}/{len(tickers)} tickers...")
        try:
            stock = yf.Ticker(ticker)
            df_ud = stock.upgrades_downgrades
            if df_ud is None or df_ud.empty:
                continue
            df_ud = df_ud.reset_index()
            for _, row in df_ud.iterrows():
                action = str(row.get('Action', '')).lower()
                if 'up' not in action and 'raise' not in action:
                    continue
                
                grade_date = row['GradeDate']
                if isinstance(grade_date, str):
                    grade_date = pd.to_datetime(grade_date)
                grade_date = grade_date.tz_localize(None)
                
                if start_date <= grade_date <= end_date:
                    target = row.get('currentPriceTarget', 0.0)
                    all_upgrades.append({
                        "ticker": ticker,
                        "firm": row.get('Firm'),
                        "analyst_name": "N/A",
                        "signal_date": grade_date.date().strftime("%Y-%m-%d"),
                        "target_price": float(target) if target else 0.0
                    })
        except Exception:
            pass
            
    print(f"Found {len(all_upgrades)} upgrades during the 3-week window.")
    if not all_upgrades:
        return

    # De-duplicate upgrades by ticker/date/firm
    df_signals = pd.DataFrame(all_upgrades).drop_duplicates(subset=['ticker', 'signal_date', 'firm'])
    print(f"De-duplicated to {len(df_signals)} unique upgrades.")
    
    # 2. Download historical bars from Alpaca for all required tickers in bulk
    unique_tickers = list(df_signals['ticker'].unique())
    if 'SPY' not in unique_tickers:
        unique_tickers.append('SPY')
        
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    
    bar_start = datetime(2026, 5, 1)
    bar_end = datetime(2026, 7, 11)
    
    print(f"Downloading historical daily price bars from Alpaca for {len(unique_tickers)} tickers...")
    bars_dict = {}
    chunk_size = 50
    for i in range(0, len(unique_tickers), chunk_size):
        chunk = unique_tickers[i:i+chunk_size]
        try:
            bars = alpaca_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=bar_start,
                end=bar_end,
                feed=DataFeed.IEX,
                adjustment='split'
            ))
            df_chunk = bars.df
            if df_chunk is not None and not df_chunk.empty:
                for symbol in chunk:
                    try:
                        df_sym = df_chunk.xs(symbol, level='symbol')
                        df_sym.index = df_sym.index.tz_localize(None)
                        bars_dict[symbol] = df_sym.sort_index()
                    except KeyError:
                        pass
        except Exception as e:
            print(f"Error downloading chunk: {e}")
            
    print(f"Successfully loaded price bars for {len(bars_dict)} tickers.")
    
    # 3. Filter Signals
    passed_signals = []
    print("\n--- Detailed Signal Filtering Log ---")
    for _, sig in df_signals.iterrows():
        ticker = sig['ticker']
        firm = sig['firm']
        analyst = sig['analyst_name']
        signal_date_str = sig['signal_date']
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
        
        # Win-rate Check
        win_rate, n_ratings = get_winrate_as_of(analyst, firm, signal_date_str)
        if n_ratings >= 10 and win_rate < 0.60:
            print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: Win-rate too low ({win_rate*100:.1f}%, n={n_ratings})")
            continue
            
        # Volume Spike Check
        df_bars = bars_dict.get(ticker)
        if df_bars is None or df_bars.empty:
            print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: No historical price bars found")
            continue
            
        # Resolve actual trading day on/after signal_date
        df_after = df_bars[df_bars.index.date >= signal_date]
        if df_after.empty:
            print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: No trading days found on or after signal date")
            continue
            
        actual_signal_dt = df_after.index[0]
        actual_signal_date = actual_signal_dt.date()
        
        day_t_bar = df_after.iloc[0]
        prior_bars = df_bars[df_bars.index.date < actual_signal_date].tail(30)
        if len(prior_bars) < 20:
            print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: Insufficient prior volume history (n={len(prior_bars)} < 20)")
            continue
            
        volume_sma_30 = prior_bars['volume'].mean()
        day_t_volume = day_t_bar['volume']
        day_t_close = day_t_bar['close']
        day_t_open = day_t_bar['open']
        
        spike_multiple = day_t_volume / volume_sma_30 if volume_sma_30 > 0 else 0.0
        positive_close = day_t_close > day_t_open
        
        if (1.5 <= spike_multiple <= 4.0) and positive_close:
            # Find entry (T+1 Open relative to actual_signal_date)
            df_entry = df_bars[df_bars.index.date > actual_signal_date]
            if not df_entry.empty:
                entry_date = df_entry.index[0].date()
                entry_price = float(df_entry.iloc[0]['open'])
                passed_signals.append({
                    "ticker": ticker,
                    "signal_date": actual_signal_date,
                    "entry_date": entry_date,
                    "entry_price": entry_price,
                    "volume_spike": spike_multiple
                })
                print(f"  {ticker} on {signal_date_str} ({firm}) -> PASSED: Vol Spike={spike_multiple:.2f}x, Close > Open, Entry on {entry_date}")
            else:
                print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: No T+1 entry bar found")
        else:
            reasons = []
            if not positive_close:
                reasons.append(f"Negative Close (Open={day_t_open}, Close={day_t_close})")
            if spike_multiple < 1.5:
                reasons.append(f"Spike too low ({spike_multiple:.2f}x)")
            elif spike_multiple > 4.0:
                reasons.append(f"Spike too high ({spike_multiple:.2f}x)")
            print(f"  {ticker} on {signal_date_str} ({firm}) -> REJECTED: Volume check failed ({', '.join(reasons)})")
            
    print(f"\nSignals that passed both filters: {len(passed_signals)}")
    for p in passed_signals:
        print(f"  {p['ticker']} | Signal: {p['signal_date']} | Entry: {p['entry_date']} at ${p['entry_price']:.2f} | Volume Spike: {p['volume_spike']:.2f}x")
        
    if not passed_signals:
        print("No signals passed the filters. No trades would have been made.")
        return
        
    # 4. Simulate Portfolio
    spy_bars = bars_dict.get('SPY')
    trading_days = sorted(list(spy_bars[(spy_bars.index.date >= start_date.date()) & (spy_bars.index.date <= end_date.date())].index.date))
    
    cash = 100000.0
    positions = []
    trades_log = []
    
    for today in trading_days:
        # A. Process Exits
        active_positions = []
        for pos in positions:
            ticker = pos['ticker']
            qty = pos['qty']
            entry_price = pos['entry_price']
            highest_price = pos['highest_price']
            entry_date = pos['entry_date']
            
            df_bars = bars_dict.get(ticker)
            ticker_close_today = df_bars[df_bars.index.date <= today]
            if ticker_close_today.empty:
                active_positions.append(pos)
                continue
                
            current_price = float(ticker_close_today.iloc[-1]['close'])
            new_highest = max(highest_price, current_price)
            pos['highest_price'] = new_highest
            
            days_held = len(ticker_close_today[ticker_close_today.index.date >= entry_date])
            
            exit_triggered = False
            exit_reason = ""
            
            # 8% Trailing Stop
            stop_price = new_highest * 0.92
            if current_price <= stop_price:
                exit_triggered = True
                exit_reason = "8% Trailing Stop"
                
            # 15-day SMA Drop
            if not exit_triggered and len(ticker_close_today) >= 15:
                sma_15 = ticker_close_today['close'].tail(15).mean()
                if current_price < sma_15:
                    exit_triggered = True
                    exit_reason = "15-day SMA Drop"
                    
            # 63-day Hold Expiration
            if not exit_triggered and days_held >= 63:
                exit_triggered = True
                exit_reason = "Hold Period Expired"
                
            if exit_triggered:
                sell_val = qty * current_price
                cash += sell_val
                pnl = qty * (current_price - entry_price)
                ret_pct = ((current_price - entry_price) / entry_price) * 100
                trades_log.append({
                    "Ticker": ticker,
                    "Entry Date": entry_date.strftime("%Y-%m-%d"),
                    "Exit Date": today.strftime("%Y-%m-%d"),
                    "Qty": qty,
                    "Entry Price": entry_price,
                    "Exit Price": current_price,
                    "P&L ($)": pnl,
                    "Return (%)": ret_pct,
                    "Reason": exit_reason
                })
            else:
                active_positions.append(pos)
                
        positions = active_positions
        
        # B. Process Entries
        todays_signals = [p for p in passed_signals if p['entry_date'] == today]
        currently_owned = {p['ticker'] for p in positions}
        todays_signals = [s for s in todays_signals if s['ticker'] not in currently_owned]
        
        if todays_signals and cash > 0:
            num_trades = len(todays_signals)
            MAX_CASH_PER_TRADE = 20000.0
            cash_per_trade = min(cash / num_trades, MAX_CASH_PER_TRADE)
            
            for sig in todays_signals:
                ticker = sig['ticker']
                entry_price = sig['entry_price']
                
                df_bars = bars_dict.get(ticker)
                ticker_close_today = df_bars[df_bars.index.date <= today]
                if len(ticker_close_today) >= 15:
                    sma_15 = ticker_close_today['close'].tail(15).mean()
                    if entry_price < sma_15:
                        continue
                        
                qty = int(cash_per_trade / entry_price)
                if qty > 0:
                    cash -= qty * entry_price
                    positions.append({
                        "ticker": ticker,
                        "qty": qty,
                        "entry_price": entry_price,
                        "entry_date": today,
                        "highest_price": entry_price
                    })

    # C. Final Portfolio Valuation
    portfolio_value = cash
    open_trades_count = 0
    for pos in positions:
        ticker = pos['ticker']
        qty = pos['qty']
        df_bars = bars_dict.get(ticker)
        current_price = float(df_bars.iloc[-1]['close'])
        portfolio_value += qty * current_price
        open_trades_count += 1
        
    print("\n=== Simulation Results (Composite 1500) ===")
    print(f"Starting Cash: $100,000.00")
    print(f"Ending Cash:   ${cash:,.2f}")
    print(f"Open Positions Value: ${portfolio_value - cash:,.2f} ({open_trades_count} positions)")
    print(f"Total Portfolio Value: ${portfolio_value:,.2f}")
    print(f"Total Return:  {((portfolio_value - 100000.0)/100000.0)*100:+.2f}%")
    
    print("\n--- Completed Trades Log ---")
    if not trades_log:
        print("  No completed trades.")
    else:
        for t in trades_log:
            print(f"  {t['Ticker']} | Enter: {t['Entry Date']} at ${t['Entry Price']:.2f} | Exit: {t['Exit Date']} at ${t['Exit Price']:.2f} | P&L: {t['P&L ($)']:+,.2f} ({t['Return (%)']:+.2f}%) | Reason: {t['Reason']}")

    print("\n--- Currently Open Positions ---")
    if not positions:
        print("  No open positions.")
    else:
        for p in positions:
            ticker = p['ticker']
            qty = p['qty']
            df_bars = bars_dict.get(ticker)
            current_price = float(df_bars.iloc[-1]['close'])
            pnl = qty * (current_price - p['entry_price'])
            ret_pct = ((current_price - p['entry_price']) / p['entry_price']) * 100
            print(f"  {ticker} | Enter: {p['entry_date'].strftime('%Y-%m-%d')} at ${p['entry_price']:.2f} | Current: ${current_price:.2f} | Unpl: {pnl:+,.2f} ({ret_pct:+.2f}%)")

if __name__ == "__main__":
    main()
