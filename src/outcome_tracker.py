import os
import time
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# Local imports
from src.db import get_connection

load_dotenv()

class SPYFinder:
    def __init__(self, alpaca_client):
        try:
            start_date = datetime.now().date() - timedelta(days=730)
            bars = alpaca_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols='SPY',
                timeframe=TimeFrame.Day,
                start=datetime.combine(start_date, datetime.min.time()),
                end=datetime.combine(datetime.now().date(), datetime.max.time()),
                feed=DataFeed.IEX,
                adjustment='split'
            ))
            df = bars.df
            if df is not None and not df.empty:
                df = df.copy()
                if isinstance(df.index, pd.MultiIndex):
                    df = df.xs('SPY', level='symbol')
                df.index = df.index.tz_localize(None)
                df = df.sort_index()
                self.df = df
            else:
                self.df = None
        except Exception as e:
            print(f"Error fetching SPY bulk data: {e}")
            self.df = None
            
    def get_close_price(self, date_str):
        if self.df is None or self.df.empty:
            return None
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            df_after = self.df[self.df.index.date >= target_date]
            if not df_after.empty:
                return float(df_after['close'].iloc[0])
        except Exception:
            pass
        return None

def get_close_price(ticker, target_date_str, alpaca_client):
    """
    Retrieves the closing price for a stock on or shortly after (up to 5 days) target_date_str.
    This handles holidays/weekends robustly. Returns float or None if delisted/unavailable.
    """
    try:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    except Exception:
        return None
        
    start_dt = datetime.combine(target_date, datetime.min.time())
    end_dt = datetime.combine(target_date + timedelta(days=5), datetime.max.time())
    
    try:
        bars = alpaca_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            feed=DataFeed.IEX,
            adjustment='split'
        ))
        
        df = bars.df
        if df is not None and not df.empty:
            df = df.copy()
            if isinstance(df.index, pd.MultiIndex):
                if ticker in df.index.levels[0]:
                    df = df.xs(ticker, level='symbol')
                else:
                    first_symbol = df.index.levels[0][0]
                    df = df.xs(first_symbol, level='symbol')
            df.index = df.index.tz_localize(None)
            df = df.sort_index()
            return float(df['close'].iloc[0])
    except Exception as e:
        print(f"Error resolving close price for {ticker} on/after {target_date_str}: {e}")
        
    return None

