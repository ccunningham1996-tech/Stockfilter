"""Offline tests for NDX Momentum Buffered: strategy rules against synthetic
prices, and the runner against a fake in-memory Alpaca account."""
import json
import math
import os
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alpaca.trading.enums import OrderStatus

from src import ndx_mom_buffer as strat
from src import ndx_mom_buffer_runner as rn
from src.ndx_universe import load_universe, parse_components

ET = rn.ET
HOLIDAYS = {date(2025, 12, 25), date(2026, 1, 1), date(2026, 9, 7), date(2025, 12, 31)}


def calendar(start="2024-06-03", end="2026-12-31"):
    return [d.date() for d in pd.bdate_range(start, end) if d.date() not in HOLIDAYS]


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def test_month_ends_follow_the_market_calendar_not_weekdays():
    mes = strat.month_end_trading_days(calendar())
    assert date(2025, 12, 30) in mes          # Dec 31 2025 is a holiday in this calendar
    assert date(2025, 12, 31) not in mes
    assert strat.is_first_trading_day_of_month(calendar(), date(2026, 1, 2))   # Jan 1 closed
    assert not strat.is_first_trading_day_of_month(calendar(), date(2026, 1, 5))
    assert strat.is_first_trading_day_of_month(calendar(), date(2026, 9, 1))
    assert strat.signal_date_for(calendar(), date(2026, 10, 1)) == date(2026, 9, 30)
    assert strat.signal_date_for(calendar(), date(2026, 1, 2)) == date(2025, 12, 30)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def _frame(days, series):
    return pd.DataFrame(series, index=pd.DatetimeIndex([pd.Timestamp(d) for d in days]))


def test_momentum_is_12_1_and_skips_the_latest_month():
    days = [d for d in calendar() if d <= date(2026, 9, 30)]
    t0 = date(2026, 9, 30)
    mes = [d for d in strat.month_end_trading_days(calendar()) if d <= t0]
    n = len(days)
    smooth = 100 * np.exp(np.linspace(0, 1, n))
    jumpy = smooth.copy()
    last_month = [i for i, d in enumerate(days) if d > mes[-2]]
    jumpy[last_month] *= 1.5                  # huge move inside the skipped month
    closes = _frame(days, {"SMOOTH": smooth, "JUMPY": jumpy})
    sig = strat.compute_signals(closes, mes, t0).set_index("symbol")

    s = closes["SMOOTH"]
    expected = s[pd.Timestamp(mes[-2])] / s[pd.Timestamp(mes[-13])] - 1
    assert sig.loc["SMOOTH", "momentum"] == pytest.approx(expected)
    assert sig.loc["JUMPY", "momentum"] == pytest.approx(expected)   # last month ignored
    assert sig.loc["SMOOTH", "trend_ok"]
    assert sig.loc["SMOOTH", "sma200"] == pytest.approx(s.iloc[-200:].mean())


def test_trend_and_eligibility_rules():
    days = [d for d in calendar() if d <= date(2026, 9, 30)]
    t0 = days[-1]
    mes = [d for d in strat.month_end_trading_days(calendar()) if d <= t0]
    n = len(days)
    falling = 200 * np.exp(np.linspace(0, -0.6, n))
    short = np.full(n, np.nan)
    short[-150:] = np.linspace(50, 80, 150)                        # 150 days only
    young = np.full(n, np.nan)
    first_idx = days.index(next(d for d in days if d > mes[-13]))  # starts after 13th month-end
    young[first_idx:] = np.linspace(50, 80, n - first_idx)         # >200 days but <13 month-ends
    closes = _frame(days, {"FALL": falling, "SHORT": short, "YOUNG": young})
    sig = strat.compute_signals(closes, mes, t0).set_index("symbol")

    assert sig.loc["FALL", "eligible"] and not sig.loc["FALL", "trend_ok"]
    assert not sig.loc["SHORT", "eligible"] and "days of history" in sig.loc["SHORT", "ineligible_reason"]
    assert sig.loc["YOUNG", "n_days"] >= 200
    assert not sig.loc["YOUNG", "eligible"] and "month-ends" in sig.loc["YOUNG", "ineligible_reason"]


# ---------------------------------------------------------------------------
# Buffer rule
# ---------------------------------------------------------------------------

