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

# Trading & scoring imports
from alpaca.trading.client import TradingClient
from src.regime_consensus import get_spy_regime_as_of, get_consensus_divergence_as_of

# Local imports
from src.db import get_connection

load_dotenv()

def normalize_firm_name(firm):
    """
    Standardizes firm names scraped from MarketBeat to match those stored in analyst_history database.
    """
    if not firm:
        return "N/A"
    
    firm_lower = firm.lower()
    
    mapping = {
        "jpmorgan chase & co.": "JP Morgan",
        "jpmorgan": "JP Morgan",
        "jp morgan": "JP Morgan",
        "bank of america": "B of A Securities",
        "bofas": "B of A Securities",
        "bofa securities": "B of A Securities",
        "bofa": "B of A Securities",
        "citigroup": "Citigroup",
        "citi": "Citigroup",
        "truist financial": "Truist Securities",
        "truist": "Truist Securities",
        "deutsche bank aktiengesellschaft": "Deutsche Bank",
        "deutsche bank": "Deutsche Bank",
        "ubs group": "UBS",
        "ubs": "UBS",
        "jefferies financial group": "Jefferies",
        "jefferies": "Jefferies",
        "wells fargo & company": "Wells Fargo",
        "wells fargo": "Wells Fargo",
        "royal bank of canada": "RBC Capital",
        "rbc capital markets": "RBC Capital",
        "rbc": "RBC Capital",
        "bmo capital markets": "BMO Capital",
        "bmo": "BMO Capital",
        "raymond james financial": "Raymond James",
        "raymond james": "Raymond James",
        "evercore isi group": "Evercore ISI Group",
        "evercore": "Evercore ISI Group",
        "goldman sachs group": "Goldman Sachs",
        "goldman sachs": "Goldman Sachs",
        "morgan stanley": "Morgan Stanley",
        "barclays": "Barclays",
        "mizuho": "Mizuho",
        "mizuho financial group": "Mizuho",
        "scotiabank": "Scotiabank",
        "bnp paribas exane": "BNP Paribas",
        "bnp paribas": "BNP Paribas",
        "keefe, bruyette & woods": "Keefe, Bruyette & Woods",
        "kbw": "Keefe, Bruyette & Woods",
        "piper sandler": "Piper Sandler",
        "wedbush": "Wedbush",
        "wolfe research": "Wolfe Research",
        "h.c. wainwright": "HC Wainwright & Co.",
        "hc wainwright": "HC Wainwright & Co.",
        "stephens": "Stephens & Co.",
        "needham & company llc": "Needham",
        "needham": "Needham",
        "td cowen": "TD Cowen",
        "cowen": "TD Cowen",
        "guggenheim": "Guggenheim",
        "guggenheim securities": "Guggenheim",
        "oppenheimer": "Oppenheimer",
        "stifel nicolaus": "Stifel",
        "stifel": "Stifel",
        "btig": "BTIG",
        "cantor fitzgerald": "Cantor Fitzgerald",
        "da davidson": "DA Davidson",
        "keybanc": "Keybanc",
        "keybanc capital markets": "Keybanc",
        "loop capital": "Loop Capital",
        "melius research": "Melius Research",
        "rosenblatt": "Rosenblatt",
        "rosenblatt securities": "Rosenblatt",
        "seaport global": "Seaport Global",
        "seaport global securities": "Seaport Global",
        "telsey advisory group": "Telsey Advisory Group",
        "william blair": "William Blair"
    }
    
    for key, val in mapping.items():
        if key in firm_lower:
            return val
            
    # Fallback title case clean
    clean_firm = firm.replace("Aktiengesellschaft", "").replace("Group", "").replace("Financial", "").replace("Securities", "").strip()
    return clean_firm

def get_winrate_as_of(analyst_name, firm, as_of_date):
    """
    Query analyst_history for the analyst's ratings closed strictly before signal_date.
    If the analyst has fewer than 10 ratings, falls back to the firm-level ratings (where analyst_name = 'N/A').
    Returns (win_rate, total_count)
    """
    normalized_firm = normalize_firm_name(firm)
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Try to query by specific analyst name and normalized firm
    cursor.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as won
        FROM analyst_history
        WHERE analyst_name = ?
          AND firm = ?
          AND evaluation_date < ?
          AND status IN ('Won', 'Lost')
    """, [analyst_name, normalized_firm, as_of_date])
    
    row = cursor.fetchone()
    total = row['total'] if row['total'] is not None else 0
    won = row['won'] if row['won'] is not None else 0
    
    # 2. Fallback to firm-level if specific analyst has < 10 ratings
    if total < 10 and analyst_name != 'N/A':
        cursor.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as won
            FROM analyst_history
            WHERE analyst_name = 'N/A'
              AND firm = ?
              AND evaluation_date < ?
              AND status IN ('Won', 'Lost')
        """, [normalized_firm, as_of_date])
        row_firm = cursor.fetchone()
        total_firm = row_firm['total'] if row_firm['total'] is not None else 0
        won_firm = row_firm['won'] if row_firm['won'] is not None else 0
        
        if total_firm >= 10:
            total = total_firm
            won = won_firm
            
    conn.close()
    
    if total == 0:
        return 0.0, 0
    return won / total, total

