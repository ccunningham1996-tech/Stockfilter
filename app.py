import os
import sqlite3
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Alpaca imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient

# Plotly imports
import plotly.express as px

# Local imports
from src.db import init_db, get_connection
from src.scraper import run_scraper
from src.filter import run_filter
from src.outcome_tracker import run_outcome_tracker

load_dotenv()

st.set_page_config(page_title="Institutional Momentum Screener", layout="wide")

# Custom Dark Mode styling
st.markdown("""
    <style>
    .main { background-color: #0d1117; color: #c9d1d9; }
    .metric-card {
        background: rgba(255, 255, 255, 0.02);
        border-radius: 8px;
        padding: 15px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        text-align: center;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .metric-card h4 { margin: 0; color: #8b949e; font-size: 14px; }
    .metric-card h2 { margin: 8px 0 4px 0; color: #58a6ff; font-size: 28px; }
    .metric-card p { margin: 0; color: #c9d1d9; font-size: 12px; }
    </style>
""", unsafe_allow_html=True)

# Initialize database on load if needed
db_path = os.path.join("data", "screener.db")
if not os.path.exists(db_path):
    init_db()

# Helper to read last scrape timestamp
def get_last_scrape_timestamp():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT scrape_timestamp FROM daily_signals ORDER BY scrape_timestamp DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            return row['scrape_timestamp']
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return "N/A"

# ----------------- SIDEBAR CONTROLS -----------------
with st.sidebar:
    st.title("⚡ Screener Controls")
    
    # Run Scraper Button
    if st.button("Run Scraper Now", use_container_width=True):
        with st.spinner("Scraping MarketBeat & Fetching Alpaca Quotes..."):
            try:
                run_scraper()
                st.success("Scraper run completed successfully.")
            except Exception as e:
                st.error(f"Scraper error: {e}")
                
    # Run Filter Engine Button
    if st.button("Run Filter Engine", use_container_width=True):
        with st.spinner("Evaluating pending signals against volume & win-rate..."):
            try:
                run_filter()
                st.success("Filter engine run completed.")
            except Exception as e:
                st.error(f"Filter error: {e}")
                
    # Evaluate Outcomes Button
    if st.button("Evaluate Outcomes", use_container_width=True):
        with st.spinner("Benchmarking outcomes against SPY (63-day)..."):
            try:
                run_outcome_tracker()
                st.success("Outcome tracker evaluation finished.")
            except Exception as e:
                st.error(f"Outcome error: {e}")
                
    st.markdown("---")
    
    # Display last scrape timestamp
    last_scrape = get_last_scrape_timestamp()
    st.markdown(f"**Last Scrape Timestamp:**\n`{last_scrape}`")

# ----------------- MAIN METRICS HEADER -----------------
st.title("⚡ Top Analyst Momentum Screener")
st.subheader("MarketBeat Scraping & Alpaca IEX Quantitative Execution Portal")
st.markdown("---")

conn = get_connection()
cursor = conn.cursor()

# Get metrics for cards
try:
    total_signals = cursor.execute("SELECT COUNT(*) FROM daily_signals").fetchone()[0]
    active_picks = cursor.execute("SELECT COUNT(*) FROM trades WHERE exit_date IS NULL").fetchone()[0]
    graded_analysts = cursor.execute("SELECT COUNT(DISTINCT(analyst_name || firm)) FROM analyst_history").fetchone()[0]
except sqlite3.Error:
    total_signals, active_picks, graded_analysts = 0, 0, 0

m1, m2, m3 = st.columns(3)
with m1:
    st.markdown(f"<div class='metric-card'><h4>Upgrades Tracked</h4><h2>{total_signals}</h2><p>Total historical signals scraped</p></div>", unsafe_allow_html=True)
with m2:
    st.markdown(f"<div class='metric-card'><h4>Graded Analysts & Firms</h4><h2>{graded_analysts}</h2><p>Distinct tracking records</p></div>", unsafe_allow_html=True)
with m3:
    st.markdown(f"<div class='metric-card'><h4>Active Open Trades</h4><h2>{active_picks}</h2><p>Passed volume band & win-rate criteria</p></div>", unsafe_allow_html=True)

st.markdown("---")

