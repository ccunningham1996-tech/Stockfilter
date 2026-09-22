"""
NDX Momentum Buffered -- paper-trading entry point.

Commands:
  rebalance [--dry-run] [--force] [--resume]
      Monthly rebalance. Exits immediately unless today is the first trading
      day of the month (per Alpaca's market calendar). --dry-run prints the
      ranked list, keep/sell/buy decisions and target orders without trading,
      on any day. --force runs on a non-first trading day (initial launch or
      catch-up). --resume finishes a rebalance recorded as partial.
  snapshot       Daily after-close record of equity, SPY close and QQQ close.
  report         Performance vs SPY and QQQ, drawdown, monthly returns, sectors.
  check-accounts Shows every strategy profile's paper account; flags sharing.

Credentials come only from .env.ndx_mom_buffer (never the shared .env), and
the strategy refuses to run on anything but the Alpaca paper endpoint or on
an account used by another strategy profile.

Exit codes: 0 ok / nothing to do, 1 refused by a safety guard, 2 alert raised.
"""
import argparse
import logging
import sys
from datetime import datetime

from alpaca.data.historical import StockHistoricalDataClient

from src.ndx_mom_buffer_runner import (
    ET, GuardError, Runner, assert_dedicated_account, check_accounts, load_profile,
    make_trading_client,
)


def _configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter("%(asctime)s ET %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda *_: datetime.now(ET).timetuple()
    handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main(argv=None):
    parser = argparse.ArgumentParser(description="NDX Momentum Buffered (paper trading only)")
    sub = parser.add_subparsers(dest="command", required=True)
    reb = sub.add_parser("rebalance")
    reb.add_argument("--dry-run", action="store_true")
    reb.add_argument("--force", action="store_true")
    reb.add_argument("--resume", action="store_true")
    sub.add_parser("snapshot")
    sub.add_parser("report")
    sub.add_parser("check-accounts")
    args = parser.parse_args(argv)
    _configure_logging()
    log = logging.getLogger("ndx_mom_buffer")

    if args.command == "check-accounts":
        return check_accounts()

    try:
        profile = load_profile()
        tc = make_trading_client(profile["ALPACA_API_KEY"], profile["ALPACA_SECRET_KEY"])
        if args.command == "rebalance":
            account = assert_dedicated_account(tc, profile["ALPACA_API_KEY"])
            log.info("Using dedicated paper account %s", account)
    except GuardError as e:
        log.error("REFUSING TO RUN: %s", e)
        return 1

    dc = StockHistoricalDataClient(profile["ALPACA_API_KEY"], profile["ALPACA_SECRET_KEY"])
    runner = Runner(profile, tc, dc)
    try:
        if args.command == "rebalance":
            return runner.rebalance(dry_run=args.dry_run, force=args.force, resume=args.resume)
        if args.command == "snapshot":
            return runner.snapshot()
        return runner.report()
    except Exception as e:
        runner.alert(f"{args.command} crashed", f"{type(e).__name__}: {e}")
        log.exception("Unhandled error")
        return 2


if __name__ == "__main__":
    sys.exit(main())
