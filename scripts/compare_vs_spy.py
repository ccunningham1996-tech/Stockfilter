"""
Compares each running strategy's real paper-account performance against
SPY's return over the same window that strategy has actually been live
for (pulled from Alpaca's own portfolio history, not recomputed locally).

Run this from wherever .env / .env.value / .env.quality actually live
(the VM, since all three real accounts are configured there) -- any
profile whose .env file is missing, or whose account has fewer than two
portfolio-history data points yet, is skipped rather than erroring.

Usage:
    python scripts/compare_vs_spy.py
"""
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetPortfolioHistoryRequest

STRATEGIES = [
    ("Momentum (analyst upgrades)", ".env"),
    ("Value (VLUE-style)", ".env.value"),
    ("Quality (QUAL-style)", ".env.quality"),
]


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


def analyze_strategy(label, env_file):
    if not os.path.exists(env_file):
        print(f"Skipping {label}: {env_file} not found in this directory.")
        return None

    load_dotenv(env_file, override=True)
    alpaca_key = os.environ.get("ALPACA_API_KEY")
    alpaca_secret = os.environ.get("ALPACA_SECRET_KEY")
    if not alpaca_key or not alpaca_secret:
        print(f"Skipping {label}: missing Alpaca credentials in {env_file}.")
        return None

    trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
    data_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)

    try:
        history = trading_client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1A", timeframe="1D")
        )
    except Exception as e:
        print(f"Skipping {label}: could not fetch portfolio history ({e}).")
        return None

    timestamps = history.timestamp or []
    equity = history.equity or []
    paired = [(t, e) for t, e in zip(timestamps, equity) if e is not None]

    if len(paired) < 2:
        print(f"Skipping {label}: only {len(paired)} portfolio-history data point(s) so far -- too early to compare.")
        return None

    start_ts, start_equity = paired[0]
    end_ts, end_equity = paired[-1]
    start_date = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_date = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    strategy_return_pct = (end_equity / start_equity - 1) * 100 if start_equity else None
    spy_return_pct = get_spy_return_pct(data_client, start_date, end_date)
    excess_return_pct = (
        strategy_return_pct - spy_return_pct
        if (strategy_return_pct is not None and spy_return_pct is not None)
        else None
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

    header = f"{'Strategy':<30}{'Window':<24}{'Equity':<15}{'Return':<10}{'SPY':<10}{'Excess':<10}"
    print(f"\n{header}")
    print("-" * len(header))
    for r in results:
        window = f"{r['start_date']} to {r['end_date']}"
        ret_str = f"{r['strategy_return_pct']:+.2f}%" if r["strategy_return_pct"] is not None else "n/a"
        spy_str = f"{r['spy_return_pct']:+.2f}%" if r["spy_return_pct"] is not None else "n/a"
        excess_str = f"{r['excess_return_pct']:+.2f}%" if r["excess_return_pct"] is not None else "n/a"
        equity_str = f"${r['end_equity']:,.2f}"
        print(f"{r['strategy']:<30}{window:<24}{equity_str:<15}{ret_str:<10}{spy_str:<10}{excess_str:<10}")


def main():
    results = []
    for label, env_file in STRATEGIES:
        result = analyze_strategy(label, env_file)
        if result:
            results.append(result)
    print_results(results)


if __name__ == "__main__":
    main()