# Helper to format open trades tables (leaving numeric columns as floats/ints for sorting)
def format_open_trades(df, current_prices):
    if df.empty:
        return df
        
    df = df.copy()
    
    # Map current prices
    df['Current Price'] = df['ticker'].map(current_prices)
    # If any ticker doesn't have a current price, fall back to entry price
    df['Current Price'] = df['Current Price'].fillna(df['entry_price'])
    df['Current Price'] = df['Current Price'].round(2)
    
    # Recalculate implied upside dynamically using current price
    df['Implied Upside'] = ((df['target_price'] - df['Current Price']) / df['Current Price']) * 100
    df['Implied Upside'] = df['Implied Upside'].round(2)
    
    df_picks = df.sort_values(by="Implied Upside", ascending=False)
    
    df_display = df_picks.rename(columns={
        "ticker": "Ticker",
        "firm": "Firm",
        "analyst_name": "Analyst",
        "analyst_winrate_at_signal": "Win-Rate",
        "analyst_n_at_signal": "Sample (n)",
        "volume_spike_multiple": "Volume Spike",
        "target_price": "Target Price",
        "entry_price": "Entry Price",
        "entry_date": "Entry Date",
        "market_regime": "Regime",
        "consensus_divergence_score": "Div Score",
        "alpaca_qty": "Qty",
        "highest_price_recorded": "Peak Price"
    })
    
    # Convert Win-Rate to percentage (0.65 -> 65.0)
    df_display['Win-Rate'] = df_display['Win-Rate'] * 100
    df_display['Win-Rate'] = df_display['Win-Rate'].round(1)
    if 'Div Score' in df_display.columns:
        df_display['Div Score'] = df_display['Div Score'].round(2)
    
    display_cols = [
        "Ticker", "Firm", "Analyst", "Win-Rate", "Sample (n)", "Volume Spike", 
        "Regime", "Div Score", "Target Price", "Entry Price", "Current Price", "Implied Upside", "Entry Date"
    ]
    
    # Conditionally add Paper Qty and Peak Price if there are any paper trades
    if (df['paper_trade_status'] == 'open').any():
        df_display['Qty'] = df_display['Qty'].fillna(0).astype(int)
        df_display['Peak Price'] = df_display['Peak Price'].fillna(df_display['Entry Price']).round(2)
        display_cols.insert(display_cols.index("Entry Price"), "Qty")
        display_cols.insert(display_cols.index("Current Price"), "Peak Price")
        
    if "earnings_date" in df.columns:
        df_display["Earnings Date"] = df["earnings_date"]
        display_cols.append("Earnings Date")
        
    df_display = df_display[display_cols]
    return df_display

# Color code function for Volume Spike
def color_volume_spike_band(val):
    try:
        if isinstance(val, str):
            val_float = float(val.replace('x', ''))
        else:
            val_float = float(val)
            
        # Sweet spot: 2.0x to 3.5x volume spike (Institutional move)
        if 2.0 <= val_float <= 3.5:
            return 'background-color: #238636; color: white;'  # Green
        # Marginal bounds: 1.5x to 2.0x or 3.5x to 4.0x
        elif 1.5 <= val_float <= 4.0:
            return 'background-color: #d29922; color: black;'  # Yellow
    except (ValueError, TypeError):
        pass
    return ''

# Column configurations for Tab 1
tab1_config = {
    "Ticker": st.column_config.TextColumn("Ticker"),
    "Firm": st.column_config.TextColumn("Firm"),
    "Analyst": st.column_config.TextColumn("Analyst"),
    "Win-Rate": st.column_config.NumberColumn("Win-Rate", format="%.1f%%"),
    "Sample (n)": st.column_config.NumberColumn("Sample (n)", format="%d"),
    "Volume Spike": st.column_config.NumberColumn("Volume Spike", format="%.2fx"),
    "Regime": st.column_config.TextColumn("Regime"),
    "Div Score": st.column_config.NumberColumn("Div Score", format="%.2f"),
    "Target Price": st.column_config.NumberColumn("Target Price", format="$%.2f"),
    "Entry Price": st.column_config.NumberColumn("Entry Price", format="$%.2f"),
    "Current Price": st.column_config.NumberColumn("Current Price", format="$%.2f"),
    "Qty": st.column_config.NumberColumn("Qty", format="%d"),
    "Peak Price": st.column_config.NumberColumn("Peak Price", format="$%.2f"),
    "Implied Upside": st.column_config.NumberColumn("Implied Upside", format="%+.2f%%"),
    "Entry Date": st.column_config.TextColumn("Entry Date"),
    "Earnings Date": st.column_config.TextColumn("Earnings Date")
}