def get_volume_data_as_of(ticker, signal_date_str, alpaca_client):
    """
    Fetches daily stock bars from Alpaca, finds the first trading day on or after signal_date_str,
    checks that day's session structure (Close > Open) and computes the volume spike multiple
    relative to the 30 trading days prior to that day.
    """
    try:
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
    except Exception:
        return None, None, None, None
        
    start_date = signal_date - timedelta(days=60)  # extra buffer for trading days
    
    # Query up to today to capture first trading day on/after signal_date
    today = datetime.now().date()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(today, datetime.max.time())
    
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
        if df is None or df.empty:
            return None, None, None, None
            
        # Normalize the DataFrame index to tz-naive dates for easy comparison
        df = df.copy()
        if isinstance(df.index, pd.MultiIndex):
            if ticker in df.index.levels[0]:
                df = df.xs(ticker, level='symbol')
            else:
                first_symbol = df.index.levels[0][0]
                df = df.xs(first_symbol, level='symbol')
        df.index = df.index.tz_localize(None)
        df = df.sort_index()
        
        # Find first trading day on or after signal_date
        df_after = df[df.index.date >= signal_date]
        if df_after.empty:
            return None, None, None, None
            
        actual_signal_dt = df_after.index[0]
        actual_signal_date = actual_signal_dt.date()
        
        # Get Day T's bar (actual signal date)
        day_t_bar = df_after.iloc[0]
        
        # 30-day SMA uses only the 30 trading days BEFORE actual_signal_date
        prior_bars = df[df.index.date < actual_signal_date].tail(30)
        if len(prior_bars) < 20:  # require at least 20 trading days of volume history
            return None, None, None, None
            
        volume_sma_30 = prior_bars['volume'].mean()
        day_t_volume = day_t_bar['volume']
        day_t_close = day_t_bar['close']
        day_t_open = day_t_bar['open']
        
        spike_multiple = day_t_volume / volume_sma_30 if volume_sma_30 > 0 else 0.0
        positive_close = day_t_close > day_t_open
        
        # Return history up to actual_signal_date
        df_history = df[df.index.date <= actual_signal_date]
        
        return spike_multiple, positive_close, (day_t_close, day_t_open), df_history
    except Exception as e:
        print(f"Error fetching OHLCV for {ticker} as of {signal_date_str}: {e}")
        return None, None, None, None

