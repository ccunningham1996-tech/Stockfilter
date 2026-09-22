"""
Compares each running strategy's real paper-account performance (return and
max drawdown) against SPY and QQQ since that strategy's actual first trade --
not an arbitrary lookback window -- pulled from Alpaca's own portfolio history
for the account side, and each strategy's own DB for "when did it actually
start."

Run this from wherever the strategies' .env profile files actually live
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
    ("NDX Momentum Buffered", ".env.ndx_mom_buffer", "ndx_orders", "trade_date"),
]
BENCHMARKS = ["SPY", "QQQ"]


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


def get_benchmark_return_pct(data_client, symbol, start_dt, end_dt):
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
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
    bench = {b: get_benchmark_return_pct(data_client, b, start_date, end_date) for b in BENCHMARKS}
    peak, max_dd = paired[0][1], 0.0
    for _, e in paired:
        peak = max(peak, e)
        max_dd = min(max_dd, e / peak - 1)

    return {
        "strategy": label,
        "start_date": start_date.date(),
        "end_date": end_date.date(),
        "end_equity": end_equity,
        "strategy_return_pct": strategy_return_pct,
        "max_drawdown_pct": max_dd * 100,
        "benchmarks": bench,
    }


def print_results(results):
    if not results:
        print("No strategies had enough data to compare yet.")
        return

    pct = lambda v: f"{v:+.2f}%" if v is not None else "n/a"
    header = (f"{'Strategy':<30}{'Since first trade':<26}{'Equity':<15}{'Return':<10}{'MaxDD':<10}"
              + "".join(f"{b:<10}{'vs ' + b:<10}" for b in BENCHMARKS))
    print(f"\n{header}")
    print("-" * len(header))
    for r in results:
        window = f"{r['start_date']} to {r['end_date']}"
        line = (f"{r['strategy']:<30}{window:<26}{'$' + format(r['end_equity'], ',.2f'):<15}"
                f"{pct(r['strategy_return_pct']):<10}{pct(r['max_drawdown_pct']):<10}")
        for b in BENCHMARKS:
            b_ret = r["benchmarks"][b]
            excess = r["strategy_return_pct"] - b_ret if b_ret is not None else None
            line += f"{pct(b_ret):<10}{pct(excess):<10}"
        print(line)
    print("\nFor NDX Momentum Buffered, QQQ is the fair benchmark; run "
          "`python -m scripts.run_ndx_mom_buffer report` for its monthly returns and sector mix.")


def main():
    results = []
    for label, env_file, table, date_col in STRATEGIES:
        result = analyze_strategy(label, env_file, table, date_col)
        if result:
            results.append(result)
    print_results(results)


if __name__ == "__main__":
    main()