# Try fetching Alpaca Paper Trading metrics
paper_portfolio_val = None
paper_cash = None
try:
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    if alpaca_key and alpaca_secret:
        tc = TradingClient(alpaca_key, alpaca_secret, paper=True)
        acc = tc.get_account()
        paper_portfolio_val = float(acc.portfolio_value)
        paper_cash = float(acc.cash)
except Exception:
    pass

# ----------------- TABS IMPLEMENTATION -----------------
tab1, tab2, tab3, tab4 = st.tabs([
    "🎯 Alpha Picks", 
    "📋 Pipeline Log", 
    "📊 Analyst Tracker", 
    "📈 Trade History"
])

# ----- TAB 1: ALPHA PICKS -----
with tab1:
    st.markdown("### Verified Open Institutional Trades")
    
    if paper_portfolio_val is not None:
        st.markdown(f"💼 **Alpaca Paper Portfolio Value**: `${paper_portfolio_val:,.2f}` | 💵 **Available Cash**: `${paper_cash:,.2f}`")
        
    st.markdown("Positions currently open (no exit date recorded), separated by their earnings proximity beat status.")
    
    try:
        query_open = """
            SELECT ticker, firm, analyst_name, analyst_winrate_at_signal, 
                   analyst_n_at_signal, volume_spike_multiple, target_price, 
                   entry_price, entry_date, is_earnings_proximate, earnings_date,
                   market_regime, consensus_divergence_score, alpaca_qty, highest_price_recorded, paper_trade_status
            FROM trades
            WHERE exit_date IS NULL
        """
        df_open_all = pd.read_sql_query(query_open, conn)
    except Exception as e:
        df_open_all = pd.DataFrame()
        st.error(f"Error loading open trades: {e}")
        
    if not df_open_all.empty:
        # Fetch current stock quotes in bulk from Alpaca
        current_prices = {}
        try:
            alpaca_key = os.getenv("ALPACA_API_KEY")
            alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
            if alpaca_key and alpaca_secret:
                alpaca_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
                unique_tickers = list(df_open_all['ticker'].unique())
                res = alpaca_client.get_stock_latest_quote(StockLatestQuoteRequest(
                    symbol_or_symbols=unique_tickers,
                    feed=DataFeed.IEX
                ))
                for ticker in unique_tickers:
                    if ticker in res:
                        q = res[ticker]
                        price = None
                        if q.ask_price > 0 and q.bid_price > 0:
                            price = (q.ask_price + q.bid_price) / 2
                        elif q.ask_price > 0:
                            price = q.ask_price
                        elif q.bid_price > 0:
                            price = q.bid_price
                        if price:
                            current_prices[ticker] = price
        except Exception as ex:
            st.warning(f"Could not fetch real-time quotes: {ex}")

        df_prox = df_open_all[df_open_all['is_earnings_proximate'] == 1]
        df_stand = df_open_all[df_open_all['is_earnings_proximate'] == 0]
        
        # Section A: Earnings Proximate Upgrades
        st.markdown("#### 💎 Category A: Earnings-Proximate Upgrades")
        st.caption("Upgrades issued within 5 trading days of an earnings announcement where the company beat or matched estimates.")
        if not df_prox.empty:
            df_prox_disp = format_open_trades(df_prox, current_prices)
            if hasattr(df_prox_disp.style, 'map'):
                styled_prox = df_prox_disp.style.map(color_volume_spike_band, subset=['Volume Spike'])
            else:
                styled_prox = df_prox_disp.style.applymap(color_volume_spike_band, subset=['Volume Spike'])
            st.dataframe(styled_prox, use_container_width=True, hide_index=True, column_config=tab1_config)
        else:
            st.info("No active open earnings-proximate upgrades.")
            
        st.markdown("---")
        
        # Section B: Standalone Upgrades
        st.markdown("#### 📢 Category B: Standalone Upgrades")
        st.caption("Routine upgrades issued outside the earnings announcement proximity window.")
        if not df_stand.empty:
            df_stand_disp = format_open_trades(df_stand, current_prices)
            if hasattr(df_stand_disp.style, 'map'):
                styled_stand = df_stand_disp.style.map(color_volume_spike_band, subset=['Volume Spike'])
            else:
                styled_stand = df_stand_disp.style.applymap(color_volume_spike_band, subset=['Volume Spike'])
            st.dataframe(styled_stand, use_container_width=True, hide_index=True, column_config=tab1_config)
        else:
            st.info("No active open standalone upgrades.")
    else:
        st.info("No active open trades currently satisfy the quantitative filters.")

