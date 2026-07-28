"""
Rebalances the QUAL-style quality factor paper account.

Runs against its own Alpaca paper account + its own SQLite DB, configured
in .env.quality (copy .env.quality.example and fill in your own
quality-account keys). Rebalances at most once every 80 days by default --
safe to invoke daily from cron/scheduler, it'll just no-op most days.

Usage:
    python scripts/run_quality_screen.py
    python scripts/run_quality_screen.py --universe-limit 50   # quick test run
    python scripts/run_quality_screen.py --force                # ignore the interval guard
"""
import argparse

from dotenv import load_dotenv

# Must run before importing src.db (which calls load_dotenv() itself and
# would otherwise silently fall back to the shared .env / default DB).
load_dotenv(".env.quality")

from src.factor_metrics import QUALITY_METRICS
from src.factor_screener import run_strategy


def main():
    parser = argparse.ArgumentParser(description="Run/rebalance the QUAL-style quality factor screen")
    parser.add_argument("--top-n", type=int, default=40, help="Basket size (default 40)")
    parser.add_argument("--universe-limit", type=int, default=None, help="Only scan the first N tickers (quick test run)")
    parser.add_argument("--force", action="store_true", help="Rebalance even if the minimum interval hasn't elapsed")
    args = parser.parse_args()

    run_strategy(
        "quality",
        QUALITY_METRICS,
        top_n=args.top_n,
        min_rebalance_interval_days=0 if args.force else 80,
        universe_limit=args.universe_limit,
    )


if __name__ == "__main__":
    main()