def _signals(rows):
    """rows: (symbol, rank or None, trend_ok)"""
    out = []
    for sym, rank, trend in rows:
        out.append({"symbol": sym, "rank": rank, "eligible": rank is not None, "trend_ok": trend,
                    "momentum": (1 - rank / 100) if rank else None, "close": 110.0 if trend else 90.0,
                    "sma200": 100.0, "ineligible_reason": None if rank else "only 5 month-ends of history (<13)"})
    df = pd.DataFrame(out)
    return pd.concat([df[df.eligible].sort_values("rank"), df[~df.eligible]]).reset_index(drop=True)


def test_buffer_keeps_top20_trend_ok_holdings_and_fills_from_the_top():
    rows = [(f"R{r:02d}", r, r not in (2, 4)) for r in range(1, 31)]  # R02, R04 fail trend
    rows.append(("NEWBIE", None, True))
    sig = _signals(rows)
    held = {"R15", "R25", "R04", "GONE", "NEWBIE"}
    decisions, targets = strat.build_portfolio(sig, held)
    by = {d["symbol"]: d for d in decisions}

    assert by["R15"]["action"] == "keep"                       # rank 15 <= 20, trend OK
    assert by["R25"]["action"] == "sell" and "rank 25 > 20" in by["R25"]["reason"]
    assert by["R04"]["action"] == "sell" and "trend fail" in by["R04"]["reason"]
    assert by["GONE"]["action"] == "sell" and "universe" in by["GONE"]["reason"]
    assert by["NEWBIE"]["action"] == "sell" and "ineligible" in by["NEWBIE"]["reason"]
    # fills skip R02/R04 (trend fail) and anything already held
    assert targets == ["R15", "R01", "R03", "R05", "R06", "R07", "R08", "R09", "R10", "R11"]
    assert len(targets) == strat.TARGET_HOLDINGS


def test_unfilled_slots_stay_in_cash_no_market_filter():
    sig = _signals([(f"R{r:02d}", r, r <= 6) for r in range(1, 40)])  # only 6 trend-OK names
    _, targets = strat.build_portfolio(sig, set())
    assert targets == [f"R{r:02d}" for r in range(1, 7)]


def test_fill_excludes_current_holdings_even_if_they_were_sold():
    # Literal rule: FILL picks names "not already held". R25 is held, fails the
    # keep test (rank > 20) and is sold; it is not re-bought via FILL.
    sig = _signals([(f"R{r:02d}", r, r >= 21) for r in range(1, 40)])
    decisions, targets = strat.build_portfolio(sig, {"R25"})
    assert {d["symbol"]: d["action"] for d in decisions}["R25"] == "sell"
    assert "R25" not in targets and targets[0] == "R21"


# ---------------------------------------------------------------------------
# Order planning
# ---------------------------------------------------------------------------

def test_plan_orders_exits_by_qty_resizes_over_200_and_new_at_10pct():
    decisions = [{"symbol": "OUT", "action": "sell", "reason": "rank 30 > 20", "rank": 30},
                 {"symbol": "BIG", "action": "keep", "reason": "k", "rank": 1},
                 {"symbol": "SMALL", "action": "keep", "reason": "k", "rank": 2},
                 {"symbol": "EXACT", "action": "keep", "reason": "k", "rank": 3},
                 {"symbol": "NEW", "action": "buy", "reason": "b", "rank": 4}]
    positions = {"OUT": {"qty": 3.25, "market_value": 5000.0},
                 "BIG": {"qty": 1, "market_value": 11000.0},     # 1000 over target -> trim
                 "SMALL": {"qty": 1, "market_value": 9500.0},    # 500 under -> top up
                 "EXACT": {"qty": 1, "market_value": 10150.0}}   # 150 over -> skip
    sells, buys, skipped = strat.plan_orders(decisions, ["BIG", "SMALL", "EXACT", "NEW"], positions, 100_000)
    assert {(o["symbol"], o["kind"]) for o in sells} == {("OUT", "exit"), ("BIG", "trim")}
    assert next(o for o in sells if o["symbol"] == "OUT")["qty"] == 3.25
    assert next(o for o in sells if o["symbol"] == "BIG")["notional"] == 1000.0
    assert {(o["symbol"], o["kind"], o["notional"]) for o in buys} == {("SMALL", "topup", 500.0), ("NEW", "new", 10000.0)}
    assert [s["symbol"] for s in skipped] == ["EXACT"]