# ----- TAB 2: PIPELINE LOG -----
with tab2:
    st.markdown("### 7-Day Pipeline Status Logs")
    st.markdown("Review signal entries from the past 7 days and see the filter results, highlighting earnings proximity.")
    
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    try:
        query_log = """
            SELECT signal_date, ticker, firm, filter_status, volume_spike_multiple, target_price, is_earnings_proximate
            FROM daily_signals
            WHERE signal_date >= ?
            ORDER BY signal_date DESC, ticker ASC
        """
        df_log = pd.read_sql_query(query_log, conn, params=(seven_days_ago,))
    except Exception as e:
        df_log = pd.DataFrame()
        st.error(f"Error loading logs: {e}")
        
    if not df_log.empty:
        for idx, row in df_log.iterrows():
            status = row['filter_status']
            ticker = row['ticker']
            firm = row['firm'] or "Unknown Firm"
            date = row['signal_date']
            vol_spike = row['volume_spike_multiple']
            is_prox = row['is_earnings_proximate']
            
            badge = " [💎 Earnings-Proximate Beat]" if is_prox == 1 else ""
            
            if status == 'confirmed_buy':
                st.markdown(f"✅ **{ticker}** ({date}){badge} — `confirmed_buy` (volume spike {vol_spike:.2f}x, win-rate passed/bypassed)")
            elif status == 'rejected_volume':
                reason = "volume spike was outside the 1.5x–4.0x band or close was negative"
                if vol_spike is not None:
                    if vol_spike < 1.5:
                        reason = f"volume spike was {vol_spike:.2f}x (below 1.5x floor)"
                    elif vol_spike > 4.0:
                        reason = f"volume spike was {vol_spike:.2f}x (above 4.0x cap)"
                st.markdown(f"❌ **{ticker}** ({date}){badge} — `rejected_volume` ({reason})")
            elif status == 'rejected_winrate':
                st.markdown(f"❌ **{ticker}** ({date}){badge} — `rejected_winrate` (analyst historical win rate below 60% with n>=10)")
            elif status == 'rejected_no_close_data':
                st.markdown(f"❌ **{ticker}** ({date}){badge} — `rejected_no_close_data` (insufficient Alpaca historical bar data)")
            elif status == 'pending':
                st.markdown(f"⏳ **{ticker}** ({date}){badge} — `pending` (awaiting T+1 session close)")
            else:
                st.markdown(f"❓ **{ticker}** ({date}){badge} — `{status}`")
    else:
        st.info("No signal records logged in the past 7 days.")

# ----- TAB 3: ANALYST TRACKER -----
with tab3:
    st.markdown("### Analyst Performance Statistics")
    
    try:
        query_tracker = """
            SELECT analyst_name as "Analyst Name",
                   firm as "Firm",
                   COUNT(*) as "Total Ratings",
                   SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) as "Won",
                   SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END) as "Lost",
                   SUM(CASE WHEN status='Pending' THEN 1 ELSE 0 END) as "Pending",
                   SUM(CASE WHEN status='Withdrawn' THEN 1 ELSE 0 END) as "Withdrawn",
                   AVG(stock_return_pct) as "Avg Stock Return",
                   AVG(stock_return_pct - spy_return_pct) as "Avg Excess Return"
            FROM analyst_history
            GROUP BY analyst_name, firm
            HAVING (SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) + SUM(CASE WHEN status='Lost' THEN 1 ELSE 0 END)) >= 1
        """
        df_tracker = pd.read_sql_query(query_tracker, conn)
    except Exception as e:
        df_tracker = pd.DataFrame()
        st.error(f"Error loading analyst stats: {e}")
        
    if not df_tracker.empty:
        # Calculate win-rate as raw numeric percentage (0-100)
        df_tracker['Win-Rate %'] = (df_tracker['Won'] / (df_tracker['Won'] + df_tracker['Lost'])) * 100
        df_tracker['Win-Rate %'] = df_tracker['Win-Rate %'].round(1)
        
        df_tracker = df_tracker[[
            "Analyst Name", "Firm", "Total Ratings", "Won", "Lost", 
            "Pending", "Withdrawn", "Win-Rate %", "Avg Stock Return", "Avg Excess Return"
        ]]
        
        tab3_config = {
            "Analyst Name": st.column_config.TextColumn("Analyst Name"),
            "Firm": st.column_config.TextColumn("Firm"),
            "Total Ratings": st.column_config.NumberColumn("Total Ratings", format="%d"),
            "Won": st.column_config.NumberColumn("Won", format="%d"),
            "Lost": st.column_config.NumberColumn("Lost", format="%d"),
            "Pending": st.column_config.NumberColumn("Pending", format="%d"),
            "Withdrawn": st.column_config.NumberColumn("Withdrawn", format="%d"),
            "Win-Rate %": st.column_config.NumberColumn("Win-Rate %", format="%.1f%%"),
            "Avg Stock Return": st.column_config.NumberColumn("Avg Stock Return", format="%+.2f%%"),
            "Avg Excess Return": st.column_config.NumberColumn("Avg Excess Return", format="%+.2f%%")
        }
        st.dataframe(df_tracker, use_container_width=True, hide_index=True, column_config=tab3_config)
    else:
        st.info("No analysts currently have closed historical ratings (requires Won + Lost >= 1).")