def run_filter(alpaca_client=None):
    if alpaca_client is None:
        alpaca_key = os.getenv("ALPACA_API_KEY")
        alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
        
        if not alpaca_key or not alpaca_secret:
            raise ValueError("Alpaca API credentials missing. Set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
            
        alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    
    # 1. Fetch pending signals and close connection immediately
    conn = get_connection()
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        SELECT * FROM daily_signals 
        WHERE filter_status = 'pending' 
          AND signal_date <= ?
    """, (today_str,))
    pending_signals = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    print(f"Found {len(pending_signals)} pending signals to process.")
    live_passing_trades = []
    tickers_passed_today = set()
    
    for idx, signal in enumerate(pending_signals):
        # Rate limit safety sleep
        if idx > 0 and not hasattr(alpaca_client, 'is_mock'):
            time.sleep(1.1)
            
        sig_id = signal['id']
        ticker = signal['ticker']
        analyst = signal['analyst_name'] or "N/A"
        firm = signal['firm'] or "N/A"
        signal_date = signal['signal_date']
        target_price = signal['target_price']
        is_earnings_prox = signal['is_earnings_proximate']
        earnings_date = signal['earnings_date']
        eps_actual = signal['eps_actual']
        eps_estimate = signal['eps_estimate']
        
        print(f"Processing signal {sig_id}: {ticker} upgraded by {analyst} ({firm}) on {signal_date}...")
        
        # Evaluate Analyst Win-Rate (Filter A)
        win_rate, n_ratings = get_winrate_as_of(analyst, firm, signal_date)
        
        winrate_passed = False
        winrate_bypassed = False
        
        if n_ratings < 10:
            # Bypass logic during trial period (n < 10)
            winrate_bypassed = True
            winrate_passed = True
            print(f"WINRATE_BYPASSED: {analyst} at {firm} — only {n_ratings} closed ratings (< 10 minimum). Proceeding to volume filter.")
        else:
            winrate_passed = (win_rate >= 0.60)
            
        if not winrate_passed:
            print(f" -> Rejected: Analyst win rate is {win_rate * 100:.1f}% (< 60% threshold)")
            conn_write = get_connection()
            cursor_write = conn_write.cursor()
            cursor_write.execute("""
                UPDATE daily_signals 
                SET filter_status = 'rejected_winrate', volume_spike_multiple = NULL 
                WHERE id = ?
            """, (sig_id,))
            conn_write.commit()
            conn_write.close()
            continue
            
        # Evaluate Volume Spike (Filter B)
        spike_multiple, positive_close, prices, df_history = get_volume_data_as_of(ticker, signal_date, alpaca_client)
        
        if spike_multiple is None:
            print(" -> Rejected: No OHLCV close data found for signal date.")
            conn_write = get_connection()
            cursor_write = conn_write.cursor()
            cursor_write.execute("""
                UPDATE daily_signals 
                SET filter_status = 'rejected_no_close_data', volume_spike_multiple = NULL 
                WHERE id = ?
            """, (sig_id,))
            conn_write.commit()
            conn_write.close()
            continue
            
        # Check volume spike in band [1.5, 4.0] and green close
        volume_passed = (1.5 <= spike_multiple <= 4.0) and positive_close
        
        if not volume_passed:
            reason = []
            if not positive_close:
                reason.append(f"close was negative ({prices[0]} <= {prices[1]})")
            if spike_multiple < 1.5:
                reason.append(f"volume spike was {spike_multiple:.2f}x (below 1.5x floor)")
            elif spike_multiple > 4.0:
                reason.append(f"volume spike was {spike_multiple:.2f}x (above 4.0x cap)")
                
            print(f" -> Rejected: Volume check failed ({', '.join(reason)})")
            
            conn_write = get_connection()
            cursor_write = conn_write.cursor()
            cursor_write.execute("""
                UPDATE daily_signals 
                SET filter_status = 'rejected_volume', volume_spike_multiple = ? 
                WHERE id = ?
            """, (spike_multiple, sig_id))
            conn_write.commit()
            conn_write.close()
            continue
            
        # Passed both filters - confirmed buy!
        print(f" -> Passed! Confirmed buy with {spike_multiple:.2f}x volume spike.")
        
        # Resolve T+1 trading day Open price for trade entry
        entry_date = None
        entry_price = None
        
        try:
            actual_signal_date = df_history.index[-1].date()
            t1_start = datetime.combine(actual_signal_date + timedelta(days=1), datetime.min.time())
            t1_end = datetime.combine(actual_signal_date + timedelta(days=7), datetime.max.time())
            
            t1_bars = alpaca_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Day,
                start=t1_start,
                end=t1_end,
                feed=DataFeed.IEX,
                adjustment='split'
            ))
            
            df_after = t1_bars.df
            if df_after is not None and not df_after.empty:
                df_after = df_after.copy()
                if isinstance(df_after.index, pd.MultiIndex):
                    if ticker in df_after.index.levels[0]:
                        df_after = df_after.xs(ticker, level='symbol')
                    else:
                        first_symbol = df_after.index.levels[0][0]
                        df_after = df_after.xs(first_symbol, level='symbol')
                df_after.index = df_after.index.tz_localize(None)
                df_after = df_after.sort_index()
                
                t_plus_1_bar = df_after.iloc[0]
                entry_date = df_after.index[0].date().strftime("%Y-%m-%d")
                entry_price = round(float(t_plus_1_bar['open']), 2)
                print(f" -> Found T+1 Open: Entry on {entry_date} at ${entry_price:.2f}")
            else:
                print(f" -> Warning: No T+1 to T+7 bars returned from Alpaca for {ticker} starting from {actual_signal_date + timedelta(days=1)}")
        except Exception as e:
            print(f"Could not resolve T+1 entry parameters: {e}")
            
        # Check if the signal is live (within last 4 days to handle weekends/holidays)
        is_live = False
        try:
            sig_date_obj = datetime.strptime(signal_date, "%Y-%m-%d").date()
            is_live = sig_date_obj >= (datetime.now().date() - timedelta(days=4))
        except Exception:
            pass

        # Calculate consensus and regime
        try:
            buy_ratio, hold_ratio, sell_ratio, div_score = get_consensus_divergence_as_of(ticker, signal_date)
        except Exception as e:
            print(f"Error getting consensus divergence for {ticker}: {e}")
            buy_ratio, hold_ratio, sell_ratio, div_score = 0.5, 0.4, 0.1, 0.5
            
        try:
            regime = get_spy_regime_as_of(signal_date, alpaca_client)
        except Exception as e:
            print(f"Error getting SPY regime for {ticker}: {e}")
            regime = "Unknown"

        # De-duplication check:
        # Check if we already own it or if we already added it to our buy list today.
        conn_check = get_connection()
        cursor_check = conn_check.cursor()
        cursor_check.execute("SELECT COUNT(*) FROM trades WHERE ticker = ? AND exit_date IS NULL", (ticker,))
        already_owned = cursor_check.fetchone()[0] > 0
        conn_check.close()
        
        is_duplicate = (ticker in tickers_passed_today) or already_owned
        trade_status = 'skipped_duplicate' if is_duplicate else ('pending_buy' if is_live else 'none')

        # Write updates and insert trade in a brief write transaction
        conn_write = get_connection()
        cursor_write = conn_write.cursor()
        
        # Update daily signals status
        cursor_write.execute("""
            UPDATE daily_signals 
            SET filter_status = 'confirmed_buy', 
                volume_spike_multiple = ?,
                consensus_buy_ratio = ?,
                consensus_hold_ratio = ?,
                consensus_sell_ratio = ?,
                consensus_divergence_score = ?
            WHERE id = ?
        """, (spike_multiple, buy_ratio, hold_ratio, sell_ratio, div_score, sig_id))
        
        # Insert trade record copying earnings proximity details
        cursor_write.execute("""
            INSERT INTO trades (
                signal_date, entry_date, ticker, analyst_name, firm,
                analyst_winrate_at_signal, analyst_n_at_signal, target_price,
                entry_price, volume_spike_multiple,
                is_earnings_proximate, earnings_date, eps_actual, eps_estimate,
                evaluation_window_days,
                consensus_buy_ratio, consensus_hold_ratio, consensus_sell_ratio, consensus_divergence_score,
                market_regime, paper_trade_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 63, ?, ?, ?, ?, ?, ?)
        """, (
            signal_date, entry_date, ticker, analyst, firm,
            win_rate, n_ratings, target_price, entry_price, spike_multiple,
            is_earnings_prox, earnings_date, eps_actual, eps_estimate,
            buy_ratio, hold_ratio, sell_ratio, div_score,
            regime, trade_status
        ))
        inserted_trade_id = cursor_write.lastrowid
        conn_write.commit()
        conn_write.close()

        if is_live and not is_duplicate:
            live_passing_trades.append({"trade_id": inserted_trade_id, "ticker": ticker, "target_price": target_price})
            tickers_passed_today.add(ticker)
        elif is_live and is_duplicate:
            print(f" -> Skipped execution: {ticker} is a duplicate or already owned.")
            
    # Execute live paper trades if any
    if live_passing_trades:
        print(f"\nEvaluating Alpaca Paper Trading execution for {len(live_passing_trades)} live signals...")
        try:
            alpaca_key = os.getenv("ALPACA_API_KEY")
            alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
            if not alpaca_key or not alpaca_secret:
                print("Alpaca credentials missing. Skipping paper order placement.")
            else:
                trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
                account = trading_client.get_account()
                available_cash = float(account.cash)
                print(f"Available Paper Cash: ${available_cash:.2f}")
                
                if available_cash <= 0:
                    print("Insufficient paper cash to trade.")
                    conn_update = get_connection()
                    cursor_update = conn_update.cursor()
                    for t in live_passing_trades:
                        cursor_update.execute("UPDATE trades SET paper_trade_status = 'rejected_no_cash' WHERE id = ?", (t["trade_id"],))
                    conn_update.commit()
                    conn_update.close()
                else:
                    MAX_CASH_PER_TRADE = 20000.0  # Cap allocation per trade to $20,000
                    cash_per_trade = min(available_cash / len(live_passing_trades), MAX_CASH_PER_TRADE)
                    print(f"Allocating ${cash_per_trade:.2f} per trade (Cap: ${MAX_CASH_PER_TRADE:.2f}).")
                    
                    conn_update = get_connection()
                    cursor_update = conn_update.cursor()
                    
                    for t in live_passing_trades:
                        ticker = t["ticker"]
                        trade_id = t["trade_id"]
                        
                        # Get latest quote to calculate quantity
                        from alpaca.data.requests import StockLatestQuoteRequest
                        try:
                            res_quote = alpaca_client.get_stock_latest_quote(StockLatestQuoteRequest(
                                symbol_or_symbols=ticker,
                                feed=DataFeed.IEX
                            ))
                            if ticker in res_quote:
                                q = res_quote[ticker]
                                current_price = (q.ask_price + q.bid_price) / 2 if (q.ask_price > 0 and q.bid_price > 0) else (q.ask_price if q.ask_price > 0 else q.bid_price)
                                if current_price <= 0:
                                    current_price = 100.0
                            else:
                                current_price = 100.0
                        except Exception:
                            current_price = 100.0
                            
                        # Fetch recent daily bars (last 30 calendar days) to calculate 15-day SMA
                        sma_len = 15
                        today = datetime.now().date()
                        start_date = today - timedelta(days=30)
                        start_dt = datetime.combine(start_date, datetime.min.time())
                        end_dt = datetime.combine(today, datetime.max.time())
                        
                        try:
                            bars_sma = alpaca_client.get_stock_bars(StockBarsRequest(
                                symbol_or_symbols=ticker,
                                timeframe=TimeFrame.Day,
                                start=start_dt,
                                end=end_dt,
                                feed=DataFeed.IEX,
                                adjustment='split'
                            ))
                            df_sma = bars_sma.df
                            if df_sma is not None and not df_sma.empty:
                                df_sma = df_sma.copy()
                                if isinstance(df_sma.index, pd.MultiIndex):
                                    df_sma = df_sma.xs(ticker, level='symbol')
                                df_sma.index = df_sma.index.tz_localize(None)
                                df_sma = df_sma.sort_index()
                                
                                if len(df_sma) >= sma_len:
                                    sma_val = float(df_sma['close'].tail(sma_len).mean())
                                    print(f"  [{ticker}] Entry Price: ${current_price:.2f}, 15-day SMA: ${sma_val:.2f}")
                                    if current_price < sma_val:
                                        print(f"  -> Skipped order: {ticker} current price ${current_price:.2f} is below 15-day SMA ${sma_val:.2f} (Trend filter check failed).")
                                        cursor_update.execute("UPDATE trades SET paper_trade_status = 'skipped_below_sma' WHERE id = ?", (trade_id,))
                                        continue
                                else:
                                    print(f"  Warning: Not enough history to calculate SMA for {ticker} (only {len(df_sma)} bars). Proceeding.")
                            else:
                                print(f"  Warning: Could not fetch SMA bars for {ticker}. Proceeding anyway.")
                        except Exception as sma_err:
                            print(f"  Warning: SMA calculation error for {ticker}: {sma_err}. Proceeding anyway.")
                            
                        qty = int(cash_per_trade / current_price)
                        if qty > 0:
                            print(f"Submitting BUY market order: {qty} shares of {ticker} at approx ${current_price:.2f}...")
                            try:
                                from alpaca.trading.requests import MarketOrderRequest
                                from alpaca.trading.enums import OrderSide, TimeInForce
                                order = trading_client.submit_order(MarketOrderRequest(
                                    symbol=ticker,
                                    qty=qty,
                                    side=OrderSide.BUY,
                                    time_in_force=TimeInForce.DAY
                                ))
                                print(f"Order submitted successfully! Order ID: {order.id}")
                                
                                # Update trade as open and save quantity/highest price
                                today_str = datetime.now().strftime("%Y-%m-%d")
                                cursor_update.execute("""
                                    UPDATE trades
                                    SET paper_trade_status = 'open',
                                        alpaca_qty = ?,
                                        entry_price = ?,
                                        entry_date = ?,
                                        highest_price_recorded = ?
                                    WHERE id = ?
                                """, (qty, round(current_price, 2), today_str, round(current_price, 2), trade_id))
                            except Exception as order_ex:
                                print(f"Failed to submit order for {ticker}: {order_ex}")
                                cursor_update.execute("UPDATE trades SET paper_trade_status = 'failed_order' WHERE id = ?", (trade_id,))
                        else:
                            print(f"Quantity is 0 for {ticker} (cash_per_trade=${cash_per_trade:.2f}, price=${current_price:.2f}). Skipping.")
                            cursor_update.execute("UPDATE trades SET paper_trade_status = 'rejected_insufficient_cash' WHERE id = ?", (trade_id,))
                            
                    conn_update.commit()
                    conn_update.close()
        except Exception as api_ex:
            print(f"Error during paper trade execution: {api_ex}")

    print("Filter engine run completed successfully.")
    
    # Dynamically correct any stock split issues
    try:
        from scripts.fix_splits import fix_splits
        fix_splits()
    except Exception as e:
        print(f"Failed to run split correction: {e}")

if __name__ == "__main__":
    run_filter()
