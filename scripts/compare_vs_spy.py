"""
Compares each running strategy's real paper-account performance against
SPY's return since that strategy's actual first trade -- not an arbitrary
lookback window -- pulled from Alpaca's own portfolio history for the
account side, and each strategy's own DB for "when did it actually start."

Run this from wherever .env / .env.value / .env.quality actually live
(the VM, since all three real accounts are configured there) -- any
profile whose .env file is missing, whose account has no trades yet, or
whose credentials are rejected, is skipped rather than erroring the whole
run.

Usage:
    python scripts/compare_vs_spy.py
"""
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

# (label, env file, table holding this strategy's trades, column with the entry date)
STRATEGIES = [
    ("Momentum (analyst upgrades)", ".env", "trades", "entry_date"),
    ("Value (VLUE-style)", ".env.value", "factor_holdings", "entry_date"),
    ("Quality (QUAL-style)", ".env.quality", "factor_holdings", "entry_date"),
]


def get_first_trade_date(db_path, table, date_col):
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT MIN({date_col}) FROM {table} WHERE {date_col} IS NOT NULL")
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def get_spy_return_pct(data_client, start_dt, end_dt):
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Day,
        start=start_dt,
        end=end_dt,
        feed=DataFeed.IEX,
        adjustment=Adjustment.ALL,
    )
    bars = data_client.get_stock_bars(req).df
    if bars is None or bars.empty:
        return None
    closes = bars["close"]
    if len(closes) < 2:
        return None
    return (float(closes.iloc[-1]) / float(closes.iloc[0]) - 1) * 100


def analyze_strategy(label, env_file, table, date_col):
    if not os.path.exists(env_file):
        print(f"Skipping {label}: {env_file} not found in this directory.")
        return None

    load_dotenv(env_file, override=True)
    alpaca_key = os.environ.get("ALPACA_API_KEY")
    alpaca_secret = os.environ.get("ALPACA_SECRET_KEY")
    if not alpaca_key or not alpaca_secret:
        print(f"Skipping {label}: missing Alpaca credentials in {env_file}.")
        return None

    db_path = os.environ.get("SCREENER_DB_PATH", os.path.join("data", "screener.db"))
    first_trade_date_str = get_first_trade_date(db_path, table, date_col)
    if not first_trade_date_str:
        print(f"Skipping {label}: no trades found yet in {db_path}.")
        return None

    start_dt = datetime.strptime(first_trade_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
    data_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)

    try:
        history = trading_client.get_portfolio_history(
            GetPortfolioHistoryRequest(start=start_dt, timeframe="1D")
        )
    except Exception as e:
        print(f"Skipping {label}: could not fetch portfolio history ({e}). Check {env_file}'s Alpaca keys are the real ones, not placeholders.")
        return None

    timestamps = history.timestamp or []
    equity = history.equity or []
    # Alpaca sometimes pads a leading 0 equity point before real data starts;
    # treat non-positive equity as missing rather than a real starting balance.
    paired = [(t, e) for t, e in zip(timestamps, equity) if e is not None and e > 0]

    if len(paired) < 2:
        print(f"Skipping {label}: only {len(paired)} usable portfolio-history data point(s) since {first_trade_date_str} -- too early to compare.")
        return None

    start_ts, start_equity = paired[0]
    end_ts, end_equity = paired[-1]
    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_date = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    strategy_return_pct = (end_equity / start_equity - 1) * 100
    spy_return_pct = get_spy_return_pct(data_client, start_date, end_date)
    excess_return_pct = (
        strategy_return_pct - spy_return_pct if spy_return_pct is not None else None
    )

    return {
        "strategy": label,
        "start_date": start_date.date(),
        "end_date": end_date.date(),
        "end_equity": end_equity,
        "strategy_return_pct": strategy_return_pct,
        "spy_return_pct": spy_return_pct,
        "excess_return_pct": excess_return_pct,
    }


def print_results(results):
    if not results:
        print("No strategies had enough data to compare yet.")
        return

    header = f"{'Strategy':<30}{'Since first trade':<24}{'Equity':<15}{'Return':<10}{'SPY':<10}{'Excess':<10}"
    print(f"\n{header}")
    print("-" * len(header))
    for r in results:
        window = f"{r['start_date']} to {r['end_date']}"
        ret_str = f"{r['strategy_return_pct']:+.2f}%"
        spy_str = f"{r['spy_return_pct']:+.2f}%" if r["spy_return_pct"] is not None else "n/a"
        excess_str = f"{r['excess_return_pct']:+.2f}%" if r["excess_return_pct"] is not None else "n/a"
        equity_str = f"${r['end_equity']:,.2f}"
        print(f"{r['strategy']:<30}{window:<24}{equity_str:<15}{ret_str:<10}{spy_str:<10}{excess_str:<10}")


def main():
    results = []
    for label, env_file, table, date_col in STRATEGIES:
        result = analyze_strategy(label, env_file, table, date_col)
        if result:
            results.append(result)
    print_results(results)


if __name__ == "__main__":
    main()
