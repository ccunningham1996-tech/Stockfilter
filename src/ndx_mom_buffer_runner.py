"""
NDX Momentum Buffered -- broker/IO layer.

Safety guards (paper-only endpoint, dedicated account), calendar gating,
the monthly rebalance (sells first, then buys, retry-once, alert, idempotent),
per-rebalance JSON/CSV records, the daily equity/SPY/QQQ snapshot, and the
performance report. Strategy rules live in src/ndx_mom_buffer.py.

All state for this strategy lives in data/ndx_mom_buffer.db and
data/ndx_mom_buffer/, and credentials are read directly from
.env.ndx_mom_buffer (never from the shared environment), so it cannot
accidentally pick up another strategy's keys or database.
"""
import csv
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import dotenv_values

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from src import ndx_mom_buffer as strat
from src.ndx_universe import load_universe

STRATEGY_NAME = "NDX Momentum Buffered"
SLUG = "ndx_mom_buffer"
PROFILE_FILE = ".env.ndx_mom_buffer"
PAPER_URL = "https://paper-api.alpaca.markets"
ORDER_PREFIX = "ndxmb-"
ET = ZoneInfo("America/New_York")
STARTING_CAPITAL = 100_000.0
HISTORY_TRADING_DAYS = 420  # >= 400 required; a little headroom
OTHER_PROFILES = [
    (".env", "Momentum (analyst upgrades)"),
    (".env.value", "Value (VLUE-style)"),
    (".env.quality", "Quality (QUAL-style)"),
]
DATA_DIR = os.path.join("data", SLUG)
DB_PATH = os.path.join("data", f"{SLUG}.db")
REBALANCE_WINDOW = (dtime(9, 35), dtime(15, 30))
ORDER_WAIT_SECONDS = 180
SNAPSHOT_DELAY_AFTER_CLOSE = timedelta(minutes=16)  # SIP daily bars need >15 min

TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
            OrderStatus.DONE_FOR_DAY, OrderStatus.STOPPED, OrderStatus.SUSPENDED, OrderStatus.REPLACED}

log = logging.getLogger(SLUG)


class GuardError(Exception):
    """A safety check refused to let the strategy run."""


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def load_profile(path=PROFILE_FILE):
    if not os.path.exists(path):
        raise GuardError(f"{path} not found. Create it from {path}.example with this strategy's "
                         f"OWN Alpaca paper-account keys.")
    vals = dotenv_values(path)
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        v = vals.get(k) or ""
        if not v or v.startswith("your_"):
            raise GuardError(f"{k} is missing or still a placeholder in {path}.")
    for source, url in (("profile ALPACA_BASE_URL", vals.get("ALPACA_BASE_URL")),
                        ("env ALPACA_BASE_URL", os.environ.get("ALPACA_BASE_URL")),
                        ("env APCA_API_BASE_URL", os.environ.get("APCA_API_BASE_URL"))):
        if url and url.rstrip("/") != PAPER_URL:
            raise GuardError(f"{source} is {url}; {STRATEGY_NAME} only runs against {PAPER_URL}.")
    return vals


def make_trading_client(key, secret):
    tc = TradingClient(key, secret, paper=True, url_override=PAPER_URL)
    actual = str(getattr(tc, "_base_url", "")).rstrip("/")
    if actual != PAPER_URL:
        raise GuardError(f"Trading client base URL is {actual!r}, expected {PAPER_URL}. Refusing to run.")
    return tc


def other_profile_accounts(my_key):
    """Yields (label, env_file, account_number|None, note) for each other profile."""
    for env_file, label in OTHER_PROFILES:
        if not os.path.exists(env_file):
            yield label, env_file, None, "profile file not present"
            continue
        vals = dotenv_values(env_file)
        key, secret = vals.get("ALPACA_API_KEY"), vals.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            yield label, env_file, None, "no Alpaca keys in profile"
            continue
        if key == my_key:
            yield label, env_file, "SAME-API-KEY", "identical API key"
            continue
        try:
            yield label, env_file, make_trading_client(key, secret).get_account().account_number, None
        except Exception as e:
            yield label, env_file, None, f"could not query paper account ({e})"