def test_buys_scaled_down_rather_than_using_margin():
    buys = [{"symbol": "A", "notional": 10000.0}, {"symbol": "B", "notional": 10000.0}]
    scaled, factor = strat.scale_buys_to_cash(buys, 15000.0)
    assert factor == pytest.approx(0.75) and sum(b["notional"] for b in scaled) <= 15000.0
    assert strat.scale_buys_to_cash(buys, 50000.0) == (buys, 1.0)


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def _tables(n=101):
    syms = ["GOOGL", "GOOG"] + [f"S{i:03d}" for i in range(n - 2)]
    return [pd.DataFrame({"x": [1]}),
            pd.DataFrame({"Company": syms, "Ticker": syms, "GICS Sector": ["Tech"] * n,
                          "GICS Sub-Industry": ["x"] * n})]


def test_universe_drops_goog_and_falls_back_to_dated_cache(tmp_path, caplog):
    members, source = parse_components(_tables())
    syms = [m["symbol"] for m in members]
    assert "GOOGL" in syms and "GOOG" not in syms and source == "GICS Sector"

    cache = str(tmp_path / "u.json")
    live = load_universe(cache, fetch=lambda: parse_components(_tables()))
    assert live["source"] == "live" and os.path.exists(cache)

    def boom():
        raise ConnectionError("wikipedia down")
    cached = load_universe(cache, fetch=boom)
    assert cached["source"] == "cache" and cached["as_of"] == live["as_of"]
    assert "wikipedia down" in cached["warning"] and "wikipedia down" in caplog.text

    with pytest.raises(RuntimeError):
        load_universe(str(tmp_path / "missing.json"), fetch=boom)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_refuses_non_paper_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.ndx_mom_buffer").write_text(
        "ALPACA_API_KEY=k\nALPACA_SECRET_KEY=s\nALPACA_BASE_URL=https://api.alpaca.markets\n")
    with pytest.raises(rn.GuardError, match="paper-api"):
        rn.load_profile()
    (tmp_path / ".env.ndx_mom_buffer").write_text("ALPACA_API_KEY=k\nALPACA_SECRET_KEY=s\n")
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(rn.GuardError):
        rn.load_profile()
    monkeypatch.delenv("APCA_API_BASE_URL")
    assert rn.load_profile()["ALPACA_API_KEY"] == "k"
    assert str(rn.make_trading_client("k", "s")._base_url) == rn.PAPER_URL