# ----- TAB 4: TRADE HISTORY -----
with tab4:
    st.markdown("### Closed Trades Performance Ledger")
    
    try:
        # Load closed trades (where exit_date IS NOT NULL)
        query_closed = """
            SELECT * FROM trades
            WHERE exit_date IS NOT NULL
            ORDER BY exit_date DESC
        """
        df_closed = pd.read_sql_query(query_closed, conn)
    except Exception as e:
        df_closed = pd.DataFrame()
        st.error(f"Error loading closed trades: {e}")
        
    if not df_closed.empty:
        # Let's separate closed trades by category to show the comparative metrics!
        df_prox_c = df_closed[df_closed['is_earnings_proximate'] == 1]
        df_stand_c = df_closed[df_closed['is_earnings_proximate'] == 0]
        
        # Calculate summary statistics for both categories
        def get_stats_for_subset(df_subset):
            if df_subset.empty:
                return {"count": 0, "winrate": 0.0, "avg_return": 0.0, "avg_excess": 0.0, "best": 0.0}
            count = len(df_subset)
            wins = df_subset[df_subset['stock_return_pct'] > 0]
            winrate = (len(wins) / count) * 100
            avg_return = df_subset['stock_return_pct'].mean()
            avg_excess = df_subset['excess_return_pct'].mean()
            best = df_subset['excess_return_pct'].max()
            return {"count": count, "winrate": winrate, "avg_return": avg_return, "avg_excess": avg_excess, "best": best}
            
        stats_prox = get_stats_for_subset(df_prox_c)
        stats_stand = get_stats_for_subset(df_stand_c)
        
        # Display overall metrics cards for closed trades
        total_closed = len(df_closed)
        overall_avg_return = df_closed['stock_return_pct'].mean()
        overall_avg_excess = df_closed['excess_return_pct'].mean()
        
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(f"<div class='metric-card'><h4>Closed Trades</h4><h2>{total_closed}</h2><p>Total holding periods expired</p></div>", unsafe_allow_html=True)
        with c2:
            st.markdown(f"<div class='metric-card'><h4>Avg Stock Return</h4><h2>{overall_avg_return:+.2f}%</h2><p>Average return of closed positions</p></div>", unsafe_allow_html=True)
        with c3:
            st.markdown(f"<div class='metric-card'><h4>Avg Excess Return</h4><h2>{overall_avg_excess:+.2f}%</h2><p>Average outperformance vs SPY</p></div>", unsafe_allow_html=True)
            
        st.markdown("---")
        
        # 2. Render Plotly Scatter Chart
        st.markdown("#### 📊 Volume Spike vs. Excess Return Chart")
        st.caption("Hover over dots to inspect detail. Green is Win (stock return > 0 & outperforming SPY), Red is Loss.")
        try:
            # Create outcome column for coloring
            df_plot = df_closed.copy()
            df_plot['Outcome'] = df_plot.apply(
                lambda r: 'Win' if (r['stock_return_pct'] > 0 and r['excess_return_pct'] > 0) else 'Loss',
                axis=1
            )
            fig = px.scatter(
                df_plot,
                x="volume_spike_multiple",
                y="excess_return_pct",
                color="Outcome",
                color_discrete_map={"Win": "#238636", "Loss": "#da3637"},
                hover_data=["ticker", "entry_date", "exit_date", "stock_return_pct", "excess_return_pct", "firm"],
                labels={
                    "volume_spike_multiple": "Volume Spike Multiple",
                    "excess_return_pct": "Excess Return vs SPY (%)",
                    "Outcome": "Outcome"
                }
            )
            fig.update_layout(
                template="plotly_dark",
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#c9d1d9"),
                margin=dict(l=40, r=40, t=10, b=40)
            )
            st.plotly_chart(fig, use_container_width=True)
        except Exception as plot_ex:
            st.error(f"Could not render scatter plot: {plot_ex}")
            
        st.markdown("---")
        
        # Display side-by-side comparison table
        st.markdown("#### 📈 Proximity Hypothesis Testing Ledger")
        st.markdown("Compare the performance of earnings-proximate trades vs standalone routine trades:")
        
        comparison_data = {
            "Metric": ["Total Closed Trades", "Trade Win-Rate", "Avg Stock Return", "Avg Excess Return vs SPY", "Best Excess Return"],
            "💎 Category A: Earnings-Proximate": [
                f"{stats_prox['count']}",
                f"{stats_prox['winrate']:.1f}%",
                f"{stats_prox['avg_return']:+.2f}%",
                f"{stats_prox['avg_excess']:+.2f}%",
                f"{stats_prox['best']:+.2f}%"
            ],
            "📢 Category B: Standalone": [
                f"{stats_stand['count']}",
                f"{stats_stand['winrate']:.1f}%",
                f"{stats_stand['avg_return']:+.2f}%",
                f"{stats_stand['avg_excess']:+.2f}%",
                f"{stats_stand['best']:+.2f}%"
            ]
        }
        df_comp = pd.DataFrame(comparison_data)
        st.table(df_comp)
        
        st.markdown("---")
        st.markdown("#### 📋 Detailed Trade Performance Log")
        
        # Format the ledger output table
        df_closed_disp = df_closed.copy()
        
        df_closed_disp['Category'] = df_closed_disp['is_earnings_proximate'].apply(
            lambda x: "💎 Earnings-Prox" if x == 1 else "📢 Standalone"
        )
        
        df_ledger = df_closed_disp[[
            "Category", "ticker", "firm", "entry_date", "entry_price", 
            "exit_date", "exit_price", "stock_return_pct", "excess_return_pct", "volume_spike_multiple",
            "market_regime", "consensus_divergence_score"
        ]].rename(columns={
            "ticker": "Ticker",
            "firm": "Firm",
            "entry_date": "Entry Date",
            "entry_price": "Entry Price",
            "exit_date": "Exit Date",
            "exit_price": "Exit Price",
            "stock_return_pct": "Stock Return",
            "excess_return_pct": "Excess Return",
            "volume_spike_multiple": "Volume Spike",
            "market_regime": "Regime",
            "consensus_divergence_score": "Div Score"
        })
        
        tab4_config = {
            "Category": st.column_config.TextColumn("Category"),
            "Ticker": st.column_config.TextColumn("Ticker"),
            "Firm": st.column_config.TextColumn("Firm"),
            "Entry Date": st.column_config.TextColumn("Entry Date"),
            "Entry Price": st.column_config.NumberColumn("Entry Price", format="$%.2f"),
            "Exit Date": st.column_config.TextColumn("Exit Date"),
            "Exit Price": st.column_config.NumberColumn("Exit Price", format="$%.2f"),
            "Stock Return": st.column_config.NumberColumn("Stock Return", format="%+.2f%%"),
            "Excess Return": st.column_config.NumberColumn("Excess Return", format="%+.2f%%"),
            "Volume Spike": st.column_config.NumberColumn("Volume Spike", format="%.2fx"),
            "Regime": st.column_config.TextColumn("Regime"),
            "Div Score": st.column_config.NumberColumn("Div Score", format="%.2f")
        }
        st.dataframe(df_ledger, use_container_width=True, hide_index=True, column_config=tab4_config)
    else:
        st.info("No closed trade records currently exist in the database ledger.")

conn.close()