def evaluate_pending_ratings():
    """
    Evaluates pending ratings in the analyst_history table once 63 trading days have passed.
    """
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not alpaca_key or not alpaca_secret:
        raise ValueError("Alpaca API credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    
    print("Pre-fetching SPY benchmarks in bulk...")
    spy_finder = SPYFinder(alpaca_client)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT * FROM analyst_history 
        WHERE status = 'Pending' 
          AND evaluation_date <= ?
          AND entry_price IS NOT NULL
    """, (today_str,))
    pending_ratings = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"Found {len(pending_ratings)} pending analyst recommendations ready for evaluation.")
    
    evaluated_count = 0
    withdrawn_count = 0
    
    # Simple stock price cache to speed up duplicate queries
    stock_price_cache = {}
    
    for idx, rating in enumerate(pending_ratings):
        if idx > 0:
            time.sleep(1.1)
            
        r_id = rating['id']
        ticker = rating['ticker']
        signal_date = rating['signal_date']
        eval_date = rating['evaluation_date']
        entry_price = rating['entry_price']
        
        print(f"Evaluating rating {r_id}: {ticker} (Entry: ${entry_price:.2f} on {signal_date}, Eval Date: {eval_date})...")
        
        cache_key = (ticker, eval_date)
        if cache_key in stock_price_cache:
            stock_price = stock_price_cache[cache_key]
        else:
            stock_price = get_close_price(ticker, eval_date, alpaca_client)
            stock_price_cache[cache_key] = stock_price
            
        if stock_price is None:
            print(f" -> Mark Withdrawn: No price data available for {ticker} on {eval_date}.")
            conn_write = get_connection()
            cursor_write = conn_write.cursor()
            cursor_write.execute("""
                UPDATE analyst_history
                SET status = 'Withdrawn'
                WHERE id = ?
            """, (r_id,))
            conn_write.commit()
            conn_write.close()
            withdrawn_count += 1
            continue
            
        spy_price_start = spy_finder.get_close_price(signal_date)
        spy_price_end = spy_finder.get_close_price(eval_date)
        
        if spy_price_start is None or spy_price_end is None:
            print(" -> Skip: SPY benchmarks could not be resolved.")
            continue
            
        stock_return = (stock_price - entry_price) / entry_price
        spy_return = (spy_price_end - spy_price_start) / spy_price_start
        
        won = (stock_return > 0) and (stock_return > spy_return)
        status = 'Won' if won else 'Lost'
        
        stock_return_pct = round(stock_return * 100, 2)
        spy_return_pct = round(spy_return * 100, 2)
        
        print(f" -> Result: {status} (Stock: {stock_return_pct:+.1f}%, SPY: {spy_return_pct:+.1f}%)")
        
        conn_write = get_connection()
        cursor_write = conn_write.cursor()
        cursor_write.execute("""
            UPDATE analyst_history
            SET exit_price = ?,
                stock_return_pct = ?,
                spy_return_pct = ?,
                status = ?
            WHERE id = ?
        """, (stock_price, stock_return_pct, spy_return_pct, status, r_id))
        conn_write.commit()
        conn_write.close()
        evaluated_count += 1
        
    print(f"Ratings evaluation completed. Evaluated {evaluated_count}, withdrawn {withdrawn_count}.")

def evaluate_pending_trades():
    """
    Evaluates open positions in the trades table. Exits positions after exactly 63 trading days.
    """
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not alpaca_key or not alpaca_secret:
        raise ValueError("Alpaca API credentials missing.")
        
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    
    print("Pre-fetching SPY benchmarks for trade evaluation...")
    spy_finder = SPYFinder(alpaca_client)
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Select all trades where exit_date IS NULL
    cursor.execute("SELECT * FROM trades WHERE exit_date IS NULL")
    open_trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"Found {len(open_trades)} open trades in ledger to check for exit.")
    
    exited_count = 0
    
    for idx, trade in enumerate(open_trades):
        trade_id = trade['id']
        ticker = trade['ticker']
        entry_date = trade['entry_date']
        entry_price = trade['entry_price']
        
        if not entry_date or not entry_price:
            continue
            
        # Check if this is a live paper position
        paper_status = trade.get('paper_trade_status') or 'none'
        if paper_status == 'open':
            try:
                print(f"Checking live paper position exits for {ticker} (Trade ID: {trade_id})...")
                trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
                
                # Fetch recent daily bars (last 30 days) to calculate 10-day SMA and get latest close
                today = datetime.now().date()
                start_date = today - timedelta(days=30)
                start_dt = datetime.combine(start_date, datetime.min.time())
                end_dt = datetime.combine(today, datetime.max.time())
                
                bars = alpaca_client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=ticker,
                    timeframe=TimeFrame.Day,
                    start=start_dt,
                    end=end_dt,
                    feed=DataFeed.IEX,
                    adjustment='split'
                ))
                df_ticker = bars.df
                if df_ticker is not None and not df_ticker.empty:
                    df_ticker = df_ticker.copy()
                    if isinstance(df_ticker.index, pd.MultiIndex):
                        df_ticker = df_ticker.xs(ticker, level='symbol')
                    df_ticker.index = df_ticker.index.tz_localize(None)
                    df_ticker = df_ticker.sort_index()
                    
                    current_price = float(df_ticker['close'].iloc[-1])
                    print(f"  Current price for {ticker}: ${current_price:.2f}")
                    
                    highest_so_far = trade['highest_price_recorded'] or entry_price
                    highest_price_recorded = max(highest_so_far, current_price)
                    
                    # Update highest recorded price in database immediately
                    conn_up = get_connection()
                    cursor_up = conn_up.cursor()
                    cursor_up.execute("""
                        UPDATE trades
                        SET highest_price_recorded = ?
                        WHERE id = ?
                    """, (highest_price_recorded, trade_id))
                    conn_up.commit()
                    conn_up.close()
                    
                    # 1. Check 8% Rolling Stop Loss
                    stop_price = highest_price_recorded * 0.92
                    exit_triggered = False
                    exit_reason = ""
                    
                    if current_price <= stop_price:
                        exit_triggered = True
                        exit_reason = "8% Trailing Stop"
                        print(f"  -> Exit Triggered: {ticker} price ${current_price:.2f} is below trailing stop price ${stop_price:.2f} (Peak: ${highest_price_recorded:.2f})")
                    
                    # 2. Check 10-day SMA Drop Sell Rule (only if not already stopped out)
                    if not exit_triggered and len(df_ticker) >= 10:
                        sma_10 = df_ticker['close'].tail(10).mean()
                        if current_price < sma_10:
                            exit_triggered = True
                            exit_reason = "10-day SMA Drop"
                            print(f"  -> Exit Triggered: {ticker} price ${current_price:.2f} dropped below 10-day SMA ${sma_10:.2f}")
                            
                    # Execute SELL order if triggered
                    if exit_triggered:
                        qty = trade['alpaca_qty']
                        if not qty or qty <= 0:
                            try:
                                pos = trading_client.get_open_position(ticker)
                                qty = int(pos.qty)
                            except Exception:
                                qty = 0
                                
                        if qty > 0:
                            print(f"  Submitting SELL market order for {qty} shares of {ticker}...")
                            trading_client.submit_order(MarketOrderRequest(
                                symbol=ticker,
                                qty=qty,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.DAY
                            ))
                            
                        # Calculate returns
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        stock_return = (current_price - entry_price) / entry_price
                        spy_entry = spy_finder.get_close_price(entry_date)
                        spy_exit = spy_finder.get_close_price(today_str)
                        
                        spy_return = (spy_exit - spy_entry) / spy_entry if (spy_entry is not None and spy_exit is not None) else 0.0
                        excess_return = stock_return - spy_return
                        
                        stock_return_pct = round(stock_return * 100, 2)
                        spy_return_pct = round(spy_return * 100, 2)
                        excess_return_pct = round(excess_return * 100, 2)
                        
                        conn_up = get_connection()
                        cursor_up = conn_up.cursor()
                        cursor_up.execute("""
                            UPDATE trades
                            SET exit_price = ?,
                                exit_date = ?,
                                exit_reason = ?,
                                paper_trade_status = 'exited',
                                stock_return_pct = ?,
                                spy_return_pct = ?,
                                excess_return_pct = ?
                            WHERE id = ?
                        """, (current_price, today_str, exit_reason, stock_return_pct, spy_return_pct, excess_return_pct, trade_id))
                        conn_up.commit()
                        conn_up.close()
                        
                        exited_count += 1
                        print(f"  Exited trade for {ticker} updated in DB.")
                else:
                    print(f"  Warning: No bar data returned for {ticker} to check exits.")
            except Exception as e:
                print(f"  Error checking paper position exits for {ticker}: {e}")
            continue

        # Rate limit safety sleep
        if idx > 0:
            time.sleep(1.1)
            
        try:
            # Query SPY bars to find the 63rd trading session (market calendar days)
            entry_date_obj = datetime.strptime(entry_date, "%Y-%m-%d").date()
            start_dt = datetime.combine(entry_date_obj, datetime.min.time())
            end_dt = datetime.combine(entry_date_obj + timedelta(days=400), datetime.max.time())
            
            spy_bars = alpaca_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols='SPY',
                timeframe=TimeFrame.Day,
                start=start_dt,
                end=end_dt,
                feed=DataFeed.IEX,
                adjustment='split'
            ))
            
            df_spy_local = spy_bars.df
            if df_spy_local is None or df_spy_local.empty:
                continue
                
            df_spy_local = df_spy_local.copy()
            if isinstance(df_spy_local.index, pd.MultiIndex):
                df_spy_local = df_spy_local.xs('SPY', level='symbol')
            df_spy_local.index = df_spy_local.index.tz_localize(None)
            df_spy_local = df_spy_local.sort_index()
            
            if len(df_spy_local) >= 63:
                exit_date_val = df_spy_local.index[62].date()
                exit_date_str = exit_date_val.strftime("%Y-%m-%d")
                
                # Fetch stock close price on or after exit_date_str
                exit_price = get_close_price(ticker, exit_date_str, alpaca_client)
                if exit_price is None:
                    print(f" -> Warning: Could not resolve exit price for {ticker} on/after {exit_date_str}. Skipping.")
                    continue
                    
                # Fetch SPY prices at entry_date and exit_date to calculate returns
                spy_entry = spy_finder.get_close_price(entry_date)
                spy_exit = spy_finder.get_close_price(exit_date_str)
                
                if spy_entry is not None and spy_exit is not None:
                    stock_return = (exit_price - entry_price) / entry_price
                    spy_return = (spy_exit - spy_entry) / spy_entry
                    excess_return = stock_return - spy_return
                    
                    stock_return_pct = round(stock_return * 100, 2)
                    spy_return_pct = round(spy_return * 100, 2)
                    excess_return_pct = round(excess_return * 100, 2)
                    
                    print(f" -> Exiting trade {trade_id} ({ticker}): holding period met. Exit on {exit_date_str} at ${exit_price:.2f} (Return: {stock_return_pct:+.2f}%)")
                    
                    conn_write = get_connection()
                    cursor_write = conn_write.cursor()
                    cursor_write.execute("""
                        UPDATE trades
                        SET exit_price = ?,
                            exit_date = ?,
                            exit_reason = 'Hold Period Expired',
                            stock_return_pct = ?,
                            spy_return_pct = ?,
                            excess_return_pct = ?
                        WHERE id = ?
                    """, (exit_price, exit_date_str, stock_return_pct, spy_return_pct, excess_return_pct, trade_id))
                    conn_write.commit()
                    conn_write.close()
                    exited_count += 1
            else:
                # Holding period not met yet
                pass
        except Exception as e:
            print(f"Error checking exit for trade {trade_id} ({ticker}): {e}")
            
    print(f"Trades evaluation completed. Exited {exited_count} positions.")

def run_outcome_tracker():
    evaluate_pending_ratings()
    evaluate_pending_trades()
    
    # Dynamically correct any stock split issues
    try:
        from scripts.fix_splits import fix_splits
        fix_splits()
    except Exception as e:
        print(f"Failed to run split correction: {e}")

if __name__ == "__main__":
    run_outcome_tracker()