def test_refuses_missing_or_placeholder_profile(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(rn.GuardError, match="not found"):
        rn.load_profile()
    (tmp_path / ".env.ndx_mom_buffer").write_text(
        "ALPACA_API_KEY=your_ndx_key\nALPACA_SECRET_KEY=s\n")
    with pytest.raises(rn.GuardError, match="placeholder"):
        rn.load_profile()


def test_refuses_account_shared_with_another_strategy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ALPACA_API_KEY=momkey\nALPACA_SECRET_KEY=s\n")
    (tmp_path / ".env.value").write_text("ALPACA_API_KEY=valkey\nALPACA_SECRET_KEY=s\n")
    accounts = {"momkey": "PA_MOM", "valkey": "PA_VAL", "ndxkey": "PA_NDX"}

    class FakeTC:
        def __init__(self, key, secret, **kw):
            self.key, self._base_url = key, rn.PAPER_URL

        def get_account(self):
            return SimpleNamespace(account_number=accounts[self.key])

    monkeypatch.setattr(rn, "TradingClient", FakeTC)
    assert rn.assert_dedicated_account(FakeTC("ndxkey", "s"), "ndxkey") == "PA_NDX"

    accounts["ndxkey"] = "PA_MOM"                                   # same account, different key
    with pytest.raises(rn.GuardError, match="Momentum"):
        rn.assert_dedicated_account(FakeTC("ndxkey", "s"), "ndxkey")
    with pytest.raises(rn.GuardError, match="Momentum"):              # literally the same key
        rn.assert_dedicated_account(FakeTC("momkey", "s"), "momkey")


# ---------------------------------------------------------------------------
# Runner against a fake Alpaca account
# ---------------------------------------------------------------------------

SYMBOLS = [f"T{i:02d}" for i in range(30)]


def synthetic_closes():
    days = [d for d in calendar() if d <= date(2026, 12, 31)]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
    t = np.arange(len(days)) / 252
    data = {}
    for i, s in enumerate(SYMBOLS):
        drift = 0.60 - 0.04 * i                     # T00 strongest ... T29 falling
        data[s] = 100 * np.exp(drift * t) * (1 + 0.01 * np.sin(t * 40 + i))
    return pd.DataFrame(data, index=idx)


CLOSES = synthetic_closes()


class FakeBroker:
    def __init__(self, now_holder, reject=(), fail_once=()):
        self.now_holder = now_holder
        self.cash = 100_000.0
        self.qty = {}
        self.orders = {}
        self.events = []
        self.reject, self.fail_once = set(reject), set(fail_once)
        self._base_url = rn.PAPER_URL

    def price(self, sym):
        d = pd.Timestamp(self.now_holder["now"].date())
        return float(CLOSES[sym].loc[:d].iloc[-1])

    def get_calendar(self, f):
        return [SimpleNamespace(date=d, open=datetime(d.year, d.month, d.day, 9, 30),
                                close=datetime(d.year, d.month, d.day, 16, 0))
                for d in calendar() if f.start <= d <= f.end]

    def get_clock(self):
        return SimpleNamespace(is_open=True)

    def get_account(self):
        equity = self.cash + sum(q * self.price(s) for s, q in self.qty.items())
        return SimpleNamespace(equity=str(equity), cash=str(self.cash), account_number="PA_NDX")

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, qty=str(q), market_value=str(q * self.price(s)),
                                current_price=str(self.price(s))) for s, q in self.qty.items() if q > 1e-9]

    def get_asset(self, sym):
        return SimpleNamespace(fractionable=True)

    def submit_order(self, req):
        side = req.side.value
        assert req.client_order_id.startswith("ndxmb-")
        if side == "buy":
            open_sells = [o for o in self.orders.values() if o.side == "sell" and o.status != OrderStatus.FILLED]
            assert not open_sells, "a buy was submitted while sells were still open"
        self.events.append((side, req.symbol, req.client_order_id))
        if req.symbol in self.fail_once:
            self.fail_once.discard(req.symbol)
            raise RuntimeError("simulated API hiccup")
        status = OrderStatus.REJECTED if req.symbol in self.reject else OrderStatus.ACCEPTED
        o = SimpleNamespace(id=uuid.uuid4(), client_order_id=req.client_order_id, symbol=req.symbol,
                            side=side, qty=req.qty, notional=req.notional, status=status,
                            filled_qty="0", filled_avg_price=None, filled_at=None)
        self.orders[str(o.id)] = o
        return o

    def get_order_by_id(self, oid):
        o = self.orders[str(oid)]
        if o.status == OrderStatus.ACCEPTED:
            px = self.price(o.symbol)
            q = float(o.qty) if o.qty is not None else float(o.notional) / px
            if o.side == "sell":
                q = min(q, self.qty.get(o.symbol, 0))
                self.qty[o.symbol] = self.qty.get(o.symbol, 0) - q
                self.cash += q * px
            else:
                self.qty[o.symbol] = self.qty.get(o.symbol, 0) + q
                self.cash -= q * px
            o.status, o.filled_qty, o.filled_avg_price = OrderStatus.FILLED, str(q), str(px)
            o.filled_at = self.now_holder["now"]
        return o

    def cancel_order_by_id(self, oid):
        self.orders[str(oid)].status = OrderStatus.CANCELED

    def get_orders(self, f):
        return list(self.orders.values())


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    holder = {"now": datetime(2026, 10, 1, 9, 45, tzinfo=ET)}   # first trading day of October

    def make(**broker_kw):
        broker = FakeBroker(holder, **broker_kw)
        members = [{"symbol": s, "company": s, "sector": "Information Technology" if i % 3 else "Health Care"}
                   for i, s in enumerate(SYMBOLS)]
        runner = rn.Runner({}, broker, None, now_fn=lambda: holder["now"],
                           db_path=str(tmp_path / "ndx.db"), data_dir=str(tmp_path / "out"),
                           universe_fetch=lambda: (members, "GICS Sector"), sleep=lambda s: None)

        def fake_fetch(symbols, start, end_dt, adjustment=None):
            end = pd.Timestamp(end_dt.date())
            return CLOSES.loc[pd.Timestamp(start):end, [s for s in symbols if s in CLOSES]], "sip"
        runner.fetch_closes = fake_fetch
        return runner, broker
    return make, holder, tmp_path


