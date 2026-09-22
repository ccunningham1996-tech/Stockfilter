"""
5-year risk/return backtest for common retail-accessible factor ETFs
(momentum, quality, min-vol, value) benchmarked against SPY.

Pulls dividend+split adjusted daily closes from Alpaca and reports CAGR,
annualized volatility, Sharpe ratio, max drawdown, and Calmar ratio.

Note: unlike the rest of this repo (which uses adjustment='split' for
signal pricing), this script uses adjustment='all' so dividends are
included -- total return, not just price return, is what makes these
factor comparisons apples-to-apples.

Usage:
    python scripts/factor_backtest.py
    python scripts/factor_backtest.py --years 5 --rf 0.04 --tickers MTUM,QUAL,USMV,VLUE,SPY

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (see .env.example).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed, Adjustment

DEFAULT_TICKERS = ["MTUM", "QUAL", "USMV", "VLUE", "SPY"]
TRADING_DAYS_PER_YEAR = 252


def fetch_prices(client, tickers, start, end):
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
        adjustment=Adjustment.ALL,
    )
    bars = client.get_stock_bars(req).df
    prices = bars["close"].unstack(level=0)
    prices = prices.sort_index().dropna(how="all")
    return prices


def compute_metrics(prices: pd.DataFrame, rf_annual: float) -> pd.DataFrame:
    daily_returns = prices.pct_change().dropna(how="all")
    years = daily_returns.count() / TRADING_DAYS_PER_YEAR

    total_return = prices.iloc[-1] / prices.iloc[0] - 1
    cagr = (1 + total_return) ** (1 / years) - 1

    ann_vol = daily_returns.std() * np.sqrt(TRADING_DAYS_PER_YEAR)

    rf_daily = (1 + rf_annual) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    sharpe = ((daily_returns - rf_daily).mean() / daily_returns.std()) * np.sqrt(TRADING_DAYS_PER_YEAR)

    running_max = prices.cummax()
    drawdown = prices / running_max - 1
    max_drawdown = drawdown.min()

    calmar = cagr / max_drawdown.abs()

    return pd.DataFrame({
        "Total Return": total_return,
        "CAGR": cagr,
        "Ann. Volatility": ann_vol,
        "Sharpe": sharpe,
        "Max Drawdown": max_drawdown,
        "Calmar": calmar,
    })


def main():
    parser = argparse.ArgumentParser(description="Factor ETF risk/return backtest")
    parser.add_argument("--years", type=float, default=5)
    parser.add_argument("--rf", type=float, default=0.04, help="Annualized risk-free rate for Sharpe (default 4%%)")
    parser.add_argument("--tickers", type=str, default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--out", type=str, default="scratch/factor_backtest_results.csv")
    args = parser.parse_args()

    load_dotenv()
    alpaca_key = os.environ.get("ALPACA_API_KEY")
    alpaca_secret = os.environ.get("ALPACA_SECRET_KEY")
    if not alpaca_key or not alpaca_secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env before running.")

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    end = datetime.now()
    start = end - timedelta(days=int(args.years * 365.25) + 10)

    client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    prices = fetch_prices(client, tickers, start, end)

    missing = set(tickers) - set(prices.columns)
    if missing:
        print(f"Warning: no data returned for {sorted(missing)}")

    metrics = compute_metrics(prices, args.rf)
    metrics = metrics.sort_values("Sharpe", ascending=False)

    pd.options.display.float_format = "{:.3f}".format
    print(f"\n{args.years:.1f}-year backtest, {prices.index[0].date()} to {prices.index[-1].date()}")
    print(f"Risk-free rate assumption: {args.rf:.1%}\n")
    print(metrics.to_string())

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    metrics.to_csv(args.out)
    prices.to_csv(args.out.replace(".csv", "_prices.csv"))
    print(f"\nSaved metrics to {args.out}")
    print(f"Saved daily price series to {args.out.replace('.csv', '_prices.csv')}")


if __name__ == "__main__":
    main()
