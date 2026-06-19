import os
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

def get_spy_regime_as_of(signal_date_str, alpaca_client):
    """
    Fetch SPY daily close prices for the 75 calendar days prior to signal_date_str,
    calculate the 50-day SMA, and return "Bull" if SPY's close on signal_date_str
    (or latest trading day) is > 50-day SMA, else "Bear".
    """
    try:
        sig_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
    except Exception:
        return "Unknown"
        
    start_date = sig_date - timedelta(days=75)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(sig_date, datetime.max.time())
    
    try:
        bars = alpaca_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols='SPY',
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            feed=DataFeed.IEX,
            adjustment='split'
        ))
        df = bars.df
        if df is None or df.empty:
            return "Unknown"
            
        df = df.copy()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs('SPY', level='symbol')
        df.index = df.index.tz_localize(None)
        df = df.sort_index()
        
        if len(df) < 50:
            return "Bear"
            
        # Calculate 50-day SMA of close prices
        df['sma_50'] = df['close'].rolling(window=50).mean()
        
        # Get the row closest to/on the signal date
        df_sig = df[df.index.date <= sig_date]
        if df_sig.empty:
            return "Unknown"
            
        last_row = df_sig.iloc[-1]
        close_price = float(last_row['close'])
        sma_val = float(last_row['sma_50'])
        
        if pd.isna(sma_val):
            return "Bear"
            
        return "Bull" if close_price > sma_val else "Bear"
    except Exception as e:
        print(f"Error calculating SPY regime: {e}")
        return "Unknown"

def normalize_grade(grade):
    if not grade:
        return None
    g = str(grade).lower().strip()
    
    buys = {'buy', 'overweight', 'outperform', 'strong buy', 'accumulate', 'add', 'market outperform', 'sector outperform', 'positive', 'long-term buy', 'conviction buy'}
    holds = {'hold', 'neutral', 'equal-weight', 'market perform', 'sector perform', 'equal weight', 'peer perform', 'in-line', 'inline', 'sector weight', 'fair value', 'mixed', 'average'}
    sells = {'sell', 'underweight', 'underperform', 'reduce', 'strong sell', 'sector underperform', 'negative'}
    
    if g in buys or any(x in g for x in ['buy', 'outperform', 'overweight']):
        return 'Buy'
    elif g in sells or any(x in g for x in ['sell', 'underperform', 'underweight', 'reduce']):
        return 'Sell'
    else:
        return 'Hold'

def get_consensus_divergence_as_of(ticker, signal_date_str):
    """
    Fetch ticker upgrades/downgrades from yfinance before signal_date_str,
    classify ToGrade per firm into Buy/Hold/Sell, and calculate consensus metrics:
    - consensus_buy_ratio
    - consensus_hold_ratio
    - consensus_sell_ratio
    - consensus_divergence_score = 1.0 - consensus_buy_ratio
    """
    try:
        target_date = datetime.strptime(signal_date_str, "%Y-%m-%d")
        stock = yf.Ticker(ticker)
        
        if not hasattr(stock, 'upgrades_downgrades'):
            return 0.5, 0.4, 0.1, 0.5
            
        df = stock.upgrades_downgrades
        if df is None or df.empty:
            return 0.5, 0.4, 0.1, 0.5
            
        df = df.copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
            
        df_prior = df[df.index < target_date]
        if df_prior.empty:
            return 0.5, 0.4, 0.1, 0.5
            
        df_prior = df_prior.sort_index()
        last_ratings = df_prior.groupby('Firm').last()
        
        classified = last_ratings['ToGrade'].apply(normalize_grade)
        counts = classified.value_counts()
        
        b = int(counts.get('Buy', 0))
        h = int(counts.get('Hold', 0))
        s = int(counts.get('Sell', 0))
        total = b + h + s
        
        if total == 0:
            return 0.5, 0.4, 0.1, 0.5
            
        buy_ratio = round(b / total, 4)
        hold_ratio = round(h / total, 4)
        sell_ratio = round(s / total, 4)
        div_score = round(1.0 - buy_ratio, 4)
        
        return buy_ratio, hold_ratio, sell_ratio, div_score
    except Exception as e:
        print(f"Error calculating consensus for {ticker}: {e}")
        return 0.5, 0.4, 0.1, 0.5