def test_first_rebalance_buys_top10_trend_ok_sells_first_and_records(env, capsys):
    make, holder, tmp = env
    runner, broker = make()
    assert runner.rebalance() == 0

    assert [e[0] for e in broker.events] == ["buy"] * 10
    assert {e[1] for e in broker.events} == {f"T{i:02d}" for i in range(10)}
    held = {p.symbol: float(p.market_value) for p in broker.get_all_positions()}
    equity = float(broker.get_account().equity)
    assert len(held) == 10 and all(v / equity == pytest.approx(0.10, abs=1e-6) for v in held.values())

    rec = json.load(open(tmp / "out" / "rebalances" / "2026-10.json"))
    assert rec["status"] == "completed" and rec["signal_date"] == "2026-09-30"
    assert rec["universe"]["size"] == 30 and len(rec["ranked"]) == 30
    assert all(0.099 < w < 0.101 for w in rec["actual_weights"].values())
    assert rec["turnover"]["one_sided"] == pytest.approx(0.5, abs=0.01)
    assert os.path.exists(tmp / "out" / "rebalances" / "2026-10_ranked.csv")
    assert "LIVE REBALANCE" in capsys.readouterr().out


def test_second_run_same_month_is_a_no_op(env):
    make, holder, _ = env
    runner, broker = make()
    runner.rebalance()
    n = len(broker.events)
    holder["now"] = holder["now"].replace(hour=11)
    assert runner.rebalance() == 0 and len(broker.events) == n


def test_does_nothing_unless_first_trading_day(env):
    make, holder, _ = env
    holder["now"] = datetime(2026, 10, 2, 9, 45, tzinfo=ET)
    runner, broker = make()
    assert runner.rebalance() == 0 and broker.events == []


def test_monthly_rebalance_sells_before_buys_and_restores_10pct(env):
    make, holder, tmp = env
    runner, broker = make()
    runner.rebalance()
    broker.qty["T03"] *= 1.5                               # drifted overweight -> trim
    broker.qty["ZZZ_OLD"] = 10.0                           # a name that left the universe
    CLOSES["ZZZ_OLD"] = 50.0
    try:
        holder["now"] = datetime(2026, 11, 2, 9, 45, tzinfo=ET)
        assert runner.rebalance() == 0
    finally:
        CLOSES.drop(columns="ZZZ_OLD", inplace=True)
    nov = [e for e in broker.events if "-2026-11-" in e[2]]
    sides = [e[0] for e in nov]
    assert "sell" in sides and sides == sorted(sides, key=lambda s: s != "sell")   # all sells first
    assert ("sell", "ZZZ_OLD") in {(e[0], e[1]) for e in nov}
    rec = json.load(open(tmp / "out" / "rebalances" / "2026-11.json"))
    reasons = {d["symbol"]: d["reason"] for d in rec["decisions"]}
    assert "universe" in reasons["ZZZ_OLD"]


def test_failed_order_is_retried_once(env):
    make, *_ = env
    runner, broker = make(fail_once={"T00"})
    assert runner.rebalance() == 0
    t00 = [e[2] for e in broker.events if e[1] == "T00"]
    assert len(t00) == 2 and t00[0].endswith("-a1") and t00[1].endswith("-a2")


def test_persistent_failure_alerts_and_blocks_silent_rerun(env):
    make, holder, tmp = env
    runner, broker = make(reject={"T01"})
    assert runner.rebalance() == 2
    assert len([e for e in broker.events if e[1] == "T01"]) == 2          # original + one retry
    alerts = open(tmp / "out" / "ALERTS.log").read()
    assert "Rebalance incomplete" in alerts and "T01" in alerts
    rec = json.load(open(tmp / "out" / "rebalances" / "2026-10.json"))
    assert rec["status"] == "partial"

    holder["now"] = holder["now"].replace(hour=10)
    assert runner.rebalance() == 2                                           # needs --resume
    broker.reject.clear()
    assert runner.rebalance(resume=True) == 0
    assert "T01" in {p.symbol for p in broker.get_all_positions()}
    assert runner.get_rebalance("2026-10")["status"] == "completed"