def assert_dedicated_account(tc, my_key):
    mine = tc.get_account().account_number
    for label, env_file, acct, note in other_profile_accounts(my_key):
        if acct in ("SAME-API-KEY", mine):
            raise GuardError(f"{STRATEGY_NAME} is configured with the same Alpaca account as "
                             f"{label} ({env_file}, account {mine}). It must have its own paper "
                             f"account so positions are never shared. Refusing to run.")
        if acct is None and note != "profile file not present":
            log.warning("Could not verify %s is a different account: %s", label, note)
    return mine


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS ndx_rebalances (
    month TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, signal_date TEXT,
    status TEXT, universe_size INTEGER, equity_before REAL,
    turnover_one_sided REAL, record_path TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS ndx_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT, client_order_id TEXT UNIQUE,
    alpaca_order_id TEXT, symbol TEXT, side TEXT, kind TEXT, attempt INTEGER,
    qty REAL, notional REAL, status TEXT, filled_qty REAL, filled_avg_price REAL,
    trade_date TEXT, error TEXT, submitted_at TEXT);
CREATE TABLE IF NOT EXISTS ndx_daily (
    date TEXT PRIMARY KEY, equity REAL, cash REAL, n_positions INTEGER,
    spy_close REAL, qqq_close REAL, recorded_at TEXT);
