import os
import requests
import pandas as pd
from datetime import datetime, timedelta
import yfinance as yf

def check_earnings_proximity_finnhub(symbol, signal_date_str, finnhub_key):
    """
    Queries Finnhub for earnings calendar around signal_date (+/- 5 days)
    and checks if the symbol had a positive earnings surprise (beat or match).
    """
    if not finnhub_key:
        return 0, None, None, None
        
    try:
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
    except Exception:
        return 0, None, None, None
        
    start_date = (signal_date - timedelta(days=5)).strftime("%Y-%m-%d")
    end_date = (signal_date + timedelta(days=5)).strftime("%Y-%m-%d")
    
    url = f"https://finnhub.io/api/v1/calendar/earnings?from={start_date}&to={end_date}&token={finnhub_key}"
    
    try:
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            return 0, None, None, None
            
        data = response.json()
        calendar = data.get("earningsCalendar", [])
        
        # Filter for our symbol
        symbol_events = [item for item in calendar if item.get("symbol", "").upper() == symbol.upper()]
        
        if not symbol_events:
            return 0, None, None, None
            
        # Find closest earnings event
        closest_event = None
        min_diff = 9999
        
        for item in symbol_events:
            event_date_str = item.get("date")
            if not event_date_str:
                continue
            try:
                event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
                diff = abs((event_date - signal_date).days)
                if diff < min_diff:
                    min_diff = diff
                    closest_event = item
            except Exception:
                continue
                
        if closest_event and min_diff <= 5:
            earnings_date_str = closest_event.get("date")
            eps_actual = closest_event.get("epsActual")
            eps_estimate = closest_event.get("epsEstimate")
            rev_actual = closest_event.get("revenueActual")
            rev_estimate = closest_event.get("revenueEstimate")
            
            # Check if it was a beat or match
            beat = False
            if eps_actual is not None and eps_estimate is not None:
                beat = float(eps_actual) >= float(eps_estimate)
            elif rev_actual is not None and rev_estimate is not None:
                beat = float(rev_actual) >= float(rev_estimate)
                
            return (1 if beat else 0), earnings_date_str, eps_actual, eps_estimate
            
    except Exception as e:
        print(f"Finnhub earnings query error for {symbol}: {e}")
        
    return 0, None, None, None

def check_earnings_proximity_yfinance(symbol, signal_date_str):
    """
    Queries yfinance for earnings dates history, looks for an event within +/- 5 days
    of signal_date_str, and checks if it was a beat.
    """
    try:
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
    except Exception:
        return 0, None, None, None
        
    try:
        stock = yf.Ticker(symbol)
        df = stock.earnings_dates
        if df is None or df.empty:
            return 0, None, None, None
            
        # Normalize the DataFrame index (Earnings Date) to timezone-naive date
        df = df.copy()
        if hasattr(df.index, 'tz_convert') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        elif hasattr(df.index, 'tz_localize') and df.index.tz is None:
            pass
            
        # Calculate distance to signal date
        min_diff = 9999
        closest_row = None
        closest_date_val = None
        
        for idx, row in df.iterrows():
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
            
            # If actual is NaN, we cannot confirm a beat (could be in the future)
            if pd.isna(eps_actual) or pd.isna(eps_estimate):
                return 0, earnings_date_str, None, None
                
            # Check if it was a beat or match
            beat = float(eps_actual) >= float(eps_estimate)
            return (1 if beat else 0), earnings_date_str, float(eps_actual), float(eps_estimate)
            
    except Exception as e:
        print(f"yfinance earnings query error for {symbol}: {e}")
        
    return 0, None, None, None

def get_earnings_proximity(symbol, signal_date_str, finnhub_key=None):
    """
    Unified check: Try Finnhub first, fall back to yfinance.
    """
    # For dates more than 25 days in the past, Finnhub free tier will return empty,
    # so we should immediately check if signal_date is old and route to yfinance!
    # Today's date minus 25 days:
    try:
        sig_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
        days_ago = (datetime.now().date() - sig_date).days
        if days_ago > 25:
            # Older than 25 days, use yfinance directly to avoid wasting Finnhub calls
            return check_earnings_proximity_yfinance(symbol, signal_date_str)
    except Exception:
        pass
        
    # Otherwise, try Finnhub first
    if finnhub_key:
        is_prox, earn_date, act, est = check_earnings_proximity_finnhub(symbol, signal_date_str, finnhub_key)
        if is_prox or earn_date is not None:
            return is_prox, earn_date, act, est
            
    # Fallback to yfinance
    return check_earnings_proximity_yfinance(symbol, signal_date_str)