def test_dry_run_trades_nothing_on_any_day(env, capsys):
    make, holder, tmp = env
    holder["now"] = datetime(2026, 9, 22, 20, 0, tzinfo=ET)                  # mid-month, after hours
    runner, broker = make()
    assert runner.rebalance(dry_run=True) == 0
    out = capsys.readouterr().out
    assert broker.events == [] and "DRY RUN" in out and "BUY  T00" in out
    assert "signals as of close 2026-08-31" in out
    assert runner.get_rebalance("2026-09") is None
    assert os.listdir(tmp / "out" / "dryrun")


def test_outside_window_alerts_instead_of_trading(env):
    make, holder, tmp = env
    holder["now"] = datetime(2026, 10, 1, 16, 5, tzinfo=ET)
    runner, broker = make()
    assert runner.rebalance() == 2 and broker.events == []
    assert "Outside rebalance window" in open(tmp / "out" / "ALERTS.log").read()


def test_snapshot_and_missed_rebalance_alert(env, monkeypatch):
    make, holder, tmp = env
    runner, broker = make()

    def fake_fetch(symbols, start, end_dt, adjustment=None):
        idx = pd.DatetimeIndex([pd.Timestamp(start)])
        return pd.DataFrame({"SPY": [600.0], "QQQ": [520.0]}, index=idx), "sip"
    runner.fetch_closes = fake_fetch

    holder["now"] = datetime(2026, 9, 30, 16, 5, tzinfo=ET)                  # too early
    assert runner.snapshot() == 1
    holder["now"] = datetime(2026, 9, 30, 16, 30, tzinfo=ET)
    assert runner.snapshot() == 0                                            # not live yet: no alert
    holder["now"] = datetime(2026, 10, 2, 16, 30, tzinfo=ET)                 # Oct rebalance never ran
    assert runner.snapshot() == 2
    assert "Monthly rebalance missing" in open(tmp / "out" / "ALERTS.log").read()
    with rn.connect(str(tmp / "ndx.db")) as conn:
        rows = conn.execute("SELECT date, spy_close, qqq_close FROM ndx_daily ORDER BY date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-09-30", 600.0, 520.0), ("2026-10-02", 600.0, 520.0)]


def test_report_shows_benchmarks_drawdown_monthly_and_sectors(env, capsys):
    make, holder, tmp = env
    runner, broker = make()
    real_fetch = runner.fetch_closes
    with rn.connect(str(tmp / "ndx.db")) as conn:
        conn.execute("INSERT INTO ndx_daily VALUES ('2026-09-30', 100000, 100000, 0, 0, 0, 'x')")
    runner.rebalance()
    for d in [d for d in calendar() if date(2026, 10, 1) <= d <= date(2026, 11, 30)]:
        holder["now"] = datetime(d.year, d.month, d.day, 16, 30, tzinfo=ET)
        eq = float(broker.get_account().equity)
        with rn.connect(str(tmp / "ndx.db")) as conn:
            conn.execute("INSERT OR REPLACE INTO ndx_daily VALUES (?, ?, 0, 10, 0, 0, 'x')", (d.isoformat(), eq))

    bench = pd.DataFrame({"SPY": np.linspace(600, 620, len(CLOSES)), "QQQ": np.linspace(500, 540, len(CLOSES))},
                         index=CLOSES.index)
    runner.fetch_closes = lambda symbols, start, end_dt, adjustment=None: (
        bench.loc[pd.Timestamp(start):pd.Timestamp(end_dt.date())], "sip")
    assert runner.report() == 0
    out = capsys.readouterr().out
    for needle in ("Cum return", "Max drawdown", "vs QQQ (fair benchmark)", "Monthly returns",
                   "2026-10", "2026-11", "Holdings by sector"):
        assert needle in out
    runner.fetch_closes = real_fetch