"""


def connect(db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, profile, tc, dc, now_fn=None, db_path=DB_PATH, data_dir=DATA_DIR,
                 universe_fetch=None, sleep=time.sleep, order_wait_seconds=ORDER_WAIT_SECONDS):
        self.profile = profile
        self.tc, self.dc = tc, dc
        self.now = now_fn or (lambda: datetime.now(ET))
        self.db_path, self.data_dir = db_path, data_dir
        self.universe_fetch = universe_fetch
        self.sleep = sleep
        self.order_wait_seconds = order_wait_seconds
        self._fractionable = {}
        self._last_close = {}

    # --- alerts -----------------------------------------------------------
    def alert(self, subject, detail):
        msg = f"[{STRATEGY_NAME}] {subject}: {detail}"
        log.error("ALERT %s", msg)
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, "ALERTS.log"), "a") as f:
            f.write(f"{self.now().isoformat(timespec='seconds')} {msg}\n")
        url = self.profile.get("ALERT_WEBHOOK_URL")
        if url:
            try:
                requests.post(url, json={"text": msg, "content": msg}, timeout=10)
            except Exception as e:
                log.error("Alert webhook failed: %s", e)

    # --- market data / calendar -----------------------------------------------
    def trading_days(self, start, end):
        return [c.date for c in self.tc.get_calendar(GetCalendarRequest(start=start, end=end))]

    def calendar_day(self, day):
        cal = self.tc.get_calendar(GetCalendarRequest(start=day, end=day))
        return cal[0] if cal and cal[0].date == day else None

    def _bars(self, symbols, start_dt, end_dt, adjustment, feed):
        return self.dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start_dt, end=end_dt,
            adjustment=adjustment, feed=feed)).df

    def fetch_closes(self, symbols, start, end_dt, adjustment=Adjustment.ALL):
        """Daily closes (DataFrame, index = ET trading date). Tries SIP
        (consolidated) first, then IEX. If a batch fails on one bad symbol,
        falls back to per-symbol requests so one symbol can't sink the run."""
        start_dt = datetime.combine(start, dtime(0, 0), tzinfo=ET)
        last_err = None
        for feed in (DataFeed.SIP, DataFeed.IEX):
            try:
                try:
                    frames = [self._bars(symbols, start_dt, end_dt, adjustment, feed)]
                except Exception as batch_err:
                    if len(symbols) == 1 or "symbol" not in str(batch_err).lower():
                        raise
                    log.warning("Batch bar request failed (%s); retrying per symbol", batch_err)
                    frames = []
                    for s in symbols:
                        try:
                            frames.append(self._bars([s], start_dt, end_dt, adjustment, feed))
                        except Exception as e:
                            log.warning("No bars for %s: %s", s, e)
                frames = [f for f in frames if f is not None and not f.empty]
                if not frames:
                    raise ValueError("no bars returned")
                bars = pd.concat(frames)
                closes = bars["close"].unstack(level=0)
                idx = pd.DatetimeIndex(closes.index)
                if idx.tz is not None:
                    idx = idx.tz_convert(ET).tz_localize(None)
                closes.index = idx.normalize()
                closes = closes[~closes.index.duplicated(keep="last")].sort_index()
                if feed is DataFeed.IEX:
                    log.warning("SIP bars unavailable (%s); using IEX-only bars, whose closes can "
                                "differ slightly from consolidated closes", last_err)
                return closes, feed.value
            except Exception as e:
                last_err = e
                log.warning("Bar fetch via %s failed: %s", feed.value, e)
        raise RuntimeError(f"Could not fetch daily bars: {last_err}")

    def positions(self):
        out = {}
        for p in self.tc.get_all_positions():
            out[p.symbol] = {"qty": float(p.qty), "market_value": float(p.market_value or 0),
                             "current_price": float(p.current_price or 0)}
        return out

    def is_fractionable(self, symbol):
        if symbol not in self._fractionable:
            try:
                self._fractionable[symbol] = bool(self.tc.get_asset(symbol).fractionable)
            except Exception as e:
                log.warning("Could not read asset info for %s (%s); assuming fractionable", symbol, e)
                self._fractionable[symbol] = True
        return self._fractionable[symbol]

    # --- db ---------------------------------------------------------------------
    @contextmanager
    def db(self):
        conn = connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_rebalance(self, month):
        with self.db() as conn:
            row = conn.execute("SELECT * FROM ndx_rebalances WHERE month = ?", (month,)).fetchone()
        return dict(row) if row else None

    def _save_order(self, res):
        with self.db() as conn:
            conn.execute("""
                INSERT INTO ndx_orders (month, client_order_id, alpaca_order_id, symbol, side, kind,
                    attempt, qty, notional, status, filled_qty, filled_avg_price, trade_date, error, submitted_at)
                VALUES (:month, :client_order_id, :order_id, :symbol, :side, :kind, :attempt, :qty,
                    :notional, :status, :filled_qty, :filled_avg_price, :trade_date, :error, :submitted_at)
                ON CONFLICT(client_order_id) DO UPDATE SET alpaca_order_id=excluded.alpaca_order_id,
                    status=excluded.status, filled_qty=excluded.filled_qty,
                    filled_avg_price=excluded.filled_avg_price, trade_date=excluded.trade_date,
                    error=excluded.error
            """, res)

    # --- orders -----------------------------------------------------------------
    def _submit(self, spec, month, run_tag, attempt):
        coid = f"{ORDER_PREFIX}{month}-{run_tag}-{spec['side']}-{spec['symbol']}-a{attempt}"
        res = {"month": month, "client_order_id": coid, "order_id": None, "symbol": spec["symbol"],
               "side": spec["side"], "kind": spec["kind"], "attempt": attempt, "qty": spec.get("qty"),
               "notional": spec.get("notional"), "status": None, "filled_qty": 0.0,
               "filled_avg_price": None, "trade_date": None, "error": None,
               "submitted_at": self.now().isoformat(timespec="seconds"), "spec": spec}
        kwargs = {"symbol": spec["symbol"], "time_in_force": TimeInForce.DAY, "client_order_id": coid,
                  "side": OrderSide.SELL if spec["side"] == "sell" else OrderSide.BUY}
        if spec.get("qty") is not None:
            kwargs["qty"] = spec["qty"]
        elif self.is_fractionable(spec["symbol"]):
            kwargs["notional"] = round(spec["notional"], 2)
        else:
            price = self._last_close.get(spec["symbol"])
            shares = math.floor(spec["notional"] / price) if price else 0
            if shares < 1:
                res.update(status="skipped", error="not fractionable and under one share")
                self._save_order({k: v for k, v in res.items() if k != "spec"})
                return res
            kwargs["qty"] = shares
            res["qty"] = shares
        try:
            order = self.tc.submit_order(MarketOrderRequest(**kwargs))
            res.update(order_id=str(order.id), status=order.status.value)
            log.info("Submitted %s %s %s (%s) -> %s", spec["side"].upper(), spec["symbol"],
                     f"qty {kwargs['qty']}" if "qty" in kwargs else f"${kwargs['notional']:,.2f}",
                     coid, order.status.value)
        except Exception as e:
            res.update(status="submit_error", error=str(e))
            log.error("Order submit failed for %s %s: %s", spec["side"], spec["symbol"], e)
        self._save_order({k: v for k, v in res.items() if k != "spec"})
        return res

    def _wait(self, results):
        live = [r for r in results if r["order_id"]]
        deadline = time.monotonic() + self.order_wait_seconds
        while live:
            for r in list(live):
                o = self.tc.get_order_by_id(r["order_id"])
                self._apply_order_state(r, o)
                if o.status in TERMINAL:
                    live.remove(r)
                    self._save_order({k: v for k, v in r.items() if k != "spec"})
            if not live:
                break
            if time.monotonic() >= deadline:
                for r in live:
                    try:
                        self.tc.cancel_order_by_id(r["order_id"])
                        o = self.tc.get_order_by_id(r["order_id"])
                        self._apply_order_state(r, o)
                    except Exception as e:
                        log.error("Cancel after timeout failed for %s: %s", r["symbol"], e)
                    r["error"] = f"not filled within {self.order_wait_seconds}s; canceled"
                    if r["status"] == OrderStatus.FILLED.value:
                        r["error"] = None
                    self._save_order({k: v for k, v in r.items() if k != "spec"})
                break
            self.sleep(2)

    def _apply_order_state(self, r, o):
        r["status"] = o.status.value
        r["filled_qty"] = float(o.filled_qty or 0)
        r["filled_avg_price"] = float(o.filled_avg_price) if o.filled_avg_price else None
        if o.filled_at:
            r["trade_date"] = o.filled_at.astimezone(ET).date().isoformat()

    def _retry_spec(self, r):
        spec = r["spec"]
        if spec["kind"] == "exit":
            pos = self.positions().get(spec["symbol"])
            return {**spec, "qty": pos["qty"]} if pos and pos["qty"] > 0 else None
        if spec.get("qty") is not None and r["status"] != "skipped":
            remaining = spec["qty"] - r["filled_qty"]
            return {**spec, "qty": remaining} if remaining > 0 else None
        done = r["filled_qty"] * (r["filled_avg_price"] or 0)
        remaining = round(spec["notional"] - done, 2)
        return {**spec, "notional": remaining} if remaining >= 1.0 else None

    def execute_phase(self, specs, month, run_tag, phase):
        """Submits all orders in the phase, waits for them, retries each
        failure once. Returns (all_results, failures)."""
        if not specs:
            return [], []
        log.info("%s phase: submitting %d orders", phase, len(specs))
        first = [self._submit(s, month, run_tag, 1) for s in specs]
        self._wait(first)
        retries = []
        for r in first:
            if r["status"] == OrderStatus.FILLED.value or r["status"] == "skipped":
                continue
            spec2 = self._retry_spec(r)
            if spec2 is None:
                continue
            log.warning("Retrying %s %s once (first attempt: %s %s)", r["side"], r["symbol"],
                        r["status"], r["error"] or "")
            retries.append(self._submit(spec2, month, run_tag, 2))
        self._wait(retries)
        final = {}
        for r in first + retries:
            final[r["symbol"]] = r
        failures = [r for r in final.values()
                    if r["status"] not in (OrderStatus.FILLED.value, "skipped")
                    and self._retry_spec(r) is not None]
        return first + retries, failures

    # --- rebalance ------------------------------------------------------------
    def orders_already_placed(self, month, month_start):
        orders = self.tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=500,
            after=datetime.combine(month_start, dtime(0, 0), tzinfo=ET)))
        return [o for o in orders if (o.client_order_id or "").startswith(f"{ORDER_PREFIX}{month}-")]

    def rebalance(self, dry_run=False, force=False, resume=False):
        now = self.now()
        today = now.date()
        days = self.trading_days(today - timedelta(days=760), today + timedelta(days=40))
        month = f"{today:%Y-%m}"

        if not dry_run:
            if today not in days:
                log.info("%s is not a trading day; nothing to do.", today)
                return 0
            if not force and not strat.is_first_trading_day_of_month(days, today):
                log.info("%s is not the first trading day of %s; nothing to do.", today, month)
                return 0
            existing = self.get_rebalance(month)
            if existing and existing["status"] == "completed":
                log.info("Rebalance for %s already completed at %s; exiting.", month, existing["finished_at"])
                return 0
            if existing and not resume:
                self.alert("Previous rebalance incomplete",
                           f"{month} status is '{existing['status']}'. Check the orders in Alpaca, "
                           f"then rerun with --resume to finish it against live positions.")
                return 2
            month_start = min(d for d in days if (d.year, d.month) == (today.year, today.month))
            if not existing and not resume and self.orders_already_placed(month, month_start):
                self.alert("Orders exist without a record",
                           f"Found {ORDER_PREFIX}{month}-* orders in Alpaca but no local record for {month}. "
                           f"Inspect them, then rerun with --resume.")
                return 2
            if not (REBALANCE_WINDOW[0] <= now.time() <= REBALANCE_WINDOW[1]):
                self.alert("Outside rebalance window",
                           f"It is {now:%H:%M} ET; rebalances run 09:35-15:30 ET. "
                           f"Rerun with --force during market hours.")
                return 2
            if not self.tc.get_clock().is_open:
                self.alert("Market closed", f"Market is closed at {now:%H:%M} ET on {today}.")
                return 2

        t0 = strat.signal_date_for(days, today)
        month_ends = [d for d in strat.month_end_trading_days(days) if d <= t0]
        history = [d for d in days if d <= t0]
        if len(history) < HISTORY_TRADING_DAYS:
            raise RuntimeError(f"Calendar only has {len(history)} trading days up to {t0}")
        start = history[-HISTORY_TRADING_DAYS]

        universe = load_universe(os.path.join(self.data_dir, "universe_cache.json"),
                                 **({"fetch": self.universe_fetch} if self.universe_fetch else {}))
        members = universe["members"]
        symbols = [m["symbol"] for m in members]
        sector_map = {m["symbol"]: m["sector"] for m in members}
        company_map = {m["symbol"]: m["company"] for m in members}

        closes, feed = self.fetch_closes(symbols, start, datetime.combine(t0, dtime(23, 59), tzinfo=ET))
        for s in symbols:
            if s not in closes.columns:
                closes[s] = float("nan")
        closes = closes[symbols]
        signals = strat.compute_signals(closes, month_ends, t0)
        self._last_close = {r["symbol"]: r["close"] for _, r in signals.iterrows() if r["close"]}

        account = self.tc.get_account()
        equity, cash = float(account.equity), float(account.cash)
        positions = self.positions()
        decisions, targets = strat.build_portfolio(signals, positions.keys())
        sells, buys, skipped = strat.plan_orders(decisions, targets, positions, equity)

        record = {
            "strategy": STRATEGY_NAME, "month": month, "mode": "dry_run" if dry_run else "live",
            "run_at": now.isoformat(timespec="seconds"), "signal_date": t0.isoformat(),
            "execution_date": today.isoformat(), "bar_feed": feed,
            "universe": {"size": len(symbols), "source": universe["source"], "as_of": universe["as_of"],
                         "sector_source": universe.get("sector_source"), "warning": universe["warning"]},
            "equity_before": equity, "cash_before": cash,
            "positions_before": {s: p["market_value"] for s, p in positions.items()},
            "ranked": [
                {"rank": None if pd.isna(r["rank"]) else int(r["rank"]), "symbol": r["symbol"],
                 "company": company_map.get(r["symbol"], ""), "sector": sector_map.get(r["symbol"], ""),
                 "momentum_12_1": _num(r["momentum"]), "close": _num(r["close"]), "sma200": _num(r["sma200"]),
                 "trend_ok": bool(r["trend_ok"]), "eligible": bool(r["eligible"]),
                 "ineligible_reason": r["ineligible_reason"]}
                for _, r in signals.iterrows()],
            "decisions": decisions,
            "target_weights": {s: strat.TARGET_WEIGHT for s in targets},
            "unfilled_slots_in_cash": strat.TARGET_HOLDINGS - len(targets),
            "orders_planned": {"sells": sells, "buys": buys, "skipped_under_threshold": skipped},
            "planned_turnover": strat.turnover(sells, buys, equity),
            "target_sector_counts": strat.sector_counts(targets, sector_map),
        }
        if universe["warning"]:
            log.warning(universe["warning"])
        self.print_plan(record)

        if dry_run:
            paths = self.write_record(record)
            log.info("Dry run only: no orders submitted. Record: %s", paths[0])
            return 0

        return self._execute(record, month, t0, decisions, targets, sells, equity, sector_map)

    def _execute(self, record, month, t0, decisions, targets, sells, equity, sector_map):
        prior = self.get_rebalance(month)
        with self.db() as conn:
            conn.execute("""INSERT INTO ndx_rebalances (month, started_at, signal_date, status,
                              universe_size, equity_before) VALUES (?, ?, ?, 'in_progress', ?, ?)
                            ON CONFLICT(month) DO UPDATE SET status='in_progress'""",
                         (month, record["run_at"], t0.isoformat(), record["universe"]["size"], equity))
        if prior is None:
            with self.db() as conn:
                first_ever = conn.execute("SELECT COUNT(*) FROM ndx_rebalances").fetchone()[0] == 1
            if first_ever and abs(equity - STARTING_CAPITAL) / STARTING_CAPITAL > 0.01:
                log.warning(f"First rebalance but account equity is ${equity:,.2f}, not the intended "
                            f"${STARTING_CAPITAL:,.0f} starting capital.")

        run_tag = uuid.uuid4().hex[:6]
        sell_results, sell_failures = self.execute_phase(sells, month, run_tag, "SELL")

        cash_after = float(self.tc.get_account().cash)
        _, buys, _ = strat.plan_orders(decisions, targets, self.positions(), equity)
        notes = []
        if cash_after <= 0:
            notes.append(f"cash ${cash_after:,.2f} after sells; no buys submitted")
            buys = []
        buys, factor = strat.scale_buys_to_cash(buys, cash_after)
        if factor < 1.0:
            notes.append(f"buys scaled by {factor:.3f} to fit cash ${cash_after:,.2f} (no margin)")
            log.warning(notes[-1])
        buy_results, buy_failures = self.execute_phase(buys, month, run_tag, "BUY")

        final_positions = self.positions()
        final_equity = float(self.tc.get_account().equity)
        executed = [{k: v for k, v in r.items() if k != "spec"} for r in sell_results + buy_results]
        filled_value = sum((r["filled_qty"] or 0) * (r["filled_avg_price"] or 0) for r in executed)
        failures = sell_failures + buy_failures
        record.update({
            "orders_executed": executed,
            "actual_weights": {s: round(p["market_value"] / final_equity, 4) for s, p in final_positions.items()},
            "actual_cash_weight": round(1 - sum(p["market_value"] for p in final_positions.values()) / final_equity, 4),
            "equity_after": final_equity,
            "turnover": {"gross_traded": round(filled_value, 2),
                         "one_sided": round(filled_value / (2 * equity), 4) if equity else None},
            "actual_sector_counts": strat.sector_counts(final_positions.keys(), sector_map),
            "notes": notes,
            "failures": [{"symbol": r["symbol"], "side": r["side"], "status": r["status"], "error": r["error"]}
                         for r in failures],
            "status": "completed" if not failures else "partial",
        })
        json_path, _ = self.write_record(record)
        with self.db() as conn:
            conn.execute("""UPDATE ndx_rebalances SET finished_at=?, status=?, turnover_one_sided=?,
                              record_path=?, notes=? WHERE month=?""",
                         (self.now().isoformat(timespec="seconds"), record["status"],
                          record["turnover"]["one_sided"], json_path, "; ".join(notes), month))
        log.info("Rebalance %s %s: %d sells, %d buys, turnover %.1f%%. Record: %s", month,
                 record["status"].upper(), len(sell_results), len(buy_results),
                 100 * (record["turnover"]["one_sided"] or 0), json_path)
        if failures:
            self.alert("Rebalance incomplete",
                       f"{month}: {len(failures)} order(s) still not filled after one retry: "
                       + ", ".join(f"{f['side']} {f['symbol']} ({f['status']})" for f in record["failures"])
                       + ". Rerun with --resume to finish against live positions.")
            return 2
        return 0

    # --- records & printing -------------------------------------------------------
    def write_record(self, record):
        sub = "dryrun" if record["mode"] == "dry_run" else "rebalances"
        out_dir = os.path.join(self.data_dir, sub)
        os.makedirs(out_dir, exist_ok=True)
        stem = (record["month"] if record["mode"] == "live"
                else record["run_at"][:19].replace(":", "").replace("T", "_"))
        json_path = os.path.join(out_dir, f"{stem}.json")
        csv_path = os.path.join(out_dir, f"{stem}_ranked.csv")
        with open(json_path, "w") as f:
            json.dump(record, f, indent=2, default=str)
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(record["ranked"][0].keys()))
            w.writeheader()
            w.writerows(record["ranked"])
        return json_path, csv_path

    def print_plan(self, record):
        p = print
        u = record["universe"]
        p(f"\n=== {STRATEGY_NAME} -- {'DRY RUN' if record['mode'] == 'dry_run' else 'LIVE REBALANCE'} ===")
        p(f"Run at {record['run_at']} | signals as of close {record['signal_date']} | bars: {record['bar_feed']}")
        p(f"Universe: {u['size']} names ({u['source']}, list as of {u['as_of']}, sectors: {u['sector_source']})")
        if u["warning"]:
            p(f"WARNING: {u['warning']}")
        p(f"Equity ${record['equity_before']:,.2f} | cash ${record['cash_before']:,.2f} | "
          f"current holdings: {len(record['positions_before'])}")
        p("\nRanked list (eligible names; * = Trend OK):")
        p(f"  {'Rank':>4}  {'Symbol':<6} {'12-1 Mom':>9}  {'Close':>10} {'SMA200':>10}  Trend  Sector")
        for r in record["ranked"]:
            if not r["eligible"]:
                continue
            p(f"  {r['rank']:>4}  {r['symbol']:<6} {r['momentum_12_1']:>+9.1%}  {r['close']:>10.2f} "
              f"{r['sma200']:>10.2f}  {'OK *' if r['trend_ok'] else 'fail '}  {r['sector']}")
        inel = [r for r in record["ranked"] if not r["eligible"]]
        if inel:
            p(f"\nIneligible ({len(inel)}):")
            for r in inel:
                p(f"  {r['symbol']:<6} {r['ineligible_reason']}")
        p("\nDecisions:")
        for d in record["decisions"]:
            p(f"  {d['action'].upper():<5} {d['symbol']:<6} {d['reason']}")
        if record["unfilled_slots_in_cash"]:
            p(f"  CASH  {record['unfilled_slots_in_cash']} unfilled slot(s) held in cash")
        orders = record["orders_planned"]
        p("\nTarget orders (sells first, then buys):")
        for o in orders["sells"] + orders["buys"]:
            size = f"qty {o['qty']:g} (~${o['notional']:,.2f})" if o.get("qty") else f"${o['notional']:,.2f}"
            p(f"  {o['side'].upper():<4} {o['symbol']:<6} {o['kind']:<6} {size}")
        for s in orders["skipped_under_threshold"]:
            p(f"  SKIP {s['symbol']:<6} {s['note']} (delta ${s['delta']:+,.2f})")
        if not (orders["sells"] or orders["buys"]):
            p("  (none)")
        t = record["planned_turnover"]
        p(f"\nTarget: {len(record['target_weights'])} holdings at {strat.TARGET_WEIGHT:.0%} each | "
          f"planned turnover {t['one_sided']:.1%} (gross ${t['gross_traded']:,.2f})")
        p(f"Target sector counts: {record['target_sector_counts']}\n")

    # --- daily snapshot -----------------------------------------------------------
    def snapshot(self):
        now = self.now()
        today = now.date()
        cal = self.calendar_day(today)
        if cal is None:
            log.info("%s is not a trading day; no snapshot.", today)
            return 0
        close_et = cal.close.replace(tzinfo=ET)
        if now < close_et + SNAPSHOT_DELAY_AFTER_CLOSE:
            log.warning("Market closes at %s ET; snapshot must run after %s ET.", f"{close_et:%H:%M}",
                        f"{close_et + SNAPSHOT_DELAY_AFTER_CLOSE:%H:%M}")
            return 1
        account = self.tc.get_account()
        positions = self.positions()
        bench, _ = self.fetch_closes(["SPY", "QQQ"], today, now - SNAPSHOT_DELAY_AFTER_CLOSE,
                                     adjustment=Adjustment.RAW)
        row_date = pd.Timestamp(today)
        spy = float(bench.at[row_date, "SPY"]) if row_date in bench.index else None
        qqq = float(bench.at[row_date, "QQQ"]) if row_date in bench.index else None
        with self.db() as conn:
            conn.execute("""INSERT INTO ndx_daily (date, equity, cash, n_positions, spy_close, qqq_close, recorded_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(date) DO UPDATE SET equity=excluded.equity, cash=excluded.cash,
                              n_positions=excluded.n_positions, spy_close=excluded.spy_close,
                              qqq_close=excluded.qqq_close, recorded_at=excluded.recorded_at""",
                         (today.isoformat(), float(account.equity), float(account.cash), len(positions),
                          spy, qqq, now.isoformat(timespec="seconds")))
        log.info("Snapshot %s: equity $%s, cash $%s, %d positions, SPY %s, QQQ %s", today,
                 f"{float(account.equity):,.2f}", f"{float(account.cash):,.2f}", len(positions), spy, qqq)
        return self._check_missed_rebalance(today)

    def _check_missed_rebalance(self, today):
        days = self.trading_days(date(today.year, today.month, 1), today)
        first_day = min(days)
        month = f"{today:%Y-%m}"
        rebal = self.get_rebalance(month)
        if rebal and rebal["status"] == "completed":
            return 0
        with self.db() as conn:
            was_live = conn.execute("SELECT 1 FROM ndx_daily WHERE date < ? LIMIT 1",
                                    (first_day.isoformat(),)).fetchone()
        if not was_live:
            return 0  # strategy wasn't running yet on this month's first trading day
        self.alert("Monthly rebalance missing",
                   f"No completed rebalance for {month} (status: {rebal['status'] if rebal else 'never ran'}). "
                   f"Run `python -m scripts.run_ndx_mom_buffer rebalance "
                   f"{'--resume' if rebal else '--force'}` during market hours.")
        return 2

    # --- report ---------------------------------------------------------------------
    def report(self):
        with self.db() as conn:
            daily = pd.read_sql("SELECT * FROM ndx_daily ORDER BY date", conn)
            rebals = pd.read_sql("SELECT * FROM ndx_rebalances ORDER BY month", conn)
            first_trade = conn.execute(
                "SELECT MIN(trade_date) FROM ndx_orders WHERE trade_date IS NOT NULL").fetchone()[0]
        print(f"\n=== {STRATEGY_NAME} -- status report ===")
        if rebals.empty:
            print("No rebalance has run yet. The first one happens on the next first trading day of a month.")
        else:
            last = rebals.iloc[-1]
            print(f"Last rebalance: {last['month']} -> {last['status']} "
                  f"(turnover {100 * (last['turnover_one_sided'] or 0):.1f}%), record: {last['record_path']}")
            bad = rebals[rebals["status"] != "completed"]
            if not bad.empty:
                print(f"INCOMPLETE rebalances: {', '.join(bad['month'])}")

        positions = self.positions()
        account = self.tc.get_account()
        equity = float(account.equity)
        sector_map = self._cached_sector_map()
        print(f"\nAccount equity ${equity:,.2f} | cash ${float(account.cash):,.2f} | {len(positions)} holdings")
        for sym, p in sorted(positions.items(), key=lambda kv: -kv[1]["market_value"]):
            print(f"  {sym:<6} ${p['market_value']:>11,.2f}  {p['market_value'] / equity:>6.1%}  "
                  f"{sector_map.get(sym, 'Unknown')}")
        counts = strat.sector_counts(positions.keys(), sector_map)
        print(f"Holdings by sector: {counts}")
        if positions and max(counts.values()) / len(positions) >= 0.5:
            top = next(iter(counts))
            print(f"NOTE: {counts[top]} of {len(positions)} holdings are in {top} -- effectively a single-sector bet.")

        if not first_trade or daily.empty:
            print("\nNo performance history yet (needs a completed rebalance plus daily snapshots).")
            return 0
        daily["date"] = pd.to_datetime(daily["date"])
        before = daily[daily["date"] < pd.Timestamp(first_trade)]
        start = before["date"].iloc[-1] if not before.empty else pd.Timestamp(first_trade)
        series = daily[daily["date"] >= start].set_index("date")
        if len(series) < 2:
            print("\nNot enough daily snapshots yet to compute performance.")
            return 0

        bench_note = "dividend-adjusted"
        try:
            bench, _ = self.fetch_closes(["SPY", "QQQ"], start.date(),
                                         datetime.combine(series.index[-1].date(), dtime(23, 59), tzinfo=ET))
            bench = bench.reindex(series.index).ffill()
        except Exception as e:
            bench = series[["spy_close", "qqq_close"]].rename(columns={"spy_close": "SPY", "qqq_close": "QQQ"})
            bench_note = f"raw logged closes, excludes dividends (adjusted fetch failed: {e})"
        frame = pd.DataFrame({"Strategy": series["equity"], "SPY": bench["SPY"], "QQQ": bench["QQQ"]}).dropna()

        cum = frame.iloc[-1] / frame.iloc[0] - 1
        mdd = (frame / frame.cummax() - 1).min()
        print(f"\nPerformance {frame.index[0]:%Y-%m-%d} to {frame.index[-1]:%Y-%m-%d} (benchmarks {bench_note}):")
        print(f"  {'':<10}{'Cum return':>12}{'Max drawdown':>14}")
        for col in frame.columns:
            print(f"  {col:<10}{cum[col]:>+12.2%}{mdd[col]:>14.2%}")
        print(f"  Excess vs SPY: {cum['Strategy'] - cum['SPY']:+.2%} | "
              f"vs QQQ (fair benchmark): {cum['Strategy'] - cum['QQQ']:+.2%}")

        month_end = frame.resample("ME").last()
        monthly = pd.concat([frame.iloc[[0]], month_end]).pct_change().iloc[1:]
        monthly.index = monthly.index.strftime("%Y-%m")
        monthly["vs QQQ"] = monthly["Strategy"] - monthly["QQQ"]
        print("\nMonthly returns:")
        print(monthly.map(lambda v: f"{v:+.2%}").to_string())
        return 0

    def _cached_sector_map(self):
        path = os.path.join(self.data_dir, "universe_cache.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return {m["symbol"]: m["sector"] for m in json.load(f)["members"]}


def check_accounts():
    """Prints each strategy profile's Alpaca paper account number and flags any sharing."""
    rows = []
    mine_key = None
    if os.path.exists(PROFILE_FILE):
        vals = dotenv_values(PROFILE_FILE)
        mine_key = vals.get("ALPACA_API_KEY")
        try:
            acct = make_trading_client(mine_key, vals.get("ALPACA_SECRET_KEY")).get_account().account_number
            rows.append((STRATEGY_NAME, PROFILE_FILE, acct, None))
        except Exception as e:
            rows.append((STRATEGY_NAME, PROFILE_FILE, None, f"could not query ({e})"))
    else:
        rows.append((STRATEGY_NAME, PROFILE_FILE, None, "profile file not present"))
    rows.extend(other_profile_accounts(mine_key))

    print(f"{'Strategy':<30}{'Profile':<22}{'Paper account':<16}Note")
    for label, env_file, acct, note in rows:
        print(f"{label:<30}{env_file:<22}{acct or '-':<16}{note or ''}")
    accts = [a for _, _, a, _ in rows if a]
    dupes = {a for a in accts if accts.count(a) > 1 or a == "SAME-API-KEY"}
    if dupes:
        print(f"\nSHARED ACCOUNT(S) DETECTED: {', '.join(sorted(dupes))} -- strategies must not share accounts.")
        return 1
    print("\nNo shared accounts among the profiles that could be checked.")
    return 0
