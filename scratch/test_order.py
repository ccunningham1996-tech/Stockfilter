import os
import time
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

alpaca_key = os.getenv("ALPACA_API_KEY")
alpaca_secret = os.getenv("ALPACA_SECRET_KEY")

def place_test_order():
    print("Connecting to Alpaca Paper Trading...")
    client = TradingClient(alpaca_key, alpaca_secret, paper=True)
    
    # 1. Check account
    account = client.get_account()
    print(f"Account Buying Power: ${account.buying_power}")
    
    # 2. Place a test buy order for 1 share of AAPL
    symbol = "AAPL"
    print(f"\nPlacing test market BUY order for 1 share of {symbol}...")
    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=1,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        ))
        print(f"SUCCESS: Order submitted! Order ID: {order.id}")
        print(f"Status: {order.status}")
        
        # 3. Wait 3 seconds and check active positions to verify execution
        print("\nWaiting 3 seconds for order execution...")
        time.sleep(3)
        positions = client.get_all_positions()
        print(f"Active positions ({len(positions)}):")
        for p in positions:
            print(f"  {p.symbol}: Qty={p.qty}, AvgEntryPrice=${p.avg_entry_price}, CurrentPrice=${p.current_price}")
            
    except Exception as e:
        print("ERROR placing order:", e)

if __name__ == "__main__":
    place_test_order()
