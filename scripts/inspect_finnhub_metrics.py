"""
Debug helper: prints the raw Finnhub /stock/profile2 and /stock/metric
response for one ticker, so you can confirm the exact field names before
trusting src/factor_metrics.py's field_candidates lists against them.

Run this once against a couple of tickers before your first real
scripts/run_value_screen.py or scripts/run_quality_screen.py run -- if a
metric's field_candidates list is wrong, that metric will silently be
skipped for every ticker rather than erroring loudly.

Usage:
    python scripts/inspect_finnhub_metrics.py AAPL
"""
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        sys.exit("Set FINNHUB_API_KEY in .env first.")

    profile_resp = requests.get(
        "https://finnhub.io/api/v1/stock/profile2",
        params={"symbol": ticker, "token": key},
        timeout=10,
    )
    profile_resp.raise_for_status()
    print(f"=== profile2 for {ticker} ===")
    print(json.dumps(profile_resp.json(), indent=2))

    metric_resp = requests.get(
        "https://finnhub.io/api/v1/stock/metric",
        params={"symbol": ticker, "metric": "all", "token": key},
        timeout=10,
    )
    metric_resp.raise_for_status()
    metrics = (metric_resp.json() or {}).get("metric") or {}
    print(f"\n=== metric=all for {ticker}: {len(metrics)} available keys ===")
    for k in sorted(metrics.keys()):
        print(f"  {k} = {metrics[k]}")


if __name__ == "__main__":
    main()
