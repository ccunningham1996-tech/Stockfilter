import os
import time
import requests
import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.enums import DataFeed

# Local imports
from src.db import get_connection
from src.earnings import get_earnings_proximity

load_dotenv()

def parse_marketbeat_upgrades():
    """
    Scrapes the MarketBeat upgrades table, parsing elements using BS4.
    Utilizes custom data-clean attributes where available for maximum reliability.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    url = "https://www.marketbeat.com/ratings/upgrades/"
    print(f"Scraping upgrades from MarketBeat: {url}")
    
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table")
    if not table:
        print("Warning: No table found on MarketBeat upgrades page.")
        return []
        
    rows = table.find_all("tr")
    upgrades = []
    
    for idx, row in enumerate(rows[1:]):  # Skip header row
        cells = row.find_all("td")
        if len(cells) < 7:
            continue
            
        try:
            # 0. Ticker & Company Name
            ticker = ""
            company = ""
            c0_clean = cells[0].get("data-clean")
            if c0_clean and "|" in c0_clean:
                parts = c0_clean.split("|")
                ticker = parts[0].strip().upper()
                company = parts[1].strip()
            else:
                ticker_div = cells[0].find(class_="ticker-area")
                if ticker_div:
                    ticker = ticker_div.text.strip().upper()
                company_div = cells[0].find(class_="title-area")
                if company_div:
                    company = company_div.text.strip()
            
            # 2. Firm (Brokerage)
            firm = ""
            c2_clean = cells[2].get("data-clean")
            if c2_clean and "|" in c2_clean:
                firm = c2_clean.split("|")[0].strip()
            else:
                firm = cells[2].text.strip()
                if "Subscribe to" in firm:
                    firm = firm.split("Subscribe to")[0].strip()
                    
            # 3. Analyst Name
            analyst = ""
            c3_clean = cells[3].get("data-clean")
            if c3_clean and "|" in c3_clean:
                analyst = c3_clean.split("|")[0].strip()
            else:
                analyst = cells[3].text.strip()
            if analyst == "" or not analyst:
                analyst = "N/A"
                
            # 4. Current Price (From MarketBeat)
            marketbeat_price = None
            c4_clean = cells[4].get("data-clean")
            if c4_clean and "|" in c4_clean:
                p_str = c4_clean.split("|")[0].replace("$", "").replace(",", "").strip()
                try:
                    marketbeat_price = float(p_str)
                except ValueError:
                    pass
            
            # 5. Price Target
            target_price = None
            c5_clean = cells[5].get("data-clean")
            if c5_clean and "|" in c5_clean:
                t_str = c5_clean.split("|")[0].replace("$", "").replace(",", "").strip()
                try:
                    t_val = float(t_str)
                    if t_val > 0:
                        target_price = t_val
                except ValueError:
                    pass
            
            # 6. Target Rating (To Grade)
            to_grade = ""
            c6_clean = cells[6].get("data-clean")
            if c6_clean and "|" in c6_clean:
                to_grade = c6_clean.split("|")[1].strip()
            else:
                to_grade = cells[6].text.strip()
                
            # 1. Action Details (From Grade / To Grade if parsed)
            from_grade = None
            action_text = cells[1].text.strip()
            if "upgraded from" in action_text.lower():
                # Extract original rating
                parts = action_text.lower().split("upgraded from")
                if len(parts) > 1 and "to" in parts[1]:
                    subparts = parts[1].split("to")
                    from_grade = subparts[0].strip().title()
            
            if ticker and firm:
                upgrades.append({
                    "symbol": ticker,
                    "company": company,
                    "firm": firm,
                    "analyst": analyst,
                    "from_grade": from_grade,
                    "to_grade": to_grade,
                    "marketbeat_price": marketbeat_price,
                    "target_price": target_price
                })
        except Exception as e:
            print(f"Error parsing row {idx+1}: {e}")
            continue
            
    print(f"Parsed {len(upgrades)} upgrade actions from MarketBeat.")
    return upgrades

def run_scraper():
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if not all([alpaca_key, alpaca_secret]):
        raise ValueError("Missing Alpaca API credentials in .env file. Ensure ALPACA_API_KEY and ALPACA_SECRET_KEY are set.")
        
    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{datetime.now().isoformat()}] Starting scraper for {today_str}...")
    
    # 1. Fetch upgrades from MarketBeat
    upgrades_today = []
    try:
        upgrades_today = parse_marketbeat_upgrades()
    except Exception as e:
        print(f"Error scraping MarketBeat: {e}")
        return
        
    if not upgrades_today:
        print("No upgrades scraped today.")
        return
        
    # 2. Process each upgrade and fetch current price from Alpaca
    alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    conn = get_connection()
    cursor = conn.cursor()
    
    scraped_count = 0
    skipped_count = 0
    
    for idx, item in enumerate(upgrades_today):
        ticker = item["symbol"]
        company = item["company"]
        firm = item["firm"]
        analyst_name = item["analyst"]
        from_grade = item["from_grade"]
        to_grade = item["to_grade"]
        target_price = item["target_price"]
        marketbeat_price = item["marketbeat_price"]
        
        # Rate limit safety sleep (Finnhub and Alpaca rate limits)
        if idx > 0:
            time.sleep(1.1)
            
        current_price = None
        
        # Try fetching real-time price from Alpaca
        try:
            res = alpaca_client.get_stock_latest_quote(StockLatestQuoteRequest(
                symbol_or_symbols=ticker,
                feed=DataFeed.IEX
            ))
            if ticker in res:
                quote = res[ticker]
                if quote.ask_price > 0 and quote.bid_price > 0:
                    current_price = round((quote.ask_price + quote.bid_price) / 2, 2)
                elif quote.ask_price > 0:
                    current_price = round(quote.ask_price, 2)
                elif quote.bid_price > 0:
                    current_price = round(quote.bid_price, 2)
        except Exception as e:
            print(f"Could not fetch Alpaca quote for {ticker}: {e}")
            
        # Fallback to the MarketBeat parsed price if Alpaca quote fails
        if current_price is None and marketbeat_price is not None:
            current_price = marketbeat_price
            print(f" -> Using MarketBeat price fallback for {ticker}: ${current_price:.2f}")
            
        # Ultimate default if everything else fails
        if current_price is None:
            current_price = 100.00
            
        # Compute upside
        upside_percentage = None
        if target_price is not None:
            upside_percentage = round(((target_price - current_price) / current_price) * 100, 2)
            
        # 3. Check Earnings Proximity & surprise beat status
        is_earnings_prox = 0
        earnings_date = None
        eps_actual = None
        eps_estimate = None
        
        try:
            is_earnings_prox, earnings_date, eps_actual, eps_estimate = get_earnings_proximity(
                ticker, today_str, finnhub_key
            )
            if is_earnings_prox:
                print(f" -> FLAG: {ticker} is proximate to earnings beat (Date: {earnings_date}, EPS Actual: {eps_actual}, EPS Est: {eps_estimate})")
        except Exception as e:
            print(f"Could not resolve earnings proximity for {ticker}: {e}")
            
        scrape_timestamp = datetime.now().isoformat()
        
        try:
            # INSERT OR IGNORE (UNIQUE constraint is UNIQUE(signal_date, ticker, firm))
            cursor.execute("""
            INSERT OR IGNORE INTO daily_signals (
                scrape_timestamp, signal_date, ticker, analyst_name, firm, 
                from_grade, to_grade, current_price, target_price, upside_percentage,
                is_earnings_proximate, earnings_date, eps_actual, eps_estimate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                scrape_timestamp, today_str, ticker, analyst_name, firm,
                from_grade, to_grade, current_price, target_price, upside_percentage,
                is_earnings_prox, earnings_date, eps_actual, eps_estimate
            ))
            
            if cursor.rowcount > 0:
                scraped_count += 1
            else:
                skipped_count += 1
        except sqlite3.Error as e:
            print(f"Database error inserting signal for {ticker}: {e}")
            
    conn.commit()
    conn.close()
    
    print(f"Scrape completed for {today_str}. Added {scraped_count} new upgrades, skipped {skipped_count} duplicates.")

if __name__ == "__main__":
    run_scraper()
