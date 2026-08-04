"""
Shared engine behind the VLUE-style value screen and QUAL-style quality
screen. Both strategies are mechanically identical -- score a universe on
a set of fundamental ratios, sector-neutralize, rank, and rebalance a
basket -- they just plug in different metric specs (src/factor_metrics.py).

Each strategy is expected to run against its own paper-trading account and
its own SQLite DB (via SCREENER_DB_PATH in a per-strategy .env file, see
src/db.py), so factor_holdings/factor_scores in a given DB always belong
to exactly one strategy.
"""
import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
import requests

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from src.db import get_connection, init_db
from src.universe import get_sp1500_tickers

FINNHUB_BASE = "https://finnhub.io/api/v1"
CACHE_MAX_AGE_DAYS = 25
FINNHUB_RATE_LIMIT_SLEEP = 1.1  # seconds between calls, matches existing repo convention
FINNHUB_MAX_RETRIES = 2  # extra attempts after the first, for transient errors only
FINNHUB_RETRY_BACKOFF_BASE = 2  # seconds; doubles each retry (2s, 4s, ...)


# ---------------------------------------------------------------------------
# Fundamentals fetch + cache
# ---------------------------------------------------------------------------

def _get_with_retry(url, params, timeout=10):
    """GETs a URL, retrying with exponential backoff on transient failures
    (timeouts, connection errors, 5xx server errors). Does NOT retry on 4xx
    client errors (bad key, unknown symbol, etc.) since retrying can't fix
    those -- it just raises immediately so the caller can log it once."""
    last_exception = None
    for attempt in range(FINNHUB_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status is not None and status < 500:
                raise  # 4xx: not transient, don't waste retries on it
            last_exception = e
        except requests.exceptions.RequestException as e:
            last_exception = e  # timeouts, connection errors: worth retrying

        if attempt < FINNHUB_MAX_RETRIES:
            sleep_time = FINNHUB_RETRY_BACKOFF_BASE * (2 ** attempt)
            time.sleep(sleep_time)

    raise last_exception

def _get_cached_fundamentals(ticker):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT sector, metrics_json, fetched_date FROM factor_fundamentals_cache WHERE ticker = ?",
        (ticker,),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None or row["fetched_date"] is None:
        return None
    fetched_date = datetime.strptime(row["fetched_date"], "%Y-%m-%d").date()
    if (datetime.now().date() - fetched_date).days > CACHE_MAX_AGE_DAYS:
        return None
    return {"sector": row["sector"], "metrics": json.loads(row["metrics_json"] or "{}")}


def _save_cached_fundamentals(ticker, sector, metrics):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO factor_fundamentals_cache (ticker, sector, metrics_json, fetched_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            sector = excluded.sector,
            metrics_json = excluded.metrics_json,
            fetched_date = excluded.fetched_date
        """,
        (ticker, sector, json.dumps(metrics), datetime.now().strftime("%Y-%m-%d")),
    )
    conn.commit()
    conn.close()


def fetch_fundamentals(ticker, finnhub_key, use_cache=True):
    """Returns (sector, raw_metrics_dict) for a ticker, using a 25-day cache
    to avoid re-hitting Finnhub's free-tier rate limit on every run."""
    if use_cache:
        cached = _get_cached_fundamentals(ticker)
        if cached is not None:
            return cached["sector"], cached["metrics"]

    sector = None
    metrics = {}
    metrics_fetch_succeeded = False
    try:
        resp = _get_with_retry(
            f"{FINNHUB_BASE}/stock/profile2",
            params={"symbol": ticker, "token": finnhub_key},
        )
        sector = (resp.json() or {}).get("finnhubIndustry")
    except Exception as e:
        print(f"  Warning: could not fetch profile for {ticker} (after retries): {e}")
    time.sleep(FINNHUB_RATE_LIMIT_SLEEP)  # pace every real call, not once per ticker -- each ticker makes 2

    try:
        resp = _get_with_retry(
            f"{FINNHUB_BASE}/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": finnhub_key},
        )
        metrics = (resp.json() or {}).get("metric") or {}
        metrics_fetch_succeeded = True
    except Exception as e:
        print(f"  Warning: could not fetch metrics for {ticker} (after retries): {e}")
    time.sleep(FINNHUB_RATE_LIMIT_SLEEP)

    # Only cache a genuinely successful metrics fetch -- caching a failed
    # call (e.g. bad credentials) would otherwise make every future run
    # silently reuse that empty result for CACHE_MAX_AGE_DAYS instead of
    # retrying once the real problem is fixed.
    if metrics_fetch_succeeded:
        _save_cached_fundamentals(ticker, sector, metrics)
    return sector, metrics


def _extract_metric(raw_metrics, field_candidates):
    for field in field_candidates:
        val = raw_metrics.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_records(records, metric_spec):
    """Pure scoring step (no network) -- sector-neutral z-score per metric,
    averaged into a composite score, ranked descending (best first).
    Split out from build_universe_scores so it can be unit-tested with
    synthetic data."""
    df = pd.DataFrame(records)
    if df.empty:
        return df

    for m in metric_spec:
        col = m["name"]
        z_col = f"{col}_z"

        def _sector_z(s):
            std = s.std(ddof=0)
            if pd.isna(std) or std == 0 or s.notna().sum() <= 1:
                return pd.Series(np.nan, index=s.index)
            return (s - s.mean()) / std

        df[z_col] = df.groupby("sector")[col].transform(_sector_z)
        if m["lower_is_better"]:
            df[z_col] = -df[z_col]

    z_cols = [f"{m['name']}_z" for m in metric_spec]
    df["composite_score"] = df[z_cols].mean(axis=1, skipna=True)
    df["n_metrics_available"] = df[z_cols].notna().sum(axis=1)

    # Require at least half the metrics (rounded up) so no ticker is ranked off one noisy ratio
    min_required = max(1, -(-len(metric_spec) // 2))
    df = df[df["n_metrics_available"] >= min_required].copy()
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    return df


def build_universe_scores(metric_spec, finnhub_key, tickers=None, verbose_every=50):
    """Fetches sector + fundamentals for the universe and returns a scored,
    ranked DataFrame (best composite score first)."""
    if tickers is None:
        tickers = get_sp1500_tickers()

    records = []
    for idx, ticker in enumerate(tickers):
        if idx > 0 and idx % verbose_every == 0:
            print(f"  Fetched fundamentals for {idx}/{len(tickers)} tickers...")

        sector, raw_metrics = fetch_fundamentals(ticker, finnhub_key)
        if not raw_metrics:
            continue

        row = {"ticker": ticker, "sector": sector or "Unknown"}
        have_any = False
        for m in metric_spec:
            val = _extract_metric(raw_metrics, m["field_candidates"])
            row[m["name"]] = val
            if val is not None:
                have_any = True
        if have_any:
            records.append(row)

    return _score_records(records, metric_spec)


def select_top_n(scored_df, n=40):
    return scored_df.head(n)["ticker"].tolist()


def record_scores(scored_df, as_of_date):
    conn = get_connection()
    cursor = conn.cursor()
    meta_cols = {"ticker", "sector", "composite_score", "rank", "n_metrics_available"}
    for _, row in scored_df.iterrows():
        raw_metrics = {k: (None if pd.isna(v) else v) for k, v in row.items() if k not in meta_cols}
        cursor.execute(
            """
            INSERT INTO factor_scores (as_of_date, ticker, sector, composite_score, rank, raw_metrics_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_date, ticker) DO UPDATE SET
                sector = excluded.sector,
                composite_score = excluded.composite_score,
                rank = excluded.rank,
                raw_metrics_json = excluded.raw_metrics_json
            """,
            (as_of_date, row["ticker"], row["sector"], float(row["composite_score"]), int(row["rank"]), json.dumps(raw_metrics)),
        )
    conn.commit()
    conn.close()


def days_since_last_rebalance():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(as_of_date) as last_date FROM factor_scores")
    row = cursor.fetchone()
    conn.close()
    if row is None or row["last_date"] is None:
        return None
    last_date = datetime.strptime(row["last_date"], "%Y-%m-%d").date()
    return (datetime.now().date() - last_date).days


# ---------------------------------------------------------------------------
# Rebalance execution
# ---------------------------------------------------------------------------

def get_current_holdings():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM factor_holdings WHERE status = 'open'")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def rebalance(target_tickers, trading_client, data_client):
    """Diffs target_tickers against currently open factor_holdings: sells
    positions that fell out of the target basket (freeing up cash first),
    then equal-weights the account's resulting cash across newly-added
    names."""
    current = get_current_holdings()
    current_tickers = {h["ticker"] for h in current}
    target_set = set(target_tickers)

    to_sell = [h for h in current if h["ticker"] not in target_set]
    to_buy = [t for t in target_tickers if t not in current_tickers]

    conn = get_connection()
    cursor = conn.cursor()
    today_str = datetime.now().strftime("%Y-%m-%d")

    for h in to_sell:
        ticker = h["ticker"]
        qty = h["qty"]
        try:
            order = trading_client.submit_order(MarketOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
            ))
            print(f"SELL {ticker} x{qty} submitted (order {order.id})")
            cursor.execute(
                "UPDATE factor_holdings SET status = 'closed', exit_date = ? WHERE id = ?",
                (today_str, h["id"]),
            )
        except Exception as e:
            print(f"Failed to sell {ticker}: {e}")
    conn.commit()

    if to_buy:
        account = trading_client.get_account()
        available_cash = float(account.cash)
        cash_per_position = available_cash / len(to_buy)
        print(f"Allocating ${cash_per_position:.2f} to each of {len(to_buy)} new positions.")

        for ticker in to_buy:
            try:
                quote_resp = data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
                )
                quote = quote_resp.get(ticker)
                if quote is None:
                    print(f"  Skipping {ticker}: no quote returned.")
                    continue
                price = (
                    (quote.ask_price + quote.bid_price) / 2
                    if (quote.ask_price and quote.bid_price)
                    else (quote.ask_price or quote.bid_price)
                )
                if not price or price <= 0:
                    print(f"  Skipping {ticker}: no valid quote price.")
                    continue

                qty = int(cash_per_position / price)
                if qty <= 0:
                    print(f"  Skipping {ticker}: allocation ${cash_per_position:.2f} too small for 1 share at ${price:.2f}.")
                    continue

                order = trading_client.submit_order(MarketOrderRequest(
                    symbol=ticker, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                ))
                print(f"BUY {ticker} x{qty} at ~${price:.2f} submitted (order {order.id})")
                cursor.execute(
                    """
                    INSERT INTO factor_holdings (ticker, entry_date, entry_price, qty, status)
                    VALUES (?, ?, ?, ?, 'open')
                    """,
                    (ticker, today_str, round(price, 2), qty),
                )
            except Exception as e:
                print(f"Failed to buy {ticker}: {e}")
        conn.commit()

    conn.close()
    print(f"Rebalance complete: sold {len(to_sell)}, bought {len(to_buy)}, target basket size {len(target_tickers)}.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_strategy(strategy_name, metric_spec, top_n=40, min_rebalance_interval_days=80, universe_limit=None):
    init_db()  # ensures this profile's DB file, directory, and tables exist before first use

    finnhub_key = os.getenv("FINNHUB_API_KEY")
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    if not all([finnhub_key, alpaca_key, alpaca_secret]):
        raise ValueError(
            "Missing FINNHUB_API_KEY / ALPACA_API_KEY / ALPACA_SECRET_KEY. "
            "Check that the correct .env.<profile> file was loaded before calling run_strategy()."
        )

    days_since = days_since_last_rebalance()
    if days_since is not None and days_since < min_rebalance_interval_days:
        print(
            f"[{strategy_name}] Last rebalance was {days_since} days ago "
            f"(< {min_rebalance_interval_days}-day interval). Skipping. Use --force to override."
        )
        return

    tickers = get_sp1500_tickers()
    if universe_limit:
        tickers = tickers[:universe_limit]
    print(f"[{strategy_name}] Scoring {len(tickers)} tickers...")

    scored = build_universe_scores(metric_spec, finnhub_key, tickers=tickers)
    if scored.empty:
        print(f"[{strategy_name}] No tickers scored -- aborting rebalance.")
        return

    as_of_date = datetime.now().strftime("%Y-%m-%d")
    record_scores(scored, as_of_date)

    target_tickers = select_top_n(scored, n=top_n)
    print(f"[{strategy_name}] Target basket ({len(target_tickers)}): {target_tickers}")

    trading_client = TradingClient(alpaca_key, alpaca_secret, paper=True)
    data_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
    rebalance(target_tickers, trading_client, data_client)
